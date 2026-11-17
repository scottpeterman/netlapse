"""
Netlapse Structured Diff Engine — backend-agnostic record comparison.

Path: netlapse/storage/diff.py

This module owns the *semantic* diff: given two lists of parsed records
(the `records` array from a capture's parsed JSON), it reports which were
added, removed, and changed. It knows nothing about storage — no git, no
files, no commits. Callers (any StorageBackend) read the two versions of a
capture's parsed JSON however they like, then hand the record lists here.

Why it lives on its own:
  The diff was originally embedded in GitBackend, which made structured
  diff look like a git feature. It isn't. The comparison is pure record
  logic; the only thing git contributed was reading two blobs out of
  commit trees. The file backend already versions parsed JSON in
  _history/, so it can produce the exact same diff with zero dependencies.
  One engine, both backends, single source of truth for DIFF_KEY_FIELDS.

The contract returned to the API/UI:
  added:   list[dict]  — whole records present only in `to`
  removed: list[dict]  — whole records present only in `from`
  changed: list[dict]  — { "key": {<key_field>: value, ...},
                           "changes": { <field>: {"from": old, "to": new}, ... } }
"""

import json
import logging
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Record identity per capture type.
#
# Each entry is an ORDERED LIST OF CANDIDATES. Each candidate is a
# COMPOSITE key — a list of field names that must ALL be present in the
# records for that candidate to be selected. The first candidate whose
# fields are all present wins; if none match, generic heuristics are
# tried, then whole-record hashing.
#
# Two axes are expressed here and they must not be confused:
#   * ACROSS candidates — vendor naming variants for the same identity
#     (one platform's template calls it BGP_NEIGH, another's NEIGHBOR).
#   * WITHIN a candidate — the fields that TOGETHER identify one record
#     (a MAC entry is identified by address AND vlan, not address alone).
#
# Collapsing those two axes into one flat list is what made an earlier
# version of this map resolve to a single field for every capture type,
# which silently dropped every record after the first per key. See
# _index_by_key(): duplicate keys are now detected, not overwritten.
#
# CRITICAL: these values must equal the COLUMN NAMES the TextFSM template
# emits for that capture type. If a template emits IP_ADDRESS but the map
# says ADDRESS, the diff silently drops to heuristics or hash comparison.
# The candidates below were derived by scanning the shipped template
# library for the columns actually emitted per command family — do the
# same before adding a capture type rather than guessing at a name.
DIFF_KEY_FIELDS: Dict[str, List[List[str]]] = {
    # An ARP entry is identified by its L3 address. IP_ADDRESS is what the
    # large majority of templates emit; the rest are naming variants.
    "arp": [
        ["IP_ADDRESS"],
        ["ADDRESS"],
        ["PROTOCOL_ADDRESS"],
        ["IP"],
    ],

    # A MAC entry is identified by address AND vlan — the same address can
    # legitimately appear in several vlans, and per-vlan is exactly where a
    # relearn on a different port needs to stay visible.
    "mac": [
        ["MAC_ADDRESS", "VLAN_ID"],
        ["DESTINATION_ADDRESS", "VLAN_ID"],
        ["MAC_ADDRESS", "VLAN"],
        ["MAC_ADDRESS", "VID"],
        ["MAC", "VLAN_ID"],
        ["MAC_ADDRESS"],
        ["DESTINATION_ADDRESS"],
    ],

    # A BGP peer is identified by its address; add VRF when the template
    # carries it, since the same peer address can exist per-VRF.
    "bgp": [
        ["VRF", "BGP_NEIGH"],
        ["VRF", "NEIGHBOR"],
        ["VRF", "PEER_IP"],
        ["BGP_NEIGH"],
        ["NEIGHBOR"],
        ["NEIGHBOR_IP"],
        ["PEER_IP"],          # Junos `show bgp summary`
    ],

    # An OSPF adjacency is per neighbor-id per interface — a router with
    # two links to the same neighbor has two adjacencies.
    "ospf": [
        ["NEIGHBOR_ID", "INTERFACE"],
        ["NEIGHBOR_ID", "LOCAL_INTERFACE"],
        ["NEIGHBOR_ID"],
    ],

    # A logical interface is listed once per address family on some
    # platforms, so the interface name alone is not a row identity there
    # (ae0.0 / bme0.0 / em0.0 appear several times). The wider composites are
    # tried first and validation picks whichever actually separates the rows.
    "interfaces": [
        ["INTERFACE", "PROTO", "LOCAL"],
        ["INTERFACE", "PROTO"],
        ["INTERFACE", "LOCAL", "DESTINATION"],
        ["INTERFACE", "LOCAL"],
        ["INTERFACE"],
        ["INTF"],
        ["PORT"],
        ["NAME"],
    ],

    # A route table row is a PATH, not a prefix: an ECMP prefix has one row
    # per next-hop. Keying on prefix alone made losing one of two next-hops
    # look like a next-hop change on a single route. Next-hop is therefore
    # part of identity — the consequence is that a next-hop change on a
    # single-path route reports as removed + added rather than changed,
    # which is the honest reading (the old path is gone, a new one exists).
    # Prefix length is part of identity too: a /8 and a /24 sharing a
    # network address are not the same route.
    "routes": [
        ["VRF", "NETWORK", "PREFIX_LENGTH", "NEXTHOP_IP"],
        ["NETWORK", "PREFIX_LENGTH", "NEXTHOP_IP"],
        ["NETWORK", "MASK", "NEXTHOP_IP"],
        ["NETWORK", "MASK", "NEXTHOP"],
        ["PREFIX", "NEXT_HOP"],
        ["NETWORK", "PREFIX_LENGTH"],
        ["NETWORK", "MASK"],
        ["PREFIX"],
        ["DESTINATION", "GATEWAY"],
    ],

    "vlans": [
        ["VLAN_ID"],
        ["VLAN"],
    ],

    # Spanning-tree state is per interface per vlan/instance.
    "spanning": [
        ["VLAN_ID", "INTERFACE"],
        ["INSTANCE", "INTERFACE"],
        ["INTERFACE"],
    ],

    # A discovery adjacency is identified by the local port AND what is on
    # the far end — a port facing two neighbors (a hub, or a phone with a
    # daisy-chained host) is two adjacencies, not one.
    "cdp": [
        ["LOCAL_INTERFACE", "NEIGHBOR_NAME", "NEIGHBOR_INTERFACE"],
        ["LOCAL_INTERFACE", "DEVICE_ID", "PORT_ID"],
        ["LOCAL_INTF", "DEVICE_ID", "PORT_ID"],
        ["LOCAL_INTERFACE", "NEIGHBOR_NAME"],
        ["LOCAL_INTERFACE", "NEIGHBOR_INTERFACE"],
        ["LOCAL_INTERFACE"],
    ],
    "lldp": [
        ["LOCAL_INTERFACE", "NEIGHBOR_NAME", "NEIGHBOR_INTERFACE"],
        ["PARENT_INTERFACE", "LOCAL_INTERFACE", "NEIGHBOR_NAME"],
        ["LOCAL_INTERFACE", "NEIGHBOR_NAME", "NEIGHBOR_PORT_ID"],
        ["LOCAL_INTERFACE", "NEIGHBOR", "NEIGHBOR_INTERFACE"],
        ["LOCAL_INTERFACE", "NEIGHBOR_NAME"],
        ["LOCAL_INTERFACE"],
    ],

    # NAME is the part model on several platforms, not the slot — a chassis
    # of identical optics is forty records with one NAME. Serial and slot are
    # the real identities; NAME survives only as a last resort for the
    # platforms where it IS the slot label.
    "inventory": [
        ["PORT", "NAME"],
        ["NAME", "SN"],
        ["PID", "SN"],
        ["SN"],
        ["PID"],
        ["NAME"],
    ],
}

# Capture types that are DOCUMENTS, not record sets.
#
# A running config has no record identity — it is prose with structure, not a
# table. tfsm-fire will happily score it against whatever show-command template
# fits best, producing structurally valid records with no semantic meaning, so
# these are served by the line-aligned config-aware diff instead.
#
# This is an explicit opt-OUT rather than an opt-in, and the direction matters:
# gating the structured diff on membership in DIFF_KEY_FIELDS meant any capture
# type added through captures.yaml — the whole point of the data-driven catalog
# — silently got no semantic diff and fell through to the expensive text path.
# Unknown record captures resolve a key through the heuristics in
# resolve_key_fields() and degrade loudly (see _index_by_key) if it isn't
# unique, which is the correct failure mode for a type nobody has curated yet.
RAW_CAPTURE_TYPES: set = {
    "config",
    "config-backup",
    "running-config",
    "startup-config",
}


def is_record_capture(
    capture_type: str,
    from_records: list,
    to_records: list,
) -> bool:
    """
    Should this capture be compared as records, or as text?

    True when the capture is not a known document type AND either it has a
    curated key model or its parsed records yield a usable key. False for
    configs, for captures that parsed to nothing, and for record sets with no
    identifiable key — all of which the text diff serves better.

    Both backends route on this so the change feed, the drill-in, and the
    dashboard can't disagree about which engine answered.
    """
    if capture_type in RAW_CAPTURE_TYPES:
        return False
    if not (from_records or to_records):
        return False
    if capture_type in DIFF_KEY_FIELDS:
        return True
    return bool(resolve_key_fields(capture_type, from_records, to_records))


# Generic identifier-like fields tried when the capture type isn't in the
# map, or none of its candidates match the columns actually present.
#
# These are LAST RESORT and deliberately ordered most- to least-specific.
# A heuristic key is by nature unproven, so a collision on one is expected
# to happen occasionally — _index_by_key() reports it and the caller
# degrades to hash comparison rather than dropping records.
_HEURISTIC_KEYS: List[List[str]] = [
    ["IP_ADDRESS"],
    ["MAC_ADDRESS"],
    ["NEIGHBOR_ID"],
    ["ADDRESS"],
    ["ID"],
    ["NAME"],
    ["INTERFACE"],
    ["INTF"],
    ["IP"],
]


# Note: which fields are "volatile" (excluded from the comparison by default —
# age timers, counters) is policy, not algorithm, so it does NOT live here. It
# is declared in definitions/volatility.yaml and resolved by
# registry/volatility.py against the live schema. This module stays a pure
# record-comparison engine: it excludes exactly the fields it is handed via
# `ignore_fields` and decides nothing about which those should be.


def record_field_names(from_records: list, to_records: list) -> List[str]:
    """
    Sorted union of column names across both record sets — the schema the UI
    uses to offer per-field exclusion, and the input the volatility resolver
    matches its heuristics against. Empty for capture types with no parsed
    records (e.g. raw config).
    """
    names = set()
    for rec in (from_records or []):
        if isinstance(rec, dict):
            names.update(rec.keys())
    for rec in (to_records or []):
        if isinstance(rec, dict):
            names.update(rec.keys())
    return sorted(names)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def diff_records(
    capture_type: str,
    from_records: list,
    to_records: list,
    ignore_fields: Optional[Iterable[str]] = None,
) -> Tuple[list, list, list]:
    """
    Compare two lists of parsed records.

    Resolves the composite key for the capture type, matches records on it,
    and reports (added, removed, changed). Falls back to whole-record hash
    comparison when no usable key is present, or when the resolved key is
    not unique over the data (correct, just not field-aware: a change shows
    as a remove + add rather than a 'changed').

    Args:
        capture_type: the capture type slug (selects DIFF_KEY_FIELDS entry).
        from_records: parsed records from the older version.
        to_records:   parsed records from the newer version.
        ignore_fields: column names to exclude from the comparison. A record
            that differs only on ignored fields is reported as unchanged.
            Key fields are never ignored. None means "compare every field".

    Returns:
        (added, removed, changed) — see module docstring for shapes.
    """
    from_records = _dict_records(from_records)
    to_records = _dict_records(to_records)

    if not from_records and not to_records:
        return [], [], []

    key_fields = resolve_key_fields(capture_type, from_records, to_records)

    # Key fields identify a record; they can never be excluded.
    ignore = set(ignore_fields or []) - set(key_fields)

    if not key_fields:
        return _diff_by_hash(from_records, to_records, ignore)

    return _diff_by_key(
        from_records, to_records, key_fields, ignore, capture_type
    )


def resolve_key_fields(
    capture_type: str,
    from_records: list,
    to_records: list,
    validate: bool = True,
) -> List[str]:
    """
    Determine which field(s) together identify a record for this type.

    Walks DIFF_KEY_FIELDS candidates in order, then the generic heuristics,
    and returns the first that is *usable over the records in hand* — all
    fields present, no record keyed entirely on placeholders, and no two
    records in a version sharing a key. Returns [] when nothing qualifies,
    which signals the caller to use hash comparison.

    Validating against the data rather than the schema is the whole point.
    A column existing says nothing about whether it identifies anything: an
    interface table can carry an IP_ADDRESS column that is blank on every
    L2 port, an inventory table's NAME can be the part model rather than the
    slot, and a discovery table can carry a literal '-'. Selecting those
    produced a key that mapped fifty records onto one, which is the failure
    this engine exists to avoid. Validation also does useful work upward: a
    candidate rejected for non-uniqueness lets the NEXT candidate — usually
    the wider composite that disambiguates it — win automatically, so
    per-vendor row shapes resolve themselves without curation.

    Presence is tested against the union of columns across BOTH record sets,
    but uniqueness is tested WITHIN each version separately — the same key
    appearing in both versions is the normal case, and is how a record is
    recognised as unchanged.

    Set validate=False to get the field-presence-only answer (what would
    have been chosen without the data check). Diagnostics use this to report
    what a naive selection would have collided on.

    Returns a list because keys are composite. A single-field key comes
    back as a one-element list, so the shape is stable for callers.
    """
    present = set(record_field_names(from_records, to_records))
    if not present:
        return []

    candidates = _candidate_list(capture_type)

    rejected: List[str] = []
    for candidate in candidates:
        if not all(field in present for field in candidate):
            continue
        if not validate:
            return list(candidate)

        reason = _candidate_defect(candidate, from_records, to_records)
        if reason is None:
            if rejected:
                logger.debug(
                    "capture_type=%s key=%s selected after rejecting %s",
                    capture_type or "?", "+".join(candidate),
                    "; ".join(rejected),
                )
            return list(candidate)
        rejected.append(f"{'+'.join(candidate)} ({reason})")

    if rejected:
        logger.warning(
            "capture_type=%s: no usable record key — rejected %s; "
            "falling back to hash comparison",
            capture_type or "?", "; ".join(rejected),
        )
    return []


def _candidate_list(capture_type: str) -> List[List[str]]:
    """
    Curated candidates for this capture type followed by the generic
    heuristics, de-duplicated. A heuristic frequently repeats a curated
    candidate (both list NAME, say); trying it twice wastes a pass and makes
    a diagnostic report the same rejection reason twice.
    """
    out: List[List[str]] = []
    seen = set()
    for candidate in [*DIFF_KEY_FIELDS.get(capture_type, []), *_HEURISTIC_KEYS]:
        marker = tuple(candidate)
        if marker not in seen:
            seen.add(marker)
            out.append(list(candidate))
    return out


def _candidate_defect(
    candidate: List[str],
    from_records: list,
    to_records: list,
) -> Optional[str]:
    """
    Why this candidate can't serve as a key, or None if it can.

    The only disqualifier is non-uniqueness: two records in the same version
    sharing a key. That single test subsumes the failures worth catching — a
    column blank on fifty rows collides fifty times, a column holding a
    literal '-' collides on the second such row, and a part model shared by
    forty optics collides thirty-nine times.

    An earlier version also rejected any candidate containing a placeholder
    value. That was invented rather than measured, and auditing a real fleet
    showed it only subtracted: a composite whose LAST field is blank on some
    rows is still a perfectly good key when the tuple as a whole is unique,
    and rejecting it dropped ~1600 snapshots to hash comparison to avoid a
    weakness that hash comparison has anyway. Uniqueness is the property
    that matters; blankness is only a proxy for it.

    The reason string is surfaced by explain_key_selection() so an operator
    auditing a real fleet can see WHY a capture fell back to hashing rather
    than guessing at which candidate to add.
    """
    for records in (from_records, to_records):
        if not records:
            continue
        seen = set()
        for record in records:
            key = tuple(_hashable(record.get(f, "")) for f in candidate)
            if key in seen:
                return f"not unique (duplicate key {key})"
            seen.add(key)
    return None


def explain_key_selection(
    capture_type: str,
    from_records: list,
    to_records: list = (),
) -> List[dict]:
    """
    Per-candidate verdict for a capture's key resolution.

    Returns one entry per candidate whose fields are all present, in the
    order they were tried: {"fields": [...], "usable": bool, "reason": str}.
    The first usable entry is what resolve_key_fields() returned; if none is
    usable the diff fell back to hash comparison and the reasons say why.

    Answering "why is this capture not getting a semantic diff?" from logs
    alone means reproducing the candidate walk by hand, so it lives here.
    """
    from_records = _dict_records(from_records)
    to_records = _dict_records(to_records)
    present = set(record_field_names(from_records, to_records))

    candidates = _candidate_list(capture_type)

    out: List[dict] = []
    for candidate in candidates:
        if not all(field in present for field in candidate):
            continue
        reason = _candidate_defect(candidate, from_records, to_records)
        out.append({
            "fields": list(candidate),
            "usable": reason is None,
            "reason": reason or "",
        })
        if reason is None:
            break
    return out


def detect_key_collisions(capture_type: str, records: list) -> List[dict]:
    """
    Report records that share a resolved key within a single version.

    A collision means the key does not uniquely identify a record for this
    data, so the semantic diff cannot be trusted for it. Exposed so parse
    auditing / health can surface it per device rather than leaving it to a
    log line — a capture whose key collides is a capture whose "no changes"
    result is meaningless.

    Returns a list of {"key": {...}, "count": n}, empty when the key is
    unique (or when no key could be resolved at all).
    """
    records = _dict_records(records)
    # validate=False on purpose: this reports what the naive, presence-only
    # selection would have collided on. The live resolver rejects such a
    # candidate and moves on, so asking it here would always return nothing.
    key_fields = resolve_key_fields(capture_type, records, [], validate=False)
    if not key_fields:
        return []

    _, collisions = _index_by_key(records, key_fields)
    return [
        {"key": dict(zip(key_fields, key)), "count": count}
        for key, count in sorted(collisions.items())
    ]


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _dict_records(records) -> list:
    """Drop anything that isn't a dict; the engine only compares records."""
    return [r for r in (records or []) if isinstance(r, dict)]


def _hashable(value) -> str:
    """
    Render a field value as a stable, hashable key component.

    A TextFSM `Value List` column arrives as a list, which is unhashable
    and would raise when placed in a key tuple. Everything is normalised to
    a string so mixed types across versions (a column parsed as "10" in one
    version and 10 in another) don't split one record into two.
    """
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, default=str)
    if value is None:
        return ""
    return str(value)


def _index_by_key(
    records: list,
    key_fields: List[str],
) -> Tuple[Dict[tuple, dict], Dict[tuple, int]]:
    """
    Build {key: record} plus a map of any key seen more than once.

    The previous implementation was a dict comprehension, so a duplicate
    key silently overwrote the earlier record and the diff then compared a
    subset of the data while reporting on all of it. Collisions are now
    returned so the caller can degrade loudly instead.
    """
    index: Dict[tuple, dict] = {}
    collisions: Dict[tuple, int] = {}

    for record in records:
        key = tuple(_hashable(record.get(f, "")) for f in key_fields)
        if key in index:
            collisions[key] = collisions.get(key, 1) + 1
        index[key] = record

    return index, collisions


def _diff_by_key(
    from_records: list,
    to_records: list,
    key_fields: List[str],
    ignore: Optional[set] = None,
    capture_type: str = "",
) -> Tuple[list, list, list]:
    """
    Diff records matched by composite key.

    Returns (added, removed, changed). A 'changed' entry carries the key
    that identifies the record plus a per-field {from, to} of what moved.
    Fields in `ignore` are skipped; a record differing only on ignored
    fields is not reported as changed.

    If the key is not unique on either side, the comparison would run over
    a silently truncated record set, so this falls back to hash comparison
    — a change then surfaces as remove + add, which is noisy but true. A
    silent false negative on "did the network change" is the one failure
    mode this engine must not have.
    """
    ignore = ignore or set()

    from_map, from_dupes = _index_by_key(from_records, key_fields)
    to_map, to_dupes = _index_by_key(to_records, key_fields)

    if from_dupes or to_dupes:
        logger.warning(
            "capture_type=%s key=%s is not unique (%d duplicate keys); "
            "falling back to hash comparison so no records are dropped",
            capture_type or "?",
            "+".join(key_fields),
            len(from_dupes) + len(to_dupes),
        )
        return _diff_by_hash(from_records, to_records, ignore)

    from_keys = set(from_map.keys())
    to_keys = set(to_map.keys())

    added = [to_map[k] for k in sorted(to_keys - from_keys)]
    removed = [from_map[k] for k in sorted(from_keys - to_keys)]

    changed = []
    for k in sorted(from_keys & to_keys):
        old = from_map[k]
        new = to_map[k]
        if old == new:
            continue
        diffs = {}
        for field in sorted(set(old.keys()) | set(new.keys())):
            if field in ignore:
                continue
            old_val = old.get(field)
            new_val = new.get(field)
            if old_val != new_val:
                diffs[field] = {"from": old_val, "to": new_val}
        if diffs:
            changed.append({
                "key": {kf: new.get(kf) for kf in key_fields},
                "changes": diffs,
            })

    return added, removed, changed


def _diff_by_hash(
    from_records: list,
    to_records: list,
    ignore: Optional[set] = None,
) -> Tuple[list, list, list]:
    """
    Fallback diff when no unique key is available.

    Compares records as canonicalized JSON. A modified record appears as a
    removal + an addition (no 'changed'), because without a stable key
    there's no way to say two records are 'the same record, changed.'
    Ignored fields are dropped from the hash, so records differing only on
    ignored fields collapse to identical and don't show as remove + add.

    Multiplicity is preserved: a table losing one of three identical rows
    is a real change and must not vanish into a set operation.
    """
    ignore = ignore or set()

    def record_hash(r: dict) -> str:
        clean = {k: v for k, v in r.items() if k not in ignore} if ignore else r
        return json.dumps(clean, sort_keys=True, default=str)

    from_groups: Dict[str, List[dict]] = {}
    for r in from_records:
        from_groups.setdefault(record_hash(r), []).append(r)
    to_groups: Dict[str, List[dict]] = {}
    for r in to_records:
        to_groups.setdefault(record_hash(r), []).append(r)

    added: List[dict] = []
    removed: List[dict] = []
    for h in sorted(set(from_groups) | set(to_groups)):
        old = from_groups.get(h, [])
        new = to_groups.get(h, [])
        if len(new) > len(old):
            added.extend(new[: len(new) - len(old)])
        elif len(old) > len(new):
            removed.extend(old[: len(old) - len(new)])

    return added, removed, []