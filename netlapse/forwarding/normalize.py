"""
Route and address normalization.

Route templates disagree on everything: field names, one row per route vs one
row per next hop, how a connected route is spelled. Everything below turns a
parsed route capture into one shape the model can do longest-prefix match on:

    Route = (net, plen, kind, proto, hops)
      net    IPv4 network as int (masked)
      plen   prefix length
      kind   fwd | conn | local | drop | overlay | unk
      proto  protocol as the device printed it ("B I", "OSPF", "ospf-1")
      hops   tuple of (next_hop_ip or None, interface)

Families are recognised by their fields, not by template name, so a renamed or
forked template keeps working as long as its columns look the same:

    junos      ACTIVE / NH_TYPE / TABLE           (fixed juniper_junos_show_route)
    eos        PREFIX + NEXT_HOP list              (fixed arista_eos_show_ip_route_v2)
               NETWORK + PREFIX_LENGTH + NEXT_HOP  (arista_eos_show_ip_route)
    cisco      NEXTHOP_IP / NEXTHOP_IF, one row per next hop, NETWORK filled down
               (cisco_ios_show_ip_route, cisco_nxos_show_ip_route)

A route capture from the original juniper_junos_show_route (no next hops) is
reported as unusable rather than silently modeled as "no route".

IPv4 only. All arithmetic is on ints: ipaddress is too slow for 300k-route
edge tables.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

Route = Tuple[int, int, str, str, Tuple[Tuple[Optional[str], str], ...]]

MASKS = [((0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF) for p in range(33)]
_IPV4 = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
_OVERLAY_IF = re.compile(r"^(vxlan|vtep|nve|tunnel|gr-|ip-|lt-)", re.I)
_NULL_IF = re.compile(r"^null", re.I)

# Addresses that never identify a device on the data plane.
NOT_OWNED_PREFIXES = ("127.", "0.", "128.0.", "169.254.")


# ── IPv4 helpers ────────────────────────────────────────────────────────────

def ip2int(s: str) -> int:
    a, b, c, d = s.split(".")
    return (int(a) << 24) | (int(b) << 16) | (int(c) << 8) | int(d)


def int2ip(n: int) -> str:
    return f"{(n >> 24) & 255}.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


def parse_prefix(s: Optional[str]) -> Optional[Tuple[int, int]]:
    """'10.7.22.0/24' -> (net_int, plen). None if it isn't an IPv4 prefix."""
    if not s or "/" not in s:
        return None
    ip, plen = s.strip().split("/", 1)
    try:
        p = int(plen)
        if not 0 <= p <= 32:
            return None
        return ip2int(ip) & MASKS[p], p
    except (ValueError, IndexError):
        return None


def fmt_prefix(net: int, plen: int) -> str:
    return f"{int2ip(net)}/{plen}"


def as_list(v) -> List[str]:
    if v is None or v == "":
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x not in (None, "")]
    return [str(v)]


def _scalar(v) -> str:
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return "" if v is None else str(v)


# ── routes ──────────────────────────────────────────────────────────────────

def family(record: dict) -> str:
    if "ACTIVE" in record or "NH_TYPE" in record or "TABLE" in record:
        return "junos"
    if "NEXTHOP_IP" in record or "NEXTHOP_IF" in record:
        return "cisco"
    if "NEXT_HOP" in record and ("PREFIX" in record or "NETWORK" in record):
        return "eos"
    if "PREFIX" in record and "PREFERENCE" in record:
        return "junos_no_nh"
    return "unknown"


def _junos_vrf(table: str) -> Optional[str]:
    """inet.0 -> default, BLUE.inet.0 -> BLUE, anything else (inet6, mpls) -> None."""
    t = (table or "inet.0").strip()
    if t == "inet.0":
        return "default"
    if t.endswith(".inet.0"):
        return t[: -len(".inet.0")]
    return None


def _kind_from_hops(hops, default_kind="fwd") -> str:
    ifs = [i for _, i in hops if i]
    if ifs and all(_NULL_IF.match(i) for i in ifs):
        return "drop"
    if ifs and all(_OVERLAY_IF.match(i) for i in ifs):
        return "overlay"
    return default_kind


def _junos(rec: dict, fib_only: bool) -> Optional[Tuple[str, Route]]:
    vrf = _junos_vrf(_scalar(rec.get("TABLE")))
    pp = parse_prefix(_scalar(rec.get("PREFIX")))
    if vrf is None or pp is None:
        return None
    net, plen = pp
    proto = _scalar(rec.get("PROTOCOL"))
    nh_type = _scalar(rec.get("NH_TYPE"))
    nhs = as_list(rec.get("NEXT_HOP"))
    ifs = as_list(rec.get("INTERFACE"))
    if nh_type in ("Reject", "Discard"):
        return vrf, (net, plen, "drop", proto, ())
    if nh_type in ("Local", "Receive") or proto == "Local":
        return vrf, (net, plen, "local", proto, tuple((None, i) for i in ifs))
    if proto == "Direct":
        return vrf, (net, plen, "conn", proto, tuple((None, i) for i in ifs))
    if nhs:
        raw_sel = rec.get("SELECTED") if isinstance(rec.get("SELECTED"), list) else []
        pairs = list(zip(nhs, ifs + [""] * (len(nhs) - len(ifs))))
        if fib_only and len(raw_sel) == len(nhs) and any(s == ">" for s in raw_sel):
            pairs = [p for p, s in zip(pairs, raw_sel) if s == ">"]
        hops = tuple((h, i) for h, i in pairs)
        return vrf, (net, plen, _kind_from_hops(hops), proto, hops)
    if ifs:
        hops = tuple((None, i) for i in ifs)
        return vrf, (net, plen, _kind_from_hops(hops, "unk"), proto, hops)
    return vrf, (net, plen, "unk", proto, ())


def _eos(rec: dict) -> Optional[Tuple[str, Route]]:
    vrf = _scalar(rec.get("VRF")) or "default"
    prefix = _scalar(rec.get("PREFIX"))
    if not prefix and rec.get("NETWORK") not in (None, ""):
        prefix = f"{_scalar(rec.get('NETWORK'))}/{_scalar(rec.get('PREFIX_LENGTH'))}"
    pp = parse_prefix(prefix)
    if pp is None:
        return None
    net, plen = pp
    proto = _scalar(rec.get("PROTOCOL")).strip()
    nhs = as_list(rec.get("NEXT_HOP"))
    ifs = as_list(rec.get("INTERFACE"))
    if "connected" in nhs or _scalar(rec.get("DIRECT")) == "directly":
        hops = tuple((None, i) for i in ifs)
        return vrf, (net, plen, "drop" if _kind_from_hops(hops) == "drop" else "conn", proto, hops)
    if nhs:
        pairs = list(zip(nhs, ifs + [""] * (len(nhs) - len(ifs))))
        hops = tuple((h, i) for h, i in pairs)
        return vrf, (net, plen, _kind_from_hops(hops), proto, hops)
    if ifs:
        hops = tuple((None, i) for i in ifs)
        return vrf, (net, plen, _kind_from_hops(hops, "unk"), proto, hops)
    return vrf, (net, plen, "unk", proto, ())


_CISCO_CONN = {"C", "direct"}
_CISCO_LOCAL = {"L", "local", "hsrp", "vrrp", "glbp"}


def _cisco_rows(records: Iterable[dict]) -> Iterable[Tuple[str, Route]]:
    """One row per next hop -> one route per (vrf, prefix)."""
    grouped: Dict[Tuple[str, int, int], dict] = {}
    order = []
    for rec in records:
        vrf = _scalar(rec.get("VRF")) or "default"
        net_s, plen_s = _scalar(rec.get("NETWORK")), _scalar(rec.get("PREFIX_LENGTH"))
        if not plen_s:
            plen_s = "32"
        pp = parse_prefix(f"{net_s}/{plen_s}")
        if pp is None:
            continue
        key = (vrf, pp[0], pp[1])
        g = grouped.get(key)
        if g is None:
            g = grouped[key] = {"proto": _scalar(rec.get("PROTOCOL")), "hops": []}
            order.append(key)
        ip = _scalar(rec.get("NEXTHOP_IP")) or None
        ifn = _scalar(rec.get("NEXTHOP_IF"))
        if ip or ifn:
            g["hops"].append((ip, ifn))
    for vrf, net, plen in order:
        g = grouped[(vrf, net, plen)]
        proto = g["proto"]
        hops = tuple(dict.fromkeys(g["hops"]))
        base = proto.split("-")[0] if proto else ""
        if base in _CISCO_CONN:
            kind = "conn"
        elif base in _CISCO_LOCAL:
            kind = "local"
        elif any(h for h, _ in hops):
            kind = _kind_from_hops(hops)
        else:
            kind = _kind_from_hops(hops, "unk")
        yield vrf, (net, plen, kind, proto, hops)


def normalize_routes(records: Optional[List[dict]], fib_only: bool = True) -> Tuple[Dict[str, List[Route]], dict]:
    """
    Parsed route records -> ({vrf: [Route]}, info).

    info: {"family", "records", "usable", "problem"} — problem is set when the
    capture can't be modeled (template without next hops, unknown columns).
    """
    records = records or []
    out: Dict[str, List[Route]] = {}
    info = {"family": "none", "records": len(records), "usable": 0, "problem": ""}
    if not records:
        info["problem"] = "no route capture"
        return out, info
    fam = family(records[0])
    info["family"] = fam
    if fam == "junos_no_nh":
        info["problem"] = "route template has no next hops (apply the fixed juniper_junos_show_route and reparse)"
        return out, info
    if fam == "unknown":
        info["problem"] = f"unrecognised route columns: {sorted(records[0])[:8]}"
        return out, info
    if fam == "cisco":
        rows = _cisco_rows(records)
    else:
        conv = (lambda r: _junos(r, fib_only)) if fam == "junos" else _eos
        rows = (conv(r) for r in records)
    for item in rows:
        if item is None:
            continue
        vrf, route = item
        out.setdefault(vrf, []).append(route)
        info["usable"] += 1
    # The pre-fix v2 template (PREFIX column) dropped every connected route; a
    # device always has some, so none at all means the old parse.
    if (fam == "eos" and "PREFIX" in records[0] and info["usable"]
            and not any(r[2] == "conn" for v in out.values() for r in v)):
        info["problem"] = ("no connected routes parsed (old arista_eos_show_ip_route_v2? "
                           "apply the fixed template and reparse)")
    return out, info


# ── addresses ───────────────────────────────────────────────────────────────

_ADDR_FIELDS = ("IP_ADDRESS", "LOCAL", "IPADDR", "IP_ADDR", "PRIMARY_IP")


def owned_from_records(records: Optional[List[dict]]) -> List[str]:
    """Interface addresses from show ip interface brief / interfaces terse / interfaces."""
    owned = set()
    for r in records or []:
        for key in _ADDR_FIELDS:
            for v in as_list(r.get(key)):
                for ip in _IPV4.findall(v):
                    if not ip.startswith(NOT_OWNED_PREFIXES):
                        owned.add(ip)
    return sorted(owned)


def owned_from_routes(routes: Iterable[Route]) -> List[str]:
    """Host routes a device holds for itself: Junos Local /32, EOS/IOS connected /32 (loopbacks)."""
    owned = set()
    for net, plen, kind, proto, hops in routes:
        if plen == 32 and kind in ("local", "conn"):
            ip = int2ip(net)
            if not ip.startswith(NOT_OWNED_PREFIXES):
                owned.add(ip)
    return sorted(owned)
