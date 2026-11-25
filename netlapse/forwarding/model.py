"""
Forwarding model: what every device would do with a packet, from stored captures.

Built from DCIM devices plus a CaptureReader, so it works on "latest" or on any
point in time the storage backend can reconstruct (as_of).

Loading happens in two passes to keep memory flat:

  1. Endpoints: full route tables. Their connected subnets give the
     destinations.
  2. Everyone else: only routes that could be the longest match for one of the
     destinations (every (plen, masked dest) pair goes in a cover set; a route
     is kept if it's in the set), plus host routes the device owns. A 300k-route
     edge becomes a few dozen routes.

A device's next hop becomes a device by, in order:
  ip        the next-hop address is owned by exactly one other device
  ip+lldp   owned by several; the LLDP neighbor on the egress port picks one
  lldp      not owned by anyone known; the LLDP neighbor on the egress port
Anything else is an exit: the packet leaves the modeled network (transit,
unmanaged gear) or ownership data is missing — the report lists which.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from netlapse.forwarding.config import ForwardingConfig
from netlapse.forwarding.normalize import (
    MASKS, Route, fmt_prefix, int2ip, ip2int, normalize_routes, owned_from_records,
    owned_from_routes, parse_prefix,
)
from netlapse.topology.history import CaptureReader
from netlapse.topology.lldp import normalize_records, records_from_envelope
from netlapse.topology.names import NameResolver, interface_key

logger = logging.getLogger(__name__)


class LPM:
    """Longest-prefix match: one dict per prefix length, longest first."""

    def __init__(self, routes: Iterable[Route]):
        self.by_len: Dict[int, Dict[int, Route]] = defaultdict(dict)
        for r in routes:
            self.by_len[r[1]].setdefault(r[0], r)
        self.lens = sorted(self.by_len, reverse=True)
        self.count = sum(len(v) for v in self.by_len.values())

    def lookup(self, addr: int) -> Optional[Route]:
        for p in self.lens:
            hit = self.by_len[p].get(addr & MASKS[p])
            if hit is not None:
                return hit
        return None


@dataclass
class DeviceState:
    node: str                       # NameResolver node id (name, or name@site if reused)
    device: dict
    endpoint: bool
    lpm: LPM
    owned: Set[str]
    lldp: Dict[str, str]            # interface_key(local port) -> neighbor node
    lldp_unmodeled: Dict[str, str]  # interface_key(local port) -> raw neighbor name
    routes_at: Optional[datetime]
    route_info: dict
    routes_total: int = 0


@dataclass
class Destination:
    endpoint: str       # node id
    subnet: str
    net: int
    plen: int
    host: str
    interface: str
    unique: bool
    candidates: List[str] = field(default_factory=list)


class ForwardingModel:
    def __init__(self, devices: Iterable[dict], reader: CaptureReader, cfg: ForwardingConfig,
                 sites: Optional[Iterable[str]] = None,
                 progress: Optional[Callable[[str], None]] = None,
                 extra_destinations: Optional[Iterable[str]] = None):
        self.cfg = cfg
        self.reader = reader
        self.sites = sorted(set(sites)) if sites else []
        self.progress = progress or (lambda msg: None)
        # Addresses a trace will ask about beyond the endpoint subnets; transit
        # tables keep the routes that could match them too.
        self.extra_destinations = [ip for ip in (extra_destinations or []) if ip]

        # The caller chooses the device set (the CLI passes status=active).
        all_devs = list(devices)
        self.resolver = NameResolver(all_devs)
        scope = [d for d in all_devs if not self.sites or d.get("site_slug") in self.sites]
        self.excluded = sorted(d["name"] for d in scope if cfg.is_excluded(d))
        self.devices = [d for d in scope if not cfg.is_excluded(d)]
        self.by_node: Dict[str, DeviceState] = {}
        self.destinations: Dict[str, Destination] = {}
        self.problems: Dict[str, str] = {}
        self.owners: Dict[str, Set[str]] = defaultdict(set)
        self._load()

    # ── loading ──────────────────────────────────────────────────────────

    def _routes(self, d: dict):
        env, when = self.reader.parsed(d["site_slug"], d["name"], self.cfg.routes_capture)
        recs = records_from_envelope(env) if env else []
        by_vrf, info = normalize_routes(recs, fib_only=self.cfg.fib_only)
        return by_vrf.get(self.cfg.vrf, []), info, when, len(recs)

    def _owned_ifaces(self, d: dict) -> Set[str]:
        owned: Set[str] = set()
        for ct in self.cfg.address_captures:
            env, _ = self.reader.parsed(d["site_slug"], d["name"], ct)
            if env:
                owned.update(owned_from_records(records_from_envelope(env)))
        return owned

    def _lldp(self, d: dict) -> Tuple[Dict[str, str], Dict[str, str]]:
        env, _ = self.reader.parsed(d["site_slug"], d["name"], self.cfg.lldp_capture)
        mapped, unmodeled = {}, {}
        if not env:
            return mapped, unmodeled
        for claim in normalize_records(records_from_envelope(env)):
            hit, _how = self.resolver.resolve_ex(claim.remote_name, claim.remote_ip,
                                                 from_site=d.get("site_slug"), exclude_id=d["id"])
            for port in (claim.local_port, claim.parent_port):
                k = interface_key(port)
                if not k:
                    continue
                if hit is not None:
                    mapped.setdefault(k, self.resolver.node_id_for(hit))
                elif claim.remote_name:
                    unmodeled.setdefault(k, claim.remote_name)
        return mapped, unmodeled

    def _add(self, d: dict, routes: List[Route], info: dict, when, total: int, endpoint: bool):
        node = self.resolver.node_id_for(d)
        owned = set(owned_from_routes(routes)) | self._owned_ifaces(d)
        lldp, unmodeled = self._lldp(d)
        st = DeviceState(node=node, device=d, endpoint=endpoint, lpm=LPM(routes), owned=owned,
                         lldp=lldp, lldp_unmodeled=unmodeled, routes_at=when, route_info=info,
                         routes_total=total)
        self.by_node[node] = st
        for ip in owned:
            self.owners[ip].add(node)
        if info.get("problem"):
            self.problems[node] = info["problem"]

    def _load(self):
        cfg = self.cfg
        endpoints = [d for d in self.devices if cfg.is_endpoint(d)]
        others = [d for d in self.devices if not cfg.is_endpoint(d)]
        total = len(self.devices)
        done = 0

        # Pass 1: endpoints, full tables
        endpoint_routes: Dict[str, List[Route]] = {}
        for d in sorted(endpoints, key=lambda x: (x.get("site_slug") or "", x["name"])):
            routes, info, when, n = self._routes(d)
            self._add(d, routes, info, when, n, endpoint=True)
            endpoint_routes[self.resolver.node_id_for(d)] = routes
            done += 1
            self.progress(f"[{done}/{total}] {d['name']}: {n} routes (endpoint)")

        self._pick_destinations(endpoint_routes)

        # Pass 2: transit devices, filtered to routes that can matter
        dest_ints = [ip2int(x.host) for x in self.destinations.values()]
        for extra in self.extra_destinations:
            pp = parse_prefix(extra if "/" in extra else f"{extra}/32")
            if pp:
                dest_ints.append(pp[0] + (1 if pp[1] < 32 else 0))
        cover = {(p, di & MASKS[p]) for di in dest_ints for p in range(33)}
        # Routes more specific than an endpoint subnet and inside it (a /32 to
        # one server, a /25 split) are kept too, so a trace to any address in
        # an endpoint subnet is exact without reloading the tables.
        inside = {(d.plen, d.net) for d in self.destinations.values()}
        inside_lens = sorted({p for p, _ in inside})
        for d in sorted(others, key=lambda x: (x.get("site_slug") or "", x["name"])):
            routes, info, when, n = self._routes(d)
            kept = [r for r in routes if (r[1], r[0]) in cover
                    or any(r[1] > p and (p, r[0] & MASKS[p]) in inside for p in inside_lens)]
            own_host = [r for r in routes if r[1] == 32 and r[2] in ("local", "conn") and (r[1], r[0]) not in cover]
            self._add(d, kept, info, when, n, endpoint=False)
            st = self.by_node[self.resolver.node_id_for(d)]
            for ip in owned_from_routes(own_host):
                st.owned.add(ip)
                self.owners[ip].add(st.node)
            done += 1
            self.progress(f"[{done}/{total}] {d['name']}: {n} routes -> {len(kept)} kept")

    def _pick_destinations(self, endpoint_routes: Dict[str, List[Route]]):
        cfg = self.cfg
        if cfg.dest_source == "list":
            for item in cfg.dest_list:
                dev, pfx = item.get("device"), item.get("prefix")
                node = next((n for n, s in self.by_node.items()
                             if s.device["name"] == dev or n == dev), None)
                pp = parse_prefix(pfx)
                if node and pp:
                    self.destinations[node] = self._dest(node, pp[0], pp[1], item.get("interface", ""), True, [pfx])
            return

        cands: Dict[str, List[Tuple[int, int, str]]] = {}
        for node, routes in endpoint_routes.items():
            c = []
            for net, plen, kind, proto, hops in routes:
                if kind != "conn" or not (cfg.min_plen <= plen <= cfg.max_plen):
                    continue
                ifname = (hops[0][1] if hops else "") or ""
                low = ifname.lower()
                if low.startswith(("loopback", "lo0", "management", "mgmt", "em", "fxp", "me0", "null")):
                    continue
                if cfg.dest_if_re and not cfg.dest_if_re.search(ifname):
                    continue
                if not any((net & MASKS[dp]) == dn and plen >= dp for dn, dp in cfg.dest_within):
                    continue
                c.append((plen, net, ifname))
            cands[node] = sorted(set(c))
        count = defaultdict(int)
        for c in cands.values():
            for plen, net, _ in c:
                count[(net, plen)] += 1
        for node, c in cands.items():
            uniq = [x for x in c if count[(x[1], x[0])] == 1]
            pick = (uniq or c or [None])[0]
            if pick is None:
                continue
            plen, net, ifname = pick
            self.destinations[node] = self._dest(node, net, plen, ifname, bool(uniq),
                                                 [f"{fmt_prefix(n, p)} ({i})" for p, n, i in c])

    def _dest(self, node, net, plen, ifname, unique, cands) -> Destination:
        size = 1 << (32 - plen)
        host = net + min(self.cfg.host_offset, max(size - 2, 0))
        return Destination(endpoint=node, subnet=fmt_prefix(net, plen), net=net, plen=plen,
                           host=int2ip(host), interface=ifname, unique=unique, candidates=cands)

    # ── queries ──────────────────────────────────────────────────────────

    @property
    def endpoints(self) -> List[str]:
        return sorted((n for n, s in self.by_node.items() if s.endpoint),
                      key=lambda n: (self.by_node[n].device.get("site_slug") or "", n))

    def site_of(self, node: str) -> str:
        st = self.by_node.get(node)
        return (st.device.get("site_slug") or "") if st else ""

    def ambiguous_ips(self) -> Dict[str, List[str]]:
        return {ip: sorted(o) for ip, o in self.owners.items() if len(o) > 1}

    def capture_skew(self) -> dict:
        times = {n: s.routes_at for n, s in self.by_node.items() if s.routes_at}
        if not times:
            return {"newest": None, "oldest": None, "stale": []}
        newest = max(times.values())
        limit = self.cfg.stale_minutes * 60
        stale = sorted(((n, int((newest - t).total_seconds() // 60)) for n, t in times.items()
                        if (newest - t).total_seconds() > limit), key=lambda x: -x[1])
        return {"newest": newest.isoformat(timespec="seconds"),
                "oldest": min(times.values()).isoformat(timespec="seconds"),
                "stale": [{"device": n, "minutes_behind": m} for n, m in stale]}

    def resolve(self, node: str, nh_ip: Optional[str], ifname: str) -> Tuple[str, str, str]:
        """Next hop -> (kind 'dev'|'exit', target, how)."""
        st = self.by_node[node]
        k = interface_key(ifname) if ifname else ""
        lldp_node = st.lldp.get(k) if k else None
        if lldp_node is not None and lldp_node not in self.by_node:
            lldp_node = None    # resolved to a DCIM device outside the modeled scope
        if nh_ip:
            own = self.owners.get(nh_ip, set()) - {node}
            if len(own) == 1:
                return "dev", next(iter(own)), "ip"
            if len(own) > 1:
                if lldp_node in own:
                    return "dev", lldp_node, "ip+lldp"
                return "dev", sorted(own)[0], "ip-ambiguous"
        if lldp_node and lldp_node != node:
            return "dev", lldp_node, "lldp"
        if k and st.lldp.get(k):
            return "exit", f"{st.lldp[k]} ({nh_ip or ifname})", "out-of-scope"
        if k and st.lldp_unmodeled.get(k):
            return "exit", f"{st.lldp_unmodeled[k]} ({nh_ip or ifname})", "lldp-unmodeled"
        return "exit", f"{nh_ip or '?'} via {ifname or '?'}", "unresolved"

    def action(self, node: str, dest_int: int, dest_plen: int) -> dict:
        """What `node` does with a packet for dest_int. JSON-ready."""
        st = self.by_node[node]
        r = st.lpm.lookup(dest_int)
        if r is None:
            return {"t": "noroute"}
        net, plen, kind, proto, hops = r
        cls = "default" if plen == 0 else ("specific" if plen >= dest_plen else "aggregate")
        base = {"p": fmt_prefix(net, plen), "c": cls, "proto": proto}
        if kind == "drop":
            return {"t": "blackhole", **base}
        if kind in ("conn", "local"):
            return {"t": "delivers", **base, "if": (hops[0][1] if hops else "")}
        if kind == "overlay":
            return {"t": "overlay", **base, "if": ", ".join(i for _, i in hops)}
        if kind == "unk":
            return {"t": "unknown", **base}
        targets = []
        for nh_ip, ifname in hops:
            k, name, how = self.resolve(node, nh_ip, ifname)
            targets.append({"k": k, "n": name, "nh": nh_ip, "if": ifname, "how": how})
        return {"t": "fwd", **base, "x": targets}

    def covers(self, ip: str) -> bool:
        """True when a trace to `ip` is exact on this model without reloading tables."""
        pp = parse_prefix(ip if "/" in ip else f"{ip}/32")
        if pp is None:
            return False
        a = pp[0]
        if any((a & MASKS[d.plen]) == d.net for d in self.destinations.values()):
            return True
        return ip in self.extra_destinations

    def node_for_name(self, name: str) -> Optional[str]:
        if name in self.by_node:
            return name
        hit = self.resolver.resolve(name)
        if hit is not None:
            n = self.resolver.node_id_for(hit)
            return n if n in self.by_node else None
        return None
