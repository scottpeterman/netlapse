#!/usr/bin/env python3
"""
scripts/netlapse_import_map.py — import sites, devices and platforms into
Netlapse from a topology map.json.

Reads a discovery map, recovers each device's name and site, classifies its
platform against Netlapse reference data, creates any missing sites via
/api/v1/admin/dcim/sites, then creates devices via /api/v1/devices.

Auth matches netlapse_api.py: session cookie via POST /api/v1/auth/login, or a
bearer token with --token. Stdlib only. Requires netlapse_map_model.py
alongside it.

Site derivation
    Sites come from a label of the FQDN, not a hardcoded list:

        lab-core-sw01.site1.lab.example.com  ->  site "site1"
                      ^-- label index 1 (default)

    Nodes keyed by IP address have no label to read, so the name is recovered
    from the map's own metadata (fqdn, sys_name) or reverse DNS before the site
    is derived. Anything still unresolved goes to --default-site or is skipped
    and reported — an octet is never promoted to a site.

Platform
    node_details.platform is prose, not a slug, so it is classified against
    vendor / model / os_version / sys_descr rather than matched literally.
    Low-confidence results are listed in the report; --strict-platform declines
    to apply them.

CGNAT handling
    Any management address inside 100.64.0.0/10 is re-resolved forward from the
    device name and the answer used instead, with the original address kept in
    the device comments. --no-dns disables all lookups.

Usage:
    export NETLAPSE_USER=labadmin
    export NETLAPSE_PASS=...

    # see what would happen — no writes
    python netlapse_import_map.py map.json --dry-run

    # create sites + devices
    python netlapse_import_map.py map.json

    # peers discovered but never polled directly, into a holding site
    python netlapse_import_map.py map.json --include-peers --default-site staging

    # lab box with a self-signed cert
    python netlapse_import_map.py map.json --base https://netlapse.lab.example.com --insecure
"""

import argparse
import getpass
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

try:
    from netlapse_map_model import (
        classify_platform, extract_devices, name_handles, netlapse_name,
        resolve_management_ip, resolve_name, site_for,
    )
except ImportError:
    sys.exit("netlapse_map_model.py must sit alongside this script")

KIND_SITES = "sites"
KIND_PLATFORMS = "platforms"
KIND_ROLES = "roles"


class Netlapse:
    """Thin session-cookie / bearer-token client for the Netlapse API."""

    def __init__(self, base, verify=True, timeout=20):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token = None
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def request(self, method, path, body=None):
        url = path if path.startswith("http") else f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            return resp.getcode(), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            print(f"connection error: {e.reason}", file=sys.stderr)
            sys.exit(2)

    def get_json(self, path):
        code, body = self.request("GET", path)
        if code != 200:
            raise RuntimeError(f"GET {path} -> HTTP {code}: {body[:300]}")
        return json.loads(body) if body.strip() else None

    def login(self, user, password):
        code, body = self.request(
            "POST", "/api/v1/auth/login",
            body={"username": user, "password": password},
        )
        if code != 200:
            print(f"login failed (HTTP {code}): {body}", file=sys.stderr)
            sys.exit(1)
        print(f"logged in as {user}")


def rows(payload):
    """Reference endpoints return either a bare list or {'items': [...]}."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    for key in ("items", "results", "sites", "platforms", "roles", "devices", "data"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def slugify(value):
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in value.lower()]
    return "".join(keep).strip("-") or "site"


def index_reference(client, kind):
    """Map lowercase name AND slug -> id for a reference kind."""
    idx = {}
    try:
        payload = client.get_json(f"/api/v1/admin/dcim/{kind}")
    except RuntimeError as e:
        print(f"warning: could not list {kind}: {e}", file=sys.stderr)
        return idx
    for row in rows(payload):
        if not isinstance(row, dict):
            continue
        rid = row.get("id") or row.get("pk")
        if rid is None:
            continue
        for key in (row.get("name"), row.get("slug")):
            if key:
                idx[str(key).lower()] = rid
    return idx


def ensure_site(client, sites_idx, name):
    key = name.lower()
    if key in sites_idx:
        return sites_idx[key], False
    code, body = client.request(
        "POST", f"/api/v1/admin/dcim/{KIND_SITES}",
        body={"name": name, "slug": slugify(name)},
    )
    if code not in (200, 201):
        raise RuntimeError(f"create site {name!r} -> HTTP {code}: {body[:300]}")
    payload = json.loads(body) if body.strip() else {}
    sid = payload.get("id") or (payload.get("site") or {}).get("id")
    if sid is None:  # some builds return no body; re-read
        sites_idx.update(index_reference(client, KIND_SITES))
        sid = sites_idx.get(key)
    if sid is None:
        raise RuntimeError(f"created site {name!r} but could not determine its id")
    sites_idx[key] = sid
    return sid, True


def existing_device_index(client):
    """Map every handle an existing device answers to -> its stored name.

    Netlapse's own importer stores {shortname}.{site}; this one can store the
    FQDN. Indexing both forms plus the short name and the management address
    means a device already present is recognized whichever tool created it,
    instead of getting a second row under a different string.
    """
    idx = {}
    try:
        payload = client.get_json("/api/v1/devices")
    except RuntimeError as e:
        print(f"warning: could not list devices: {e}", file=sys.stderr)
        return idx
    for row in rows(payload):
        if not isinstance(row, dict) or not row.get("name"):
            continue
        stored = str(row["name"])
        for handle in name_handles(stored, row.get("primary_ip4")):
            idx.setdefault(handle, stored)
    return idx


def build_plan(args, site_map):
    with open(args.map_file, "r", encoding="utf-8") as fh:
        doc = json.load(fh)

    devices = extract_devices(doc, include_peers=args.include_peers)
    if not devices:
        print("no devices found in map — check the file shape", file=sys.stderr)
        sys.exit(1)

    allow_dns = not args.no_dns
    planned, skipped = [], []
    for rec in devices:
        name, name_source = resolve_name(rec, allow_dns=allow_dns)
        if not name:
            skipped.append((rec["key"], "no usable name"))
            continue
        site = site_for(name, args.site_label, site_map, args.default_site)
        if not site:
            skipped.append((name, f"no site could be derived (name from {name_source})"))
            continue
        ip, ip_note = resolve_management_ip(name, rec["ip"], allow_dns=allow_dns)
        if not ip:
            skipped.append((name, ip_note))
            continue

        if args.platform:
            slug, confidence, evidence = args.platform, "override", "--platform"
        else:
            slug, confidence, evidence = classify_platform(rec)

        style = "short" if args.short_names else args.name_style
        if style == "short":
            stored_name = name.split(".")[0]
        elif style == "fqdn":
            stored_name = name
        else:
            stored_name = netlapse_name(name)

        planned.append({
            "name": stored_name,
            "fqdn": name,
            "name_source": name_source,
            "site": site,
            "ip": ip,
            "ip_note": ip_note,
            "platform": slug,
            "confidence": confidence,
            "evidence": evidence,
            "direct": rec["direct"],
        })
    return devices, planned, skipped


def report(args, devices, planned, skipped):
    print(f"\nparsed {len(devices)} device(s) from {args.map_file}")
    by_site = {}
    for row in planned:
        by_site.setdefault(row["site"], []).append(row)

    for site in sorted(by_site):
        print(f"\n  site {site}  ({len(by_site[site])} device(s))")
        for row in sorted(by_site[site], key=lambda r: r["name"]):
            notes = []
            if not row["direct"]:
                notes.append("peer")
            if row["name_source"] != "map-key":
                notes.append(f"name from {row['name_source']}")
            if row["ip_note"]:
                notes.append(row["ip_note"])
            if row["platform"] and row["confidence"] in ("low", "medium"):
                notes.append(f"platform {row['confidence']}: {row['evidence']}")
            elif not row["platform"]:
                notes.append(f"no platform ({row['evidence']})")
            suffix = f"   <- {'; '.join(notes)}" if notes else ""
            plat = row["platform"] or "-"
            print(f"    {row['name']:<42} {row['ip']:<16} {plat:<18}{suffix}")

    for name, why in skipped:
        print(f"  SKIP {name}: {why}")

    unresolved = [r for r in planned if not r["platform"]]
    shaky = [r for r in planned if r["platform"] and r["confidence"] in ("low", "medium")]
    print(f"\n  platform: {len(planned) - len(unresolved)} classified, "
          f"{len(unresolved)} unresolved, {len(shaky)} low/medium confidence")
    return by_site


def main():
    ap = argparse.ArgumentParser(
        description="Import sites and devices into Netlapse from a topology map.json")
    ap.add_argument("map_file", help="Path to map.json")
    ap.add_argument("--base", default=os.environ.get("NETLAPSE_BASE", "http://localhost:8888"))
    ap.add_argument("-u", "--user", default=os.environ.get("NETLAPSE_USER"))
    ap.add_argument("-p", "--password", default=os.environ.get("NETLAPSE_PASS"))
    ap.add_argument("--token", help="API token (Bearer) — skips session login")
    ap.add_argument("--insecure", action="store_true",
                    help="Skip TLS certificate verification (self-signed lab certs)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Parse and report, but make no writes")
    ap.add_argument("--include-peers", action="store_true",
                    help="Also import neighbors that were never polled directly")
    ap.add_argument("--site-label", type=int, default=1,
                    help="FQDN label index used as the site (default 1)")
    ap.add_argument("--site-map", action="append", default=[], metavar="FROM=TO",
                    help="Rename a derived site, repeatable")
    ap.add_argument("--default-site",
                    help="Site for devices whose name yields no site label")
    ap.add_argument("--name-style", choices=("netlapse", "fqdn", "short"),
                    default="netlapse",
                    help="Device naming. 'netlapse' (default) matches the built-in "
                         "importer: {shortname}.{site}, domain stripped. 'fqdn' keeps "
                         "the full name. 'short' keeps only the first label.")
    ap.add_argument("--short-names", action="store_true",
                    help="Deprecated alias for --name-style short")
    ap.add_argument("--allow-duplicate-ip", action="store_true",
                    help="Create a device even when its address already has a row")
    ap.add_argument("--role", help="Role name applied to every imported device")
    ap.add_argument("--platform",
                    help="Platform slug forced on every device, skipping classification")
    ap.add_argument("--strict-platform", action="store_true",
                    help="Only apply high-confidence platform matches")
    ap.add_argument("--no-dns", action="store_true",
                    help="Never do DNS lookups, forward or reverse")
    ap.add_argument("--collection-enabled", action="store_true",
                    help="Enable collection on import (default: off, enable after review)")
    args = ap.parse_args()

    site_map = {}
    for pair in args.site_map:
        if "=" not in pair:
            ap.error(f"--site-map expects FROM=TO, got {pair!r}")
        frm, to = pair.split("=", 1)
        site_map[frm.strip().lower()] = to.strip()

    devices, planned, skipped = build_plan(args, site_map)
    by_site = report(args, devices, planned, skipped)

    if args.dry_run:
        print(f"\ndry run — would create {len(by_site)} site(s), {len(planned)} device(s)")
        return

    client = Netlapse(args.base, verify=not args.insecure)
    if args.token:
        client.token = args.token
    else:
        user = args.user or input("username: ")
        password = args.password or getpass.getpass("password: ")
        client.login(user, password)

    sites_idx = index_reference(client, KIND_SITES)
    platforms_idx = index_reference(client, KIND_PLATFORMS)
    roles_idx = index_reference(client, KIND_ROLES)
    print(f"reference data: {len(sites_idx)} site key(s), "
          f"{len(platforms_idx)} platform key(s), {len(roles_idx)} role key(s)")
    if not platforms_idx:
        print("warning: no platforms indexed — every device will import without one",
              file=sys.stderr)
    have = existing_device_index(client)

    role_id = None
    if args.role:
        role_id = roles_idx.get(args.role.lower())
        if role_id is None:
            print(f"warning: role {args.role!r} not found — importing without a role",
                  file=sys.stderr)

    created_sites = created_devices = with_platform = 0
    for site in sorted(by_site):
        try:
            site_id, is_new = ensure_site(client, sites_idx, site)
        except RuntimeError as e:
            print(f"ERROR {e}", file=sys.stderr)
            continue
        if is_new:
            created_sites += 1
            print(f"created site {site} (id {site_id})")

        for row in sorted(by_site[site], key=lambda r: r["name"]):
            handles = name_handles(row["fqdn"], row["ip"]) | {row["name"].lower()}
            match = next((have[h] for h in handles if h in have), None)
            if match is not None:
                if match.lower() != row["name"].lower():
                    print(f"  exists  {row['name']}  (already present as {match})")
                else:
                    print(f"  exists  {row['name']}")
                continue
            if not args.allow_duplicate_ip and row["ip"].lower() in have:
                print(f"  exists  {row['name']}  (address already on "
                      f"{have[row['ip'].lower()]})")
                continue
            body = {
                "name": row["name"],
                "site_id": site_id,
                "primary_ip4": row["ip"],
                "collection_enabled": bool(args.collection_enabled),
            }
            if role_id is not None:
                body["role_id"] = role_id

            slug = row["platform"]
            if slug and args.strict_platform and row["confidence"] not in ("high", "override"):
                slug = None
            if slug:
                pid = platforms_idx.get(slug.lower())
                if pid is not None:
                    body["platform_id"] = pid
                    with_platform += 1
                else:
                    print(f"  note    platform {slug!r} not in reference data "
                          f"({row['name']})", file=sys.stderr)
                    slug = None

            trail = []
            if row["ip_note"].startswith("cgnat-replaced"):
                trail.append(f"map address {row['ip_note'][15:].strip('()')} was CGNAT; "
                             f"{row['ip']} resolved from DNS")
            if row["name_source"] != "map-key":
                trail.append(f"name recovered via {row['name_source']}")
            if trail:
                body["comments"] = "; ".join(trail)

            code, resp = client.request("POST", "/api/v1/devices", body=body)
            if code in (200, 201):
                created_devices += 1
                for handle in name_handles(row["fqdn"], row["ip"]) | {row["name"].lower()}:
                    have.setdefault(handle, row["name"])
                print(f"  created {row['name']}  {row['ip']}  {slug or '-'}")
            else:
                print(f"  FAILED  {row['name']}: HTTP {code} {resp[:200]}", file=sys.stderr)

    print(f"\ndone — {created_sites} site(s), {created_devices} device(s) created, "
          f"{with_platform} with a platform")
    if created_devices and not args.collection_enabled:
        print("collection is OFF on the new devices; assign credentials, then enable.")


if __name__ == "__main__":
    main()