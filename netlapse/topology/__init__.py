"""
Netlapse topology — a network map built from stored LLDP captures.

Nothing here talks to a device. The map is derived entirely from parsed
``lldp`` snapshots already in storage, joined against the DCIM inventory,
which means it can be built for any point in time the storage backend still
holds (``as_of``) and two of those points can be compared (``diff``).

Modules:
    lldp     — fold the per-template LLDP field-name variants into one shape
    names    — interface-name keys and the neighbor-name → DCIM device resolver
    history  — read a capture as it stood at a given time (git and file backends)
    builder  — neighbor claims → links (confirmed / one-sided / unverified)
               → SC2-compatible map.json
    diff     — compare two built topologies, emit a union map tagged by change

The map format is Secure Cartography's map.json, so the same document renders
in the ported viewer, opens in SC2, and round-trips through
netlapse_import_map.py.
"""

from netlapse.topology.builder import build_topology, TopologyResult
from netlapse.topology.diff import diff_topologies

__all__ = ["build_topology", "TopologyResult", "diff_topologies"]
