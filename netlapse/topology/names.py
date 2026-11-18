"""
Interface keys and neighbor-name resolution.

Interface names
    Two different jobs, two functions. display_interface() produces the short
    form SC2 shows on edge labels (GigabitEthernet0/1 → Gi0/1, Ethernet1 →
    Eth1). interface_key() produces a comparison key that lets Arista's
    "Ethernet1" (local) match the "Et1" a Cisco neighbor reports for it, and
    drops a Junos ".0" logical unit so "xe-0/0/1.0" matches "xe-0/0/1".

Device names
    LLDP carries the neighbor's own sysName, which is often the FQDN
    (edge1-01.fra1.example.net) while DCIM holds a shorter name
    (edge1-01.fra1). NameResolver strips labels from the right until it hits a
    DCIM name, so the most specific match wins before the bare short name is
    tried. A candidate that maps to more than one device is ambiguous and is
    only accepted when exactly one of them is in the claiming device's site.
    The management address is the last resort, matched against primary_ip4,
    primary_ip6 and oob_ip.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

# Longest first: "tengigabitethernet" must win over "gigabitethernet",
# "ethernet" must not eat "fastethernet".
_KEY_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("hundredgigabitethernet", "hu"),
    ("fourhundredgige", "fh"),
    ("twohundredgige", "th"),
    ("hundredgige", "hu"),
    ("fiftygigabitethernet", "fi"),
    ("fortygigabitethernet", "fo"),
    ("twentyfivegigabitethernet", "twe"),
    ("twentyfivegige", "twe"),
    ("tengigabitethernet", "te"),
    ("fivegigabitethernet", "fiv"),
    ("twogigabitethernet", "tw"),
    ("gigabitethernet", "gi"),
    ("fastethernet", "fa"),
    ("port-channel", "po"),
    ("portchannel", "po"),
    ("management", "ma"),
    ("ethernet", "et"),
    ("tengig", "te"),
    ("fortygig", "fo"),
    ("hundredgig", "hu"),
    ("gige", "gi"),
    ("eth", "et"),
    ("gig", "gi"),
    ("ten", "te"),
    ("mgmt", "ma"),
)

_DISPLAY_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("TenGigabitEthernet", "Te"),
    ("TwentyFiveGigE", "Twe"),
    ("FortyGigabitEthernet", "Fo"),
    ("HundredGigE", "Hu"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("Port-Channel", "Po"),
    ("Port-channel", "Po"),
    ("Ethernet", "Eth"),
)

# Junos physical name with a logical unit: xe-0/0/1.0, et-0/0/0:1.0, ae0.0
_JUNOS_UNIT = re.compile(r"^((?:[a-z]{2,3}-\d+/\d+/\d+(?::\d+)?)|(?:ae\d+)|(?:reth\d+))\.\d+$")


def display_interface(name: str) -> str:
    """Short display form, as SC2 renders edge labels."""
    s = (name or "").strip()
    for long, short in _DISPLAY_PREFIXES:
        if s.startswith(long):
            s = short + s[len(long):]
            break
    s = re.sub(r"^Et(\d)", r"Eth\1", s)
    return s


def interface_key(name: str) -> str:
    """Vendor-agnostic comparison key. Not for display."""
    s = (name or "").strip().lower().replace(" ", "")
    if not s:
        return ""
    m = _JUNOS_UNIT.match(s)
    if m:
        s = m.group(1)
    for long, short in _KEY_PREFIXES:
        if s.startswith(long) and len(s) > len(long) and (s[len(long)].isdigit() or s[len(long)] == "/"):
            return short + s[len(long):]
    return s


def short_host(name: str) -> str:
    return (name or "").strip().split(".")[0].lower()


def _right_strips(name: str) -> List[str]:
    """edge1-01.fra1.example.net → [edge1-01.fra1.example.net,
    edge1-01.fra1.example, edge1-01.fra1, edge1-01]"""
    parts = (name or "").strip().lower().rstrip(".").split(".")
    return [".".join(parts[:i]) for i in range(len(parts), 0, -1) if parts[:i]]


class NameResolver:
    """Map an LLDP-advertised name (and optional address) to a DCIM device.

    ``devices`` are v_device_detail rows. resolve_ex() also says how the
    match was made, because the weak kinds are the ones that go wrong:

      name   the advertised name, or a right-strip of it, is a full DCIM name
      short  only the first label matched a device's first label
      ip     matched the management address

    A short name like "edge01" or an OOB address reused per site can land on
    a device in another site. The builder only keeps a weak match that
    crosses sites when the far end confirms it.
    """

    def __init__(self, devices: Iterable[dict]):
        self._by_name: Dict[str, List[dict]] = {}
        self._by_ip: Dict[str, List[dict]] = {}
        self._node_ids: Dict[int, str] = {}

        devs = list(devices)
        name_count: Dict[str, int] = {}
        for d in devs:
            name_count[d["name"].lower()] = name_count.get(d["name"].lower(), 0) + 1

        for d in devs:
            nm = d["name"].lower()
            # A DCIM name reused across sites becomes name@site so node ids
            # stay unique; storage is already keyed by (site, name).
            nid = d["name"] if name_count[nm] == 1 else f"{d['name']}@{d.get('site_slug') or ''}"
            self._node_ids[d["id"]] = nid
            for k in {nm, short_host(nm)}:
                bucket = self._by_name.setdefault(k, [])
                if d not in bucket:
                    bucket.append(d)
            for f in ("primary_ip4", "primary_ip6", "oob_ip"):
                ip = (d.get(f) or "").split("/")[0].strip().lower()
                if ip:
                    bucket = self._by_ip.setdefault(ip, [])
                    if d not in bucket:
                        bucket.append(d)

    def node_id_for(self, device: dict) -> str:
        return self._node_ids[device["id"]]

    @staticmethod
    def _pick(hits: List[dict], from_site: Optional[str], exclude_id=None) -> Optional[dict]:
        # A device never resolves a neighbor to itself: "agg1" heard on
        # agg1.iad1 means the other agg1.
        hits = [h for h in hits if h["id"] != exclude_id]
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]
        same_site = [h for h in hits if from_site and h.get("site_slug") == from_site]
        return same_site[0] if len(same_site) == 1 else None

    def resolve_ex(self, name: str, ip: str = "", from_site: Optional[str] = None,
                   exclude_id=None) -> Tuple[Optional[dict], str]:
        for cand in _right_strips(name):
            hits = self._by_name.get(cand)
            if not hits:
                continue
            hit = self._pick(hits, from_site, exclude_id)
            if hit is None:
                # Ambiguous at this specificity: a shorter strip can only be
                # more ambiguous, so stop here and fall through to the address.
                break
            return hit, ("name" if hit["name"].lower() == cand else "short")
        ip = (ip or "").split("/")[0].strip().lower()
        if ip and ip in self._by_ip:
            hit = self._pick(self._by_ip[ip], from_site, exclude_id)
            if hit is not None:
                return hit, "ip"
        return None, ""

    def resolve(self, name: str, ip: str = "", from_site: Optional[str] = None) -> Optional[dict]:
        return self.resolve_ex(name, ip, from_site)[0]
