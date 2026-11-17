#!/usr/bin/env python3
"""
scripts/netlapse_prune_ip_devices.py — delete devices whose name is a bare IP
address.

Secure Cartography keys a node by its address when discovery reaches the device
but never resolves a hostname. netlapse's parse_hostname() passes an IPv4 key
through unchanged and files it under site 'unknown', so each failed discovery
becomes an inventory row named after an address.

Most of those are duplicates — the same box is also present under its real
name from a later discovery pass. Some are not, and deleting one of those
removes the only record that the device exists. This script separates the two
cases and only deletes the first by default.

  duplicate   another device carries the same primary_ip4 under a real name.
              Safe to delete: the box stays in inventory under its real row.
  sole record no other device carries that address. Deleting it drops the box
              from inventory entirely. Skipped unless --include-sole-records.
  collected   the row has collection history. Reported and skipped unless
              --include-collected, because its captures are attributed to the
              IP-named row.

Deleting a device removes only the DCIM row. Job history survives (device_id is
ON DELETE SET NULL, with the device name denormalized alongside it), and stored
captures on disk under <storage>/<site>/<device>/ are left untouched — purge
those separately if you want them gone.

Dry-run is the default. Nothing is deleted without --apply.

Requires netlapse_map_model.py and netlapse_import_map.py alongside it.

Usage:
    export NETLAPSE_BASE=http://127.0.0.1:8888

    # see what would go, and why
    python netlapse_prune_ip_devices.py --token ...

    # delete the clear duplicates
    python netlapse_prune_ip_devices.py --token ... --apply

    # include rows that are the only record of a device
    python netlapse_prune_ip_devices.py --token ... --include-sole-records --apply
"""

import argparse
import getpass
import ipaddress
import os
import sys

try:
    from netlapse_map_model import is_ip_literal
    from netlapse_import_map import KIND_SITES, Netlapse, index_reference, rows
except ImportError as e:
    sys.exit(f"netlapse_map_model.py and netlapse_import_map.py must sit alongside "
             f"this script ({e})")


def classify(devices):
    """Split devices into (ip_named, keepers) and tag why each one is deletable."""
    by_ip = {}
    for dev in devices:
        ip = (dev.get("primary_ip4") or "").strip()
        if ip:
            by_ip.setdefault(ip, []).append(dev)

    ip_named = []
    for dev in devices:
        name = str(dev.get("name") or "")
        if not is_ip_literal(name):
            continue
        ip = (dev.get("primary_ip4") or "").strip()
        siblings = [d for d in by_ip.get(ip, [])
                    if d["id"] != dev["id"] and not is_ip_literal(str(d.get("name") or ""))]
        collected = bool(dev.get("last_collected_at"))
        ip_named.append({
            "device": dev,
            "name": name,
            "ip": ip or "-",
            "site": dev.get("site_slug") or "-",
            "siblings": [str(d.get("name")) for d in siblings],
            "collected": collected,
            "last_status": dev.get("last_collection_status") or "-",
        })
    return ip_named


def main():
    ap = argparse.ArgumentParser(
        description="Delete Netlapse devices named after a bare IP address")
    ap.add_argument("--base", default=os.environ.get("NETLAPSE_BASE", "http://127.0.0.1:8888"))
    ap.add_argument("-u", "--user", default=os.environ.get("NETLAPSE_USER"))
    ap.add_argument("-p", "--password", default=os.environ.get("NETLAPSE_PASS"))
    ap.add_argument("--token", help="API token (Bearer) — skips session login")
    ap.add_argument("--insecure", action="store_true",
                    help="Skip TLS certificate verification (self-signed lab certs)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete. Without it this is a dry run.")
    ap.add_argument("--site", help="Only consider devices in this site slug")
    ap.add_argument("--subnet", action="append", default=[], metavar="CIDR",
                    help="Only consider devices whose address is in CIDR "
                         "(repeatable). Use this to scope a forced delete to a "
                         "range you know is not collectable — a server subnet, "
                         "say — instead of forcing fleet-wide.")
    ap.add_argument("--include-sole-records", action="store_true",
                    help="Also delete IP-named rows that are the only record of that address")
    ap.add_argument("--include-collected", action="store_true",
                    help="Also delete IP-named rows that have collection history")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="Delete at most N devices")
    ap.add_argument("--prune-empty-sites", action="store_true",
                    help="Afterward, delete sites left with no devices")
    args = ap.parse_args()

    client = Netlapse(args.base, verify=not args.insecure)
    if args.token:
        client.token = args.token
    else:
        user = args.user or input("username: ")
        password = args.password or getpass.getpass("password: ")
        client.login(user, password)

    nets = []
    for cidr in args.subnet:
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError as e:
            ap.error(f"--subnet {cidr!r}: {e}")

    def in_scope_subnet(dev):
        if not nets:
            return True
        ip = (dev.get("primary_ip4") or "").strip()
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in nets)

    payload = client.get_json("/api/v1/devices")
    devices = [r for r in rows(payload) if isinstance(r, dict) and r.get("id") is not None]
    if args.site:
        devices = [d for d in devices if (d.get("site_slug") or "") == args.site]
    print(f"{len(devices)} device(s) in scope")

    ip_named = classify(devices)
    if nets:
        before = len(ip_named)
        ip_named = [r for r in ip_named if in_scope_subnet(r["device"])]
        print(f"subnet scope: {len(ip_named)} of {before} IP-named device(s) "
              f"in {', '.join(str(n) for n in nets)}")
    if not ip_named:
        print("no devices are named after an IP address — nothing to do")
        return

    deletable, held = [], []
    for row in ip_named:
        if row["collected"] and not args.include_collected:
            held.append((row, f"has collection history (last: {row['last_status']})"))
        elif not row["siblings"] and not args.include_sole_records:
            held.append((row, "sole record of this address"))
        else:
            deletable.append(row)

    verb = "DELETE" if args.apply else "would delete"
    print(f"\n{len(ip_named)} IP-named device(s): "
          f"{len(deletable)} to delete, {len(held)} held back\n")

    if deletable:
        print("--- deletable ---")
        for row in sorted(deletable, key=lambda r: r["name"]):
            why = (f"duplicate of {', '.join(row['siblings'])}"
                   if row["siblings"] else "sole record (forced)")
            print(f"  {verb} {row['name']:<20} site={row['site']:<10} {why}")

    if held:
        print("\n--- held back ---")
        for row, why in sorted(held, key=lambda r: r[0]["name"]):
            print(f"  keep    {row['name']:<20} site={row['site']:<10} {why}")

    if not args.apply:
        print(f"\ndry run — {len(deletable)} device(s) would be deleted")
        sole = [r for r, why in held if why.startswith("sole record")]
        if sole and not args.include_sole_records:
            print("\nTo delete the sole-record rows above, scope them to a range "
                  "you know is not collectable and force within it, e.g.:")
            print("  --subnet <cidr> --include-sole-records --apply")
        return
    if not deletable:
        print("\nnothing to delete")
        return

    ok = fail = 0
    for row in sorted(deletable, key=lambda r: r["name"]):
        if args.limit and ok >= args.limit:
            print(f"  stopped at --limit {args.limit}")
            break
        did = row["device"]["id"]
        code, resp = client.request("DELETE", f"/api/v1/devices/{did}")
        if code in (200, 204):
            ok += 1
        else:
            fail += 1
            print(f"  FAILED {row['name']}: HTTP {code} {resp[:160]}", file=sys.stderr)
    print(f"\ndeleted {ok} device(s), {fail} failure(s)")
    print("stored captures on disk were NOT removed — purge those separately.")

    if args.prune_empty_sites:
        print("\n--- empty sites ---")
        sites_idx = index_reference(client, KIND_SITES)
        for site in rows(client.get_json("/api/v1/sites")):
            if not isinstance(site, dict):
                continue
            count = site.get("device_count", site.get("devices", site.get("count")))
            if count not in (0, "0"):
                continue
            sid, sname = site.get("id"), site.get("name")
            if sid is None:
                continue
            code, resp = client.request("DELETE", f"/api/v1/admin/dcim/{KIND_SITES}/{sid}")
            print(f"  deleted site {sname}" if code in (200, 204)
                  else f"  FAILED delete {sname}: HTTP {code} {resp[:160]}")

    print("\nRe-running `sync-map` will recreate these rows: parse_hostname() passes "
          "an IPv4 key through unchanged. Exclude them with a repeated "
          "--exclude-prefix on the leading octets, or fix the names at discovery.")


if __name__ == "__main__":
    main()