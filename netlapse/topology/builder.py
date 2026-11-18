"""
Neighbor claims → links → SC2 map.json.

Every LLDP record is one device's claim about one of its ports. A link's
state says how well the two ends agree:

  confirmed   both ends claim each other on matching ports. Also accepted
              when the far end claims us on the port we heard from but
              advertised its own port as something other than an interface
              name (a description, an ifIndex, a MAC) — common between
              vendors, and the reason SC2's strict port match drops links.
              Finally, when each device lists the other exactly once, that
              single pair is confirmed even if neither port id is usable.
  one_sided   the far end is in DCIM and has LLDP data at this time, but
              does not claim us back. Stale entry, LLDP off on that port,
              or a name collision — worth a look.
  unverified  the far end is in DCIM but has no LLDP capture at this time.
              Nothing to confirm against.
  unmanaged   the far end is not in DCIM (servers, carrier gear, phones).
              Also: a short-name or management-address match that lands in
              another site and isn't confirmed by the far end — the usual
              cause is a bare hostname or an OOB address reused per site.

SC2 drops anything not confirmed when the peer was discovered; netlapse
keeps them and tags the state so the viewer can draw them differently and
the diff can report a link going from confirmed to one_sided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from netlapse.topology.history import CaptureReader
from netlapse.topology.lldp import NeighborClaim, normalize_records, records_from_envelope
from netlapse.topology.names import NameResolver, display_interface, interface_key

LLDP_CAPTURE = "lldp"

# Captures that give the viewer's node dialog its lazy tabs.
_ARTIFACT_TABS = {
    "config": "config_file",
    "mac": "mac_table_file",
    "inventory": "inventory_file",
}

STATE_RANK = {"confirmed": 0, "unverified": 1, "unmanaged": 2, "one_sided": 3}


@dataclass
class Link:
    """One physical adjacency. a/b are node ids; *_if are display names."""
    a: str
    a_if: str
    b: str
    b_if: str
    state: str
    managed_b: bool = True
    detail: str = ""

    @property
    def key(self) -> str:
        """Order-independent identity used by the diff."""
        ends = sorted([(self.a.lower(), interface_key(self.a_if)),
                       (self.b.lower(), interface_key(self.b_if) or self.b.lower())])
        return "|".join(f"{n}:{i}" for n, i in ends)

    @property
    def edge_id(self) -> str:
        """Same id the viewer gives the cytoscape edge between a and b."""
        return "--".join(sorted([self.a, self.b]))

    def to_dict(self) -> dict:
        return {
            "key": self.key, "edge_id": self.edge_id,
            "a": self.a, "a_if": self.a_if, "b": self.b, "b_if": self.b_if,
            "state": self.state, "managed_b": self.managed_b, "detail": self.detail,
        }


@dataclass
class TopologyResult:
    map: dict
    links: List[Link]
    as_of: Optional[datetime]
    version: Optional[str] = None
    stats: dict = field(default_factory=dict)
    missing_lldp: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat() + "Z" if self.as_of else None,
            "version": self.version,
            "stats": self.stats,
            "missing_lldp": self.missing_lldp,
            "links": [l.to_dict() for l in self.links],
            "map": self.map,
        }


@dataclass
class _Claim:
    src: str            # node id of the claimer
    local: str          # display name of claimer's port
    local_key: str
    peer: str           # node id (managed) or advertised name (unmanaged)
    peer_managed: bool
    remote: str         # display name of the port the peer advertised
    remote_key: str
    raw: NeighborClaim
    how: str = ""           # name | short | ip — see NameResolver.resolve_ex
    cross_site: bool = False


def build_topology(
    devices: Iterable[dict],
    reader: CaptureReader,
    site=None,
    include_unmanaged: bool = False,
) -> TopologyResult:
    """Build the map from LLDP captures as ``reader`` sees them.

    ``devices`` is the full DCIM device list — name resolution and link
    confirmation always use the whole fleet so a link from a site to a
    device elsewhere is still confirmed. ``site`` only scopes the output:
    devices in the site(s) plus anything one link away. It takes one slug,
    a comma-separated string, or an iterable of slugs; empty means all.
    """
    sites = _site_set(site)
    devices = [d for d in devices if d.get("name") and d.get("site_slug")]
    resolver = NameResolver(devices)
    by_id: Dict[str, dict] = {resolver.node_id_for(d): d for d in devices}

    claims: List[_Claim] = []
    has_lldp: Set[str] = set()
    lldp_when: Dict[str, Optional[datetime]] = {}
    neighbor_rows: Dict[str, List[dict]] = {}
    unmanaged_names: Dict[str, str] = {}   # lower → first-seen spelling

    for nid, d in by_id.items():
        env, when = reader.parsed(d["site_slug"], d["name"], LLDP_CAPTURE)
        if env is None:
            continue
        has_lldp.add(nid)
        lldp_when[nid] = when
        recs = normalize_records(records_from_envelope(env))
        neighbor_rows[nid] = [c.as_neighbor_row() for c in recs]
        for c in recs:
            hit, how = resolver.resolve_ex(c.remote_name, c.remote_ip, from_site=d["site_slug"],
                                           exclude_id=d["id"])
            if hit is not None:
                peer, managed = resolver.node_id_for(hit), True
                if peer == nid:
                    continue  # a device hearing itself (loopback cable, LLDP relay)
            else:
                peer = unmanaged_names.setdefault(c.remote_name.lower(), c.remote_name)
                managed = False
            claims.append(_Claim(
                src=nid,
                local=display_interface(c.local_port),
                local_key=interface_key(c.local_port),
                peer=peer,
                peer_managed=managed,
                remote=display_interface(c.remote_port or c.remote_port_desc),
                remote_key=interface_key(c.remote_port),
                raw=c,
                how=how if managed else "",
                cross_site=managed and hit.get("site_slug") != d["site_slug"],
            ))

    # Index: what does each managed device claim, by its own local port, and
    # toward whom.
    by_src_port: Dict[Tuple[str, str], List[_Claim]] = {}
    by_src_peer: Dict[Tuple[str, str], List[_Claim]] = {}
    for c in claims:
        by_src_port.setdefault((c.src, c.local_key), []).append(c)
        by_src_peer.setdefault((c.src, c.peer), []).append(c)

    links: Dict[str, Link] = {}
    for c in claims:
        if c.peer_managed and c.cross_site and c.how in ("short", "ip"):
            # A short name or reused OOB address that lands in another site
            # is only believed when the far end claims us back.
            back = _find_reverse(c, by_src_port, by_src_peer) if c.peer in has_lldp else None
            if back is None:
                matched = c.peer
                c.peer = unmanaged_names.setdefault(c.raw.remote_name.lower(), c.raw.remote_name)
                c.peer_managed = False
                basis = "short name" if c.how == "short" else f"mgmt address {c.raw.remote_ip}"
                link = Link(c.src, c.local, c.peer, c.remote, "unmanaged", managed_b=False,
                            detail=f"{basis} matches {matched} in another site; not confirmed")
                prev = links.get(link.key)
                if prev is None or STATE_RANK[link.state] < STATE_RANK[prev.state]:
                    links[link.key] = link
                continue
        if not c.peer_managed:
            link = Link(c.src, c.local, c.peer, c.remote, "unmanaged", managed_b=False,
                        detail=c.raw.remote_desc)
        elif c.peer not in has_lldp:
            link = Link(c.src, c.local, c.peer, c.remote, "unverified")
        else:
            back = _find_reverse(c, by_src_port, by_src_peer)
            if back is not None:
                # Use each side's own local port name: it is the
                # authoritative spelling, the advertised one may not be.
                link = Link(c.src, c.local, c.peer, back.local, "confirmed")
            else:
                link = Link(c.src, c.local, c.peer, c.remote, "one_sided",
                            detail=f"{c.peer} has no LLDP entry for {c.src}")
        prev = links.get(link.key)
        if prev is None or STATE_RANK[link.state] < STATE_RANK[prev.state]:
            links[link.key] = link

    all_links = sorted(links.values(), key=lambda l: (l.a.lower(), interface_key(l.a_if), l.b.lower()))

    # ── scope ───────────────────────────────────────────────────────
    hidden_unmanaged = 0
    scoped: List[Link] = []
    for l in all_links:
        if l.state == "unmanaged" and not include_unmanaged:
            hidden_unmanaged += 1
            continue
        if sites:
            a_in = by_id.get(l.a, {}).get("site_slug") in sites
            b_in = by_id.get(l.b, {}).get("site_slug") in sites
            if not (a_in or b_in):
                continue
        scoped.append(l)

    in_scope_nodes: Set[str] = {nid for nid, d in by_id.items() if not sites or d["site_slug"] in sites}
    for l in scoped:
        for n in (l.a, l.b):
            if n in by_id:
                in_scope_nodes.add(n)

    # ── map.json ────────────────────────────────────────────────────
    topo: Dict[str, dict] = {}
    for nid in sorted(in_scope_nodes, key=str.lower):
        d = by_id[nid]
        topo[nid] = {
            "node_details": _node_details(d, nid, reader, has_lldp, lldp_when, neighbor_rows, sites),
            "peers": {},
        }

    for l in scoped:
        _add_peer(topo, l.a, l.b, l.a_if, l.b_if, l, by_id)
        if l.b in topo:
            _add_peer(topo, l.b, l.a, l.b_if, l.a_if, l, by_id)

    missing = sorted(
        (nid for nid in in_scope_nodes if nid not in has_lldp), key=str.lower
    )
    counts: Dict[str, int] = {}
    for l in scoped:
        counts[l.state] = counts.get(l.state, 0) + 1

    stats = {
        "devices": len(in_scope_nodes),
        "devices_with_lldp": len([n for n in in_scope_nodes if n in has_lldp]),
        "links": len(scoped),
        "by_state": counts,
        "unmanaged_hidden": hidden_unmanaged,
        "unmanaged_neighbors": len({l.b.lower() for l in scoped if l.state == "unmanaged"}),
    }
    return TopologyResult(
        map=topo, links=scoped, as_of=reader.as_of,
        version=reader.resolved_version, stats=stats, missing_lldp=missing,
    )


def _find_reverse(c: _Claim, by_src_port, by_src_peer) -> Optional[_Claim]:
    # Exact: the peer, on the port we heard it advertise, claims us back on
    # the port we heard it from.
    if c.remote_key:
        for r in by_src_port.get((c.peer, c.remote_key), []):
            if r.peer == c.src and r.remote_key == c.local_key:
                return r
    # Loose: the peer claims us on our port, but what it advertised as its
    # own port did not look like its interface name.
    back = by_src_peer.get((c.peer, c.src), [])
    cands = [r for r in back if r.remote_key == c.local_key]
    if len(cands) == 1:
        return cands[0]
    # Last resort: neither end advertised a usable port id (ifIndex on one
    # side, a template that lost it on the other), but each device lists the
    # other exactly once. One link between them, so the ends are known.
    if len(back) == 1 and len(by_src_peer.get((c.src, c.peer), [])) == 1:
        return back[0]
    return None


def _add_peer(topo, node, peer, local_if, remote_if, link: Link, by_id):
    entry = topo[node]["peers"].setdefault(peer, {
        "ip": "", "platform": "", "connections": [], "link_states": [],
    })
    if link.managed_b or peer in by_id:
        pd = by_id.get(peer, {})
        entry["ip"] = entry["ip"] or (pd.get("primary_ip4") or "").split("/")[0]
        entry["platform"] = entry["platform"] or pd.get("platform_name") or pd.get("platform_slug") or ""
    else:
        entry["advertised"] = {"name": link.b, "description": link.detail}
        entry["platform"] = entry["platform"] or link.detail or ""
    conn = [local_if, remote_if]
    if conn not in entry["connections"]:
        entry["connections"].append(conn)
        entry["link_states"].append(link.state)


def _site_set(site) -> Set[str]:
    if not site:
        return set()
    if isinstance(site, str):
        site = site.split(",")
    return {s.strip() for s in site if s and s.strip()}


def _node_details(d, nid, reader: CaptureReader, has_lldp, lldp_when, neighbor_rows, scope_sites):
    site, name = d["site_slug"], d["name"]
    ssh = {}
    for capture, key in _ARTIFACT_TABS.items():
        if reader.has(site, name, capture):
            ssh[key] = f"{capture}.txt"
    when = lldp_when.get(nid)
    return {
        "ip": (d.get("primary_ip4") or "").split("/")[0],
        "platform": d.get("platform_name") or d.get("platform_slug") or "",
        "vendor": d.get("manufacturer_name") or "",
        "serial": d.get("serial_number") or "",
        "metadata": {
            "device_id": d.get("id"),
            "device_dir": f"{site}/{name}",
            "site": site,
            "role": d.get("role_slug") or "",
            "platform_slug": d.get("platform_slug") or "",
            "out_of_scope": bool(scope_sites and site not in scope_sites),
            "lldp": "ok" if nid in has_lldp else "missing",
            "neighbors": neighbor_rows.get(nid, []),
            "counts": {"neighbors": len(neighbor_rows.get(nid, []))},
            "discovered_via": "netlapse lldp capture",
            "discovered_at": when.isoformat() + "Z" if when else "",
            "enrichment": {
                "collected_at": when.isoformat() + "Z" if when else "",
                "ssh": dict(ssh, status="ok" if nid in has_lldp else "no lldp capture"),
            },
        },
    }
