#!/usr/bin/env python3
"""
scripts/netlapse_map_model.py — map parsing shared by the Netlapse importer and
the repair tool.

Holds the three things both tools need to agree on: how a topology map is
flattened into device records, how a device's real name is recovered when the
map keys a node by IP address, and how a prose platform string is classified
into the netmiko-style platform slugs Netlapse keeps in reference data.

Platform classification
    Discovery writes node_details.platform as prose — either the raw SSH
    sys_descr, or a summary like "Cisco IOSv IOS 15.6(2)T" / "Arista vEOS-lab
    EOS 4.33.1F". It is never a platform slug, so an exact-match lookup against
    reference data finds nothing. classify_platform() reads the richer fields
    the map carries alongside it (vendor, model, os_version, metadata.sys_descr)
    and returns a slug plus a confidence, so low-confidence guesses can be shown
    for review instead of applied silently.

Name recovery
    A node keyed by IP address has no site label to parse. The map usually knows
    better: metadata.fqdn and metadata.sys_name carry the device's own idea of
    its name. Reverse DNS is the last resort, not the first.

Stdlib only.
"""

import ipaddress
import re
import socket

CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

# Platform slugs present in Netlapse reference data. Keep in sync with
# GET /api/v1/admin/dcim/platforms.
KNOWN_SLUGS = (
    "arista_eos", "cisco_asa", "cisco_ios", "cisco_ios_xe", "cisco_ios_xr",
    "cisco_nxos", "dell_os10", "f5_tmos", "fortinet_fortios", "hp_comware",
    "hp_procurve", "juniper_junos", "paloalto_panos",
)

# (haystack keyword, slug, confidence). Evaluated in order — most specific
# first, because a bare "ios" substring also appears in IOS-XE and NX-OS
# descriptions, and "hp" appears inside "hp comware".
_RULES = (
    # Cisco — order is load-bearing
    ("nx-os", "cisco_nxos", "high"),
    ("nxos", "cisco_nxos", "high"),
    ("nexus", "cisco_nxos", "high"),
    ("ios-xe", "cisco_ios_xe", "high"),
    ("ios xe", "cisco_ios_xe", "high"),
    ("iosxe", "cisco_ios_xe", "high"),
    ("ios-xr", "cisco_ios_xr", "high"),
    ("ios xr", "cisco_ios_xr", "high"),
    ("iosxr", "cisco_ios_xr", "high"),
    ("adaptive security appliance", "cisco_asa", "high"),
    ("cisco asa", "cisco_asa", "high"),
    ("cisco ios", "cisco_ios", "high"),
    # Arista
    ("arista", "arista_eos", "high"),
    (" eos ", "arista_eos", "high"),
    # Juniper
    ("junos", "juniper_junos", "high"),
    ("juniper", "juniper_junos", "high"),
    # Palo Alto
    ("pan-os", "paloalto_panos", "high"),
    ("palo alto", "paloalto_panos", "high"),
    ("paloalto", "paloalto_panos", "high"),
    # Fortinet
    ("fortigate", "fortinet_fortios", "high"),
    ("fortios", "fortinet_fortios", "high"),
    ("fortinet", "fortinet_fortios", "high"),
    # HP — comware before any generic HP match
    ("comware", "hp_comware", "high"),
    ("h3c", "hp_comware", "high"),
    ("procurve", "hp_procurve", "high"),
    ("aruba", "hp_procurve", "medium"),
    # Dell
    ("os10", "dell_os10", "high"),
    ("dell", "dell_os10", "medium"),
    # F5
    ("big-ip", "f5_tmos", "high"),
    ("tmos", "f5_tmos", "high"),
    ("f5 networks", "f5_tmos", "high"),
)

# Fallbacks when only a coarse vendor is known and no product keyword matched.
# HP is deliberately absent: comware and procurve are different platforms and
# guessing between them is worse than reporting the device as unresolved.
_VENDOR_FALLBACK = {
    "cisco": ("cisco_ios", "low"),
    "arista": ("arista_eos", "high"),
    "juniper": ("juniper_junos", "high"),
    "paloalto": ("paloalto_panos", "high"),
    "fortinet": ("fortinet_fortios", "high"),
}


def classify_platform(record):
    """Return (slug, confidence, evidence) or (None, 'none', reason).

    `record` is a normalized device dict from extract_devices(). Confidence is
    'high' when a product keyword matched, 'medium'/'low' when the answer rests
    on a vendor fallback or an ambiguous keyword.
    """
    parts = [
        record.get("platform") or "",
        record.get("sys_descr") or "",
        record.get("model") or "",
        record.get("os_version") or "",
    ]
    haystack = " ".join(p for p in parts if p).lower()
    # Pad so " eos " style boundary rules can match at the ends.
    padded = f" {haystack} "

    for keyword, slug, confidence in _RULES:
        if keyword in padded:
            return slug, confidence, keyword

    vendor = (record.get("vendor") or "").strip().lower()
    if vendor in _VENDOR_FALLBACK:
        slug, confidence = _VENDOR_FALLBACK[vendor]
        return slug, confidence, f"vendor={vendor}"
    if vendor == "hp":
        return None, "none", "vendor hp is ambiguous (comware vs procurve)"
    if not haystack.strip():
        return None, "none", "no platform data in map"
    return None, "none", f"unrecognized: {haystack[:60]}"


# Mirrors netlapse's own parse_hostname() in dcim/map_importer.py. The built-in
# importer stores {shortname}.{site} with the domain stripped, so a device it
# created and the same device named by FQDN are different strings for the same
# box. Matching this convention is what keeps the two importers idempotent
# against each other.
_DOT_SITE = re.compile(r"^(?P<shortname>[^.]+)\.(?P<site>[a-z]{2,5}\d{1,2})(?:\.|$)")
_DASH_SITE = re.compile(r"^(?P<site>[a-z]{2,5})-(?P<rest>.+)$", re.IGNORECASE)


def netlapse_name(fqdn):
    """Return the device name netlapse's own importer would store."""
    if not fqdn:
        return ""
    if is_ip_literal(fqdn):
        return fqdn
    m = _DOT_SITE.match(fqdn)
    if m:
        return f"{m.group('shortname')}.{m.group('site')}"
    return fqdn.split(".")[0]


def name_handles(name, ip=None):
    """Every string this device might already be stored under.

    A device created by the built-in importer, by an earlier run of this one,
    or by hand can be under any of these. Checking all of them before creating
    is what prevents a second row for a box that is already present.
    """
    out = set()
    for candidate in (name, netlapse_name(name), (name or "").split(".")[0], ip):
        if candidate:
            out.add(str(candidate).strip().lower())
    return out


def is_ip_literal(value):
    try:
        ipaddress.ip_address(str(value).strip())
        return True
    except ValueError:
        return False


def resolve_name(record, allow_dns=True):
    """Recover a usable device name. Returns (name, source).

    Prefers the map's own idea of the device name over reverse DNS, and never
    returns an IP literal as a name.
    """
    key = (record.get("key") or "").strip().rstrip(".")
    if key and not is_ip_literal(key):
        return key, "map-key"

    for field, label in (("fqdn", "map-fqdn"), ("sys_name", "map-sys_name")):
        value = (record.get(field) or "").strip().rstrip(".")
        if value and not is_ip_literal(value):
            return value, label

    ip = (record.get("ip") or "").strip()
    if allow_dns and ip:
        try:
            host = socket.gethostbyaddr(ip)[0].strip().rstrip(".")
        except (socket.herror, socket.gaierror, OSError):
            host = ""
        if host and not is_ip_literal(host):
            return host, "reverse-dns"

    return key or ip or "", "unresolved"


def resolve_management_ip(fqdn, ip, allow_dns=True):
    """Return (ip, note). Addresses inside 100.64.0.0/10 are re-resolved.

    A CGNAT address is not reachable for collection from the Netlapse host, so
    the device name is resolved forward and the answer used instead. The
    original address is preserved in the note for the audit trail.
    """
    if not ip:
        if not (allow_dns and fqdn):
            return None, "no management address"
        try:
            return socket.gethostbyname(fqdn), "resolved-from-dns"
        except socket.gaierror:
            return None, "no-ip-no-dns"
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip, "unparsable-ip"
    if addr.version == 4 and addr in CGNAT_NET:
        if not allow_dns:
            return ip, "cgnat-dns-disabled"
        if not fqdn or is_ip_literal(fqdn):
            return ip, "cgnat-no-name-to-resolve"
        try:
            resolved = socket.gethostbyname(fqdn)
        except socket.gaierror:
            return ip, "cgnat-dns-failed"
        if resolved == ip:
            return ip, "cgnat-dns-agrees"
        return resolved, f"cgnat-replaced ({ip})"
    return ip, ""


def extract_devices(doc, include_peers=False):
    """Normalize a map document to a list of device records.

    Each record: {key, ip, platform, vendor, model, os_version, sys_descr,
    fqdn, sys_name, direct}.

    Handles the secure_cartography shape
    ({node: {node_details: {...}, peers: {...}}}), a flat list of dicts, and a
    plain {name: ip} mapping.
    """
    found = {}

    def add(key, details, direct):
        if not key:
            return
        key = str(key).strip().rstrip(".")
        meta = details.get("metadata") if isinstance(details.get("metadata"), dict) else {}
        record = {
            "key": key,
            "ip": (details.get("ip") or details.get("primary_ip4")
                   or details.get("mgmt_ip") or "").strip() or None,
            "platform": (details.get("platform") or details.get("os")
                         or details.get("device_type") or "").strip() or None,
            "vendor": (details.get("vendor") or "").strip() or None,
            "model": (details.get("model") or "").strip() or None,
            "os_version": (details.get("os_version") or "").strip() or None,
            "sys_descr": (meta.get("sys_descr") or "").strip() or None,
            "fqdn": (meta.get("fqdn") or "").strip() or None,
            "sys_name": (meta.get("sys_name") or "").strip() or None,
            "direct": direct,
        }
        prior = found.get(key.lower())
        if prior:
            # Keep the directly-polled record; fill its gaps from the peer entry.
            winner, loser = (prior, record) if prior["direct"] or not direct else (record, prior)
            for field, value in loser.items():
                if field != "direct" and not winner.get(field):
                    winner[field] = value
            found[key.lower()] = winner
            return
        found[key.lower()] = record

    if isinstance(doc, list):
        for entry in doc:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("hostname") or entry.get("device")
                add(name, entry, True)
    elif isinstance(doc, dict):
        for key, entry in doc.items():
            if isinstance(entry, str):
                add(key, {"ip": entry}, True)
                continue
            if not isinstance(entry, dict):
                continue
            details = entry.get("node_details") or entry.get("details") or {}
            merged = dict(entry)
            merged.pop("peers", None)
            merged.update(details)
            add(key, merged, True)
            if include_peers:
                for peer_key, peer in (entry.get("peers") or {}).items():
                    add(peer_key, peer if isinstance(peer, dict) else {}, False)

    return sorted(found.values(), key=lambda d: d["key"].lower())


def site_for(name, label_index, site_map, default_site):
    """Derive a site slug from an FQDN label. IP literals never yield a site."""
    if not name or is_ip_literal(name):
        return default_site
    labels = name.lower().split(".")
    slug = labels[label_index] if len(labels) > label_index else None
    slug = slug or default_site
    if not slug:
        return None
    return site_map.get(slug, slug)