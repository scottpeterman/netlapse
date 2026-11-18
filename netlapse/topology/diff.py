"""
Compare two built topologies.

Links are matched on Link.key (both endpoints as (node, interface-key),
order-independent), so a cable moved to another port is a removal plus an
addition, not a change. A link present at both times whose state moved
(confirmed → one_sided, say) is 'changed'.

The output is a union map: every node and link from either side, so the
viewer can draw removed things greyed out next to added ones. Each link
carries 'change' and, where it applies, 'state_from'/'state_to'.
"""

from __future__ import annotations

import copy
from typing import Dict, List

from netlapse.topology.builder import Link, TopologyResult


def diff_topologies(old: TopologyResult, new: TopologyResult) -> dict:
    old_links: Dict[str, Link] = {l.key: l for l in old.links}
    new_links: Dict[str, Link] = {l.key: l for l in new.links}

    out_links: List[dict] = []
    for key in sorted(set(old_links) | set(new_links)):
        o, n = old_links.get(key), new_links.get(key)
        if o and n:
            d = n.to_dict()
            if o.state != n.state:
                d.update(change="changed", state_from=o.state, state_to=n.state)
            else:
                d["change"] = "unchanged"
        elif n:
            d = n.to_dict()
            d["change"] = "added"
        else:
            d = o.to_dict()
            d["change"] = "removed"
        out_links.append(d)

    old_nodes, new_nodes = set(old.map), set(new.map)
    node_change = {}
    for nid in old_nodes | new_nodes:
        if nid in old_nodes and nid in new_nodes:
            node_change[nid] = "unchanged"
        elif nid in new_nodes:
            node_change[nid] = "added"
        else:
            node_change[nid] = "removed"

    # Union map: start from new, fold in what only old had.
    union = copy.deepcopy(new.map)
    for nid, entry in old.map.items():
        if nid not in union:
            union[nid] = copy.deepcopy(entry)
            continue
        for peer, pdata in entry.get("peers", {}).items():
            tgt = union[nid]["peers"].setdefault(
                peer, {k: v for k, v in pdata.items() if k not in ("connections", "link_states")}
            )
            tgt.setdefault("connections", [])
            tgt.setdefault("link_states", [])
            for conn, st in zip(pdata.get("connections", []), pdata.get("link_states", [])):
                if conn not in tgt["connections"]:
                    tgt["connections"].append(conn)
                    tgt["link_states"].append(st)

    counts: Dict[str, int] = {}
    for d in out_links:
        counts[d["change"]] = counts.get(d["change"], 0) + 1
    node_counts: Dict[str, int] = {}
    for c in node_change.values():
        node_counts[c] = node_counts.get(c, 0) + 1

    return {
        "from": {"as_of": old.to_dict()["as_of"], "version": old.version, "stats": old.stats},
        "to": {"as_of": new.to_dict()["as_of"], "version": new.version, "stats": new.stats},
        "summary": {"links": counts, "nodes": node_counts},
        "links": out_links,
        "nodes": node_change,
        "map": union,
    }
