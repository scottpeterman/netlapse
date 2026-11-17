"""
Tests for the structured diff engine (netlapse/storage/diff.py).

All fixtures use lab devices (eng-leaf/eng-spine, vEOS) and documentation
address space (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) with
locally-administered MACs, so nothing here depends on or reveals a real
network.

Column names in the fixtures are the ones the shipped TextFSM templates
actually emit for each command family. That matters: a test that invents
plausible column names would pass while the engine silently fails on real
parsed output, which is exactly how the key-collapse defect survived.
"""

import logging

import pytest

from netlapse.storage.diff import (
    DIFF_KEY_FIELDS,
    RAW_CAPTURE_TYPES,
    detect_key_collisions,
    diff_records,
    is_record_capture,
    record_field_names,
    resolve_key_fields,
    _diff_by_hash,
    _hashable,
    _index_by_key,
)


# ---------------------------------------------------------------------------
# Fixtures — shaped like real parsed output
# ---------------------------------------------------------------------------

def arp(ip, mac, intf, age="0:01:00"):
    return {"IP_ADDRESS": ip, "MAC_ADDRESS": mac, "INTERFACE": intf, "AGE": age}


def mac_entry(addr, vlan, port, moves="0"):
    return {
        "MAC_ADDRESS": addr,
        "VLAN_ID": vlan,
        "DESTINATION_PORT": port,
        "TYPE": "DYNAMIC",
        "MOVES": moves,
    }


def route(network, plen, nexthop, protocol="B", vrf="default"):
    return {
        "VRF": vrf,
        "NETWORK": network,
        "PREFIX_LENGTH": plen,
        "NEXTHOP_IP": nexthop,
        "PROTOCOL": protocol,
        "UPTIME": "1d02h",
    }


def lldp(local, neighbor, remote):
    return {
        "LOCAL_INTERFACE": local,
        "NEIGHBOR_NAME": neighbor,
        "NEIGHBOR_INTERFACE": remote,
    }


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------

class TestResolveKeyFields:

    def test_arp_keys_on_ip_not_interface(self):
        """
        Regression: the map said ADDRESS, templates emit IP_ADDRESS, so key
        resolution fell through the heuristics onto INTERFACE and collapsed
        every host on a vlan into one record.
        """
        recs = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        assert resolve_key_fields("arp", recs, []) == ["IP_ADDRESS"]

    def test_mac_key_is_composite(self):
        recs = [mac_entry("0200.5e00.0001", "10", "Ethernet1")]
        assert resolve_key_fields("mac", recs, []) == ["MAC_ADDRESS", "VLAN_ID"]

    def test_route_key_includes_prefix_length_and_nexthop(self):
        recs = [route("198.51.100.0", "24", "192.0.2.1")]
        assert resolve_key_fields("routes", recs, []) == [
            "VRF", "NETWORK", "PREFIX_LENGTH", "NEXTHOP_IP",
        ]

    def test_lldp_key_includes_far_end(self):
        recs = [lldp("Ethernet1", "eng-spine-01", "Ethernet49/1")]
        assert resolve_key_fields("lldp", recs, []) == [
            "LOCAL_INTERFACE", "NEIGHBOR_NAME", "NEIGHBOR_INTERFACE",
        ]

    def test_partial_candidate_is_skipped_for_next_one(self):
        """A candidate only wins when ALL of its fields are present."""
        recs = [{"MAC_ADDRESS": "0200.5e00.0001", "DESTINATION_PORT": "Et1"}]
        assert resolve_key_fields("mac", recs, []) == ["MAC_ADDRESS"]

    def test_vendor_variant_falls_through_to_next_candidate(self):
        recs = [{"BGP_NEIGH": "192.0.2.2", "STATE": "Established"}]
        assert resolve_key_fields("bgp", recs, []) == ["BGP_NEIGH"]

        recs = [{"NEIGHBOR": "192.0.2.2", "STATE": "Established"}]
        assert resolve_key_fields("bgp", recs, []) == ["NEIGHBOR"]

    def test_ragged_records_use_union_not_first_row(self):
        """
        Optional columns are frequently absent from the first parsed row.
        Sampling record[0] made key selection depend on emission order, so
        presence is tested against the union. One ragged row is tolerated —
        it cannot collide with anything — so the composite still wins.
        """
        recs = [
            {"MAC_ADDRESS": "0200.5e00.0001", "DESTINATION_PORT": "Ethernet1"},
            {"MAC_ADDRESS": "0200.5e00.0002", "VLAN_ID": "20",
             "DESTINATION_PORT": "Ethernet2"},
        ]
        assert resolve_key_fields("mac", recs, []) == ["MAC_ADDRESS", "VLAN_ID"]

    def test_composite_wins_when_the_column_is_populated_on_every_row(self):
        recs = [
            {"MAC_ADDRESS": "0200.5e00.0001", "VLAN_ID": "10",
             "DESTINATION_PORT": "Ethernet1"},
            {"MAC_ADDRESS": "0200.5e00.0001", "VLAN_ID": "20",
             "DESTINATION_PORT": "Ethernet2"},
        ]
        assert resolve_key_fields("mac", recs, []) == ["MAC_ADDRESS", "VLAN_ID"]

    def test_unknown_capture_type_uses_heuristics(self):
        recs = [{"IP_ADDRESS": "192.0.2.5", "STATE": "up"}]
        assert resolve_key_fields("something-new", recs, []) == ["IP_ADDRESS"]

    def test_no_usable_key_returns_empty(self):
        recs = [{"COUNTER": "5", "STATE": "up"}]
        assert resolve_key_fields("arp", recs, []) == []

    def test_empty_records_return_empty(self):
        assert resolve_key_fields("arp", [], []) == []

    def test_key_resolved_from_to_side_when_from_is_empty(self):
        recs = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        assert resolve_key_fields("arp", [], recs) == ["IP_ADDRESS"]


class TestKeyMapIntegrity:
    """Guard the shape of the map itself — it is data, so it can rot."""

    def test_every_entry_is_a_list_of_candidate_lists(self):
        for capture_type, candidates in DIFF_KEY_FIELDS.items():
            assert isinstance(candidates, list), capture_type
            assert candidates, f"{capture_type} has no candidates"
            for candidate in candidates:
                assert isinstance(candidate, list), capture_type
                assert candidate, f"{capture_type} has an empty candidate"
                assert all(isinstance(f, str) for f in candidate), capture_type

    def test_candidates_are_ordered_most_specific_first(self):
        """
        A shorter candidate listed before a longer one can never be beaten
        by it, which silently disables the composite key.
        """
        for capture_type, candidates in DIFF_KEY_FIELDS.items():
            for i, candidate in enumerate(candidates):
                for later in candidates[i + 1:]:
                    assert not set(candidate).issubset(set(later)), (
                        f"{capture_type}: {candidate} shadows {later}"
                    )


# ---------------------------------------------------------------------------
# The regression cases from the design review
# ---------------------------------------------------------------------------

class TestKeyCollapseRegressions:

    def test_arp_mac_change_is_reported(self):
        """
        Two hosts on one vlan, one changes MAC. Under the old key
        (INTERFACE) both collapsed to a single record and the diff was
        empty — a silent false negative on the tool's core question.
        """
        v1 = [
            arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:04:11"),
            arp("192.0.2.11", "0200.5e00.0002", "Vlan10", age="0:00:52"),
        ]
        v2 = [
            arp("192.0.2.10", "0200.5e00.00ff", "Vlan10", age="0:00:03"),
            arp("192.0.2.11", "0200.5e00.0002", "Vlan10", age="0:01:52"),
        ]
        added, removed, changed = diff_records("arp", v1, v2, ignore_fields=["AGE"])

        assert added == []
        assert removed == []
        assert len(changed) == 1
        assert changed[0]["key"] == {"IP_ADDRESS": "192.0.2.10"}
        assert changed[0]["changes"] == {
            "MAC_ADDRESS": {"from": "0200.5e00.0001", "to": "0200.5e00.00ff"}
        }

    def test_mac_moves_port_in_one_vlan_while_static_in_another(self):
        """
        The README's headline example. Same address in vlan 10 and vlan 20;
        the vlan-10 entry relearns on a different port. Keying on address
        alone dropped one of the two records and reported nothing.
        """
        v1 = [
            mac_entry("0200.5e00.00aa", "10", "Ethernet1"),
            mac_entry("0200.5e00.00aa", "20", "Ethernet48"),
        ]
        v2 = [
            mac_entry("0200.5e00.00aa", "10", "Ethernet7", moves="1"),
            mac_entry("0200.5e00.00aa", "20", "Ethernet48"),
        ]
        added, removed, changed = diff_records("mac", v1, v2)

        assert added == []
        assert removed == []
        assert len(changed) == 1
        assert changed[0]["key"] == {
            "MAC_ADDRESS": "0200.5e00.00aa", "VLAN_ID": "10",
        }
        assert changed[0]["changes"]["DESTINATION_PORT"] == {
            "from": "Ethernet1", "to": "Ethernet7",
        }

    def test_ecmp_losing_one_nexthop_is_a_removal_not_a_change(self):
        """
        Keying on prefix alone reported a lost path as a next-hop change on
        a surviving route — a real outage rendered as a cosmetic edit.
        """
        v1 = [
            route("198.51.100.0", "24", "192.0.2.1"),
            route("198.51.100.0", "24", "192.0.2.5"),
        ]
        v2 = [route("198.51.100.0", "24", "192.0.2.1")]

        added, removed, changed = diff_records("routes", v1, v2)

        assert added == []
        assert changed == []
        assert len(removed) == 1
        assert removed[0]["NEXTHOP_IP"] == "192.0.2.5"

    def test_same_network_different_prefix_length_are_distinct_routes(self):
        v1 = [route("203.0.113.0", "24", "192.0.2.1")]
        v2 = [
            route("203.0.113.0", "24", "192.0.2.1"),
            route("203.0.113.0", "28", "192.0.2.1"),
        ]
        added, removed, changed = diff_records("routes", v1, v2)

        assert len(added) == 1
        assert added[0]["PREFIX_LENGTH"] == "28"
        assert removed == []
        assert changed == []

    def test_port_with_two_neighbors_does_not_collapse(self):
        """A port facing two LLDP neighbors is two adjacencies."""
        v1 = [
            lldp("Ethernet10", "eng-access-01", "Ethernet1"),
            lldp("Ethernet10", "eng-phone-04", "Port1"),
        ]
        v2 = [lldp("Ethernet10", "eng-access-01", "Ethernet1")]

        added, removed, changed = diff_records("lldp", v1, v2)

        assert added == []
        assert changed == []
        assert len(removed) == 1
        assert removed[0]["NEIGHBOR_NAME"] == "eng-phone-04"


# ---------------------------------------------------------------------------
# Core diff semantics
# ---------------------------------------------------------------------------

class TestDiffSemantics:

    def test_added_and_removed(self):
        v1 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        v2 = [arp("192.0.2.11", "0200.5e00.0002", "Vlan10")]
        added, removed, changed = diff_records("arp", v1, v2)

        assert [r["IP_ADDRESS"] for r in added] == ["192.0.2.11"]
        assert [r["IP_ADDRESS"] for r in removed] == ["192.0.2.10"]
        assert changed == []

    def test_identical_records_produce_no_diff(self):
        recs = [
            arp("192.0.2.10", "0200.5e00.0001", "Vlan10"),
            arp("192.0.2.11", "0200.5e00.0002", "Vlan10"),
        ]
        assert diff_records("arp", recs, list(recs)) == ([], [], [])

    def test_both_sides_empty(self):
        assert diff_records("arp", [], []) == ([], [], [])

    def test_none_inputs_are_tolerated(self):
        assert diff_records("arp", None, None) == ([], [], [])

    def test_first_collection_reports_everything_as_added(self):
        v2 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        added, removed, changed = diff_records("arp", [], v2)
        assert len(added) == 1
        assert removed == [] and changed == []

    def test_non_dict_records_are_ignored(self):
        v1 = ["garbage", None, arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        v2 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        assert diff_records("arp", v1, v2) == ([], [], [])

    def test_changed_entry_reports_only_the_fields_that_moved(self):
        v1 = [mac_entry("0200.5e00.0001", "10", "Ethernet1")]
        v2 = [mac_entry("0200.5e00.0001", "10", "Ethernet2", moves="3")]
        _, _, changed = diff_records("mac", v1, v2)

        assert set(changed[0]["changes"]) == {"DESTINATION_PORT", "MOVES"}

    def test_new_column_appearing_is_reported_as_a_change(self):
        v1 = [{"IP_ADDRESS": "192.0.2.10", "MAC_ADDRESS": "0200.5e00.0001"}]
        v2 = [{"IP_ADDRESS": "192.0.2.10", "MAC_ADDRESS": "0200.5e00.0001",
               "INTERFACE": "Vlan10"}]
        _, _, changed = diff_records("arp", v1, v2)

        assert changed[0]["changes"]["INTERFACE"] == {"from": None, "to": "Vlan10"}


class TestIgnoreFields:

    def test_ignored_field_change_is_not_reported(self):
        v1 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:00:10")]
        v2 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:29:44")]
        assert diff_records("arp", v1, v2, ignore_fields=["AGE"]) == ([], [], [])

    def test_ignored_field_does_not_mask_a_real_change(self):
        v1 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:00:10")]
        v2 = [arp("192.0.2.10", "0200.5e00.0002", "Vlan10", age="0:29:44")]
        _, _, changed = diff_records("arp", v1, v2, ignore_fields=["AGE"])

        assert set(changed[0]["changes"]) == {"MAC_ADDRESS"}

    def test_key_field_cannot_be_ignored(self):
        v1 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        v2 = [arp("192.0.2.99", "0200.5e00.0001", "Vlan10")]
        added, removed, changed = diff_records(
            "arp", v1, v2, ignore_fields=["IP_ADDRESS"]
        )
        assert len(added) == 1 and len(removed) == 1
        assert changed == []

    def test_none_means_compare_every_field(self):
        v1 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:00:10")]
        v2 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:29:44")]
        _, _, changed = diff_records("arp", v1, v2, ignore_fields=None)
        assert "AGE" in changed[0]["changes"]

    def test_empty_list_means_compare_every_field(self):
        v1 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:00:10")]
        v2 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10", age="0:29:44")]
        _, _, changed = diff_records("arp", v1, v2, ignore_fields=[])
        assert "AGE" in changed[0]["changes"]


# ---------------------------------------------------------------------------
# Collision detection and degradation
# ---------------------------------------------------------------------------

class TestCollisions:

    def test_resolver_escapes_to_a_composite_rather_than_colliding(self):
        """
        Two line cards sharing a NAME. Rather than key on NAME and drop one,
        the resolver rejects that candidate and takes the next one that
        actually separates the rows.
        """
        v1 = [
            {"NAME": "slot1", "DESCR": "linecard", "SN": "LAB0001"},
            {"NAME": "slot1", "DESCR": "linecard", "SN": "LAB0002"},
        ]
        assert resolve_key_fields("inventory", v1, []) == ["NAME", "SN"]

        added, removed, changed = diff_records("inventory", v1, v1[:1])
        assert len(removed) == 1 and removed[0]["SN"] == "LAB0002"
        assert added == [] and changed == []

    def test_unusable_key_degrades_to_hash_without_dropping_records(self, caplog):
        """
        When no candidate survives validation the diff must fall back to
        hash comparison — noisy, but no record is silently discarded.
        """
        v1 = [
            {"NAME": "slot1", "DESCR": "linecard"},
            {"NAME": "slot1", "DESCR": "supervisor"},
        ]
        v2 = [{"NAME": "slot1", "DESCR": "linecard"}]

        with caplog.at_level(logging.WARNING):
            added, removed, changed = diff_records("inventory", v1, v2)

        assert len(removed) == 1
        assert removed[0]["DESCR"] == "supervisor"
        assert added == [] and changed == []
        assert "no usable record key" in caplog.text

    def test_detect_key_collisions_reports_the_naive_offender(self):
        """
        Diagnostics report what a presence-only selection WOULD have
        collided on — the live resolver rejects it, so this is the only way
        to surface that the column is unsuitable.
        """
        recs = [
            {"NAME": "slot1"},
            {"NAME": "slot1"},
            {"NAME": "slot2"},
        ]
        collisions = detect_key_collisions("inventory", recs)

        assert collisions == [{"key": {"NAME": "slot1"}, "count": 2}]

    def test_detect_key_collisions_empty_when_unique(self):
        recs = [
            arp("192.0.2.10", "0200.5e00.0001", "Vlan10"),
            arp("192.0.2.11", "0200.5e00.0002", "Vlan10"),
        ]
        assert detect_key_collisions("arp", recs) == []

    def test_detect_key_collisions_empty_when_no_key(self):
        assert detect_key_collisions("arp", [{"COUNTER": "1"}]) == []

    def test_index_by_key_counts_repeats(self):
        recs = [{"A": "1"}, {"A": "1"}, {"A": "1"}, {"A": "2"}]
        index, collisions = _index_by_key(recs, ["A"])

        assert set(index) == {("1",), ("2",)}
        assert collisions == {("1",): 3}


class TestKeyValidation:
    """
    A column existing says nothing about whether it identifies anything.
    These are the real-fleet shapes that a presence-only check selected and
    then silently collapsed.
    """

    def test_blank_column_is_rejected_in_favour_of_a_populated_one(self):
        """
        An interface table carries an address column that is blank on every
        L2 port. Keying on it maps every such port onto the empty string.
        """
        recs = [
            {"INTERFACE": f"Ethernet{i}", "IP_ADDRESS": "", "MTU": "9214"}
            for i in range(1, 20)
        ]
        assert resolve_key_fields("mtu", recs, []) == ["INTERFACE"]

    def test_one_ragged_row_does_not_disqualify_a_working_key(self):
        """
        A single blank cannot collide with anything. Rejecting the key over
        it would throw away a working key for one malformed row — which is
        what an earlier, stricter rule did across thousands of real
        snapshots.
        """
        recs = [
            {"IP_ADDRESS": "192.0.2.10", "MAC_ADDRESS": "0200.5e00.0001"},
            {"IP_ADDRESS": "192.0.2.11", "MAC_ADDRESS": "0200.5e00.0002"},
            {"IP_ADDRESS": "", "MAC_ADDRESS": "0200.5e00.0003"},
        ]
        assert resolve_key_fields("arp", recs, []) == ["IP_ADDRESS"]

    @pytest.mark.parametrize("placeholder", ["", "-", "--", "N/A", "none"])
    def test_repeated_placeholder_values_are_never_a_key(self, placeholder):
        """Caught by uniqueness: the second placeholder row duplicates the first."""
        recs = [
            {"LOCAL_INTERFACE": "Ethernet1", "MAC_ADDRESS": placeholder},
            {"LOCAL_INTERFACE": "Ethernet2", "MAC_ADDRESS": placeholder},
        ]
        assert "MAC_ADDRESS" not in resolve_key_fields("lldp", recs, [])

    def test_model_column_loses_to_slot_on_an_inventory_of_optics(self):
        """Forty identical optics share a NAME; the slot separates them."""
        recs = [
            {"NAME": "SFP-10G-SR-OSI", "SN": f"LAB{i:04d}", "PORT": f"Et{i}"}
            for i in range(40)
        ]
        assert resolve_key_fields("inventory", recs, []) == ["PORT", "NAME"]

    def test_model_column_loses_to_serial_when_there_is_no_slot(self):
        recs = [
            {"NAME": "SFP-10G-SR-OSI", "SN": f"LAB{i:04d}"}
            for i in range(40)
        ]
        assert resolve_key_fields("inventory", recs, []) == ["NAME", "SN"]

    def test_per_address_family_rows_resolve_to_the_wider_composite(self):
        """
        A logical interface listed once per address family: the name alone
        repeats, so the resolver takes the widest composite that separates
        the rows — including a field that is blank on some of them, which is
        fine as long as the tuple as a whole is unique.
        """
        recs = [
            {"INTERFACE": "ae0.0", "PROTO": "inet", "LOCAL": "192.0.2.1"},
            {"INTERFACE": "ae0.0", "PROTO": "inet6", "LOCAL": "2001:db8::1"},
            {"INTERFACE": "ae0.0", "PROTO": "multiservice", "LOCAL": ""},
            {"INTERFACE": "ae1.0", "PROTO": "multiservice", "LOCAL": ""},
        ]
        assert resolve_key_fields("interfaces", recs, []) == [
            "INTERFACE", "PROTO", "LOCAL",
        ]

    def test_uniqueness_is_checked_within_a_version_not_across(self):
        """
        The same key appearing in both versions is the normal case — it is
        how an unchanged record is recognised.
        """
        v1 = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        v2 = [arp("192.0.2.10", "0200.5e00.0002", "Vlan10")]
        assert resolve_key_fields("arp", v1, v2) == ["IP_ADDRESS"]

    def test_a_bad_candidate_in_one_version_rejects_it_for_both(self):
        v1 = [{"IP_ADDRESS": "192.0.2.10", "INTERFACE": "Vlan10"}]
        v2 = [
            {"IP_ADDRESS": "", "INTERFACE": "Ethernet1"},
            {"IP_ADDRESS": "", "INTERFACE": "Ethernet2"},
        ]
        assert resolve_key_fields("arp", v1, v2) == ["INTERFACE"]

    def test_validate_false_returns_the_presence_only_answer(self):
        recs = [{"NAME": "slot1"}, {"NAME": "slot1"}]
        assert resolve_key_fields("inventory", recs, [], validate=False) == ["NAME"]
        assert resolve_key_fields("inventory", recs, []) == []

    def test_single_record_always_validates(self):
        recs = [{"NAME": "slot1"}]
        assert resolve_key_fields("inventory", recs, []) == ["NAME"]

    def test_rejection_is_logged_with_the_reason(self, caplog):
        recs = [{"NAME": "slot1"}, {"NAME": "slot1"}]
        with caplog.at_level(logging.WARNING):
            resolve_key_fields("inventory", recs, [])

        assert "not unique" in caplog.text


# ---------------------------------------------------------------------------
# Hash fallback
# ---------------------------------------------------------------------------

class TestHashFallback:

    def test_unkeyable_records_diff_by_hash(self):
        v1 = [{"COUNTER": "1", "STATE": "up"}]
        v2 = [{"COUNTER": "2", "STATE": "up"}]
        added, removed, changed = diff_records("arp", v1, v2)

        assert len(added) == 1 and len(removed) == 1
        assert changed == []

    def test_hash_fallback_honours_ignore_fields(self):
        v1 = [{"COUNTER": "1", "STATE": "up"}]
        v2 = [{"COUNTER": "2", "STATE": "up"}]
        assert diff_records("arp", v1, v2, ignore_fields=["COUNTER"]) == ([], [], [])

    def test_hash_fallback_preserves_multiplicity(self):
        """
        Three identical rows becoming two is a real change; a set-based
        comparison would report nothing.
        """
        row = {"COUNTER": "1", "STATE": "up"}
        added, removed, changed = _diff_by_hash([row, row, row], [row, row])

        assert added == []
        assert len(removed) == 1
        assert changed == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_record_field_names_is_sorted_union(self):
        v1 = [{"B": 1, "A": 2}]
        v2 = [{"C": 3}]
        assert record_field_names(v1, v2) == ["A", "B", "C"]

    def test_record_field_names_skips_non_dicts(self):
        assert record_field_names(["x", None, {"A": 1}], None) == ["A"]

    @pytest.mark.parametrize("value,expected", [
        ("Ethernet1", "Ethernet1"),
        (10, "10"),
        (None, ""),
        (["a", "b"], '["a", "b"]'),
    ])
    def test_hashable_normalises(self, value, expected):
        assert _hashable(value) == expected

    def test_list_valued_key_field_does_not_raise(self):
        """
        A TextFSM `Value List` column is a list — unhashable, and it used
        to blow up the key tuple.
        """
        v1 = [{"IP_ADDRESS": ["192.0.2.10"], "STATE": "up"}]
        v2 = [{"IP_ADDRESS": ["192.0.2.10"], "STATE": "down"}]
        _, _, changed = diff_records("arp", v1, v2)

        assert changed[0]["changes"]["STATE"] == {"from": "up", "to": "down"}

    def test_mixed_type_key_values_match_across_versions(self):
        """A vlan parsed as "10" then 10 is still the same record."""
        v1 = [{"MAC_ADDRESS": "0200.5e00.0001", "VLAN_ID": "10", "PORT": "Et1"}]
        v2 = [{"MAC_ADDRESS": "0200.5e00.0001", "VLAN_ID": 10, "PORT": "Et2"}]
        added, removed, changed = diff_records("mac", v1, v2)

        assert added == [] and removed == []
        assert changed[0]["changes"]["PORT"] == {"from": "Et1", "to": "Et2"}


# ---------------------------------------------------------------------------
# Record vs document routing
# ---------------------------------------------------------------------------

class TestRecordVsDocumentRouting:
    """
    Which engine answers for a capture. Routing on membership in
    DIFF_KEY_FIELDS meant any capture type added through captures.yaml got no
    semantic diff at all and silently fell through to the text diff — which
    defeats the point of a data-driven capture catalog.
    """

    def test_builtin_record_capture_routes_to_the_record_engine(self):
        recs = [arp("192.0.2.10", "0200.5e00.0001", "Vlan10")]
        assert is_record_capture("arp", recs, [])

    def test_config_is_a_document_even_when_it_parsed_to_records(self):
        """
        Template scoring will match a config against some show-command
        template and emit structurally valid, semantically meaningless
        records. Documents must never reach the record engine.
        """
        bogus = [{"NAME": "hostname", "VALUE": "eng-leaf-01"}]
        assert not is_record_capture("config", bogus, [])

    @pytest.mark.parametrize("capture_type", sorted(RAW_CAPTURE_TYPES))
    def test_every_declared_document_type_routes_to_text(self, capture_type):
        assert not is_record_capture(capture_type, [{"NAME": "x"}], [])

    def test_yaml_defined_capture_type_gets_a_semantic_diff(self):
        """
        The regression this routing change fixes: a capture type nobody
        hardcoded still resolves a key through the heuristics.
        """
        recs = [{"INTERFACE": "Ethernet1", "MTU": "9214", "STATUS": "up"}]
        assert is_record_capture("mtu", recs, [])
        assert resolve_key_fields("mtu", recs, []) == ["INTERFACE"]

    def test_yaml_defined_capture_type_diffs_by_record(self):
        v1 = [{"INTERFACE": "Ethernet1", "MTU": "9214"},
              {"INTERFACE": "Ethernet2", "MTU": "9214"}]
        v2 = [{"INTERFACE": "Ethernet1", "MTU": "1500"},
              {"INTERFACE": "Ethernet2", "MTU": "9214"}]
        added, removed, changed = diff_records("mtu", v1, v2)

        assert added == [] and removed == []
        assert changed[0]["key"] == {"INTERFACE": "Ethernet1"}
        assert changed[0]["changes"]["MTU"] == {"from": "9214", "to": "1500"}

    def test_unparseable_capture_routes_to_text(self):
        assert not is_record_capture("something-new", [], [])

    def test_records_with_no_resolvable_key_route_to_text(self):
        """Hash-diffing a keyless record set is worse than the text diff."""
        recs = [{"COUNTER": "1", "STATE": "up"}]
        assert not is_record_capture("something-new", recs, [])

    def test_routing_uses_both_sides(self):
        """A capture appearing for the first time still routes correctly."""
        recs = [{"INTERFACE": "Ethernet1", "MTU": "9214"}]
        assert is_record_capture("mtu", [], recs)

    def test_no_document_type_is_also_a_key_map_entry(self):
        """The two tables must not disagree about the same capture type."""
        assert not (RAW_CAPTURE_TYPES & set(DIFF_KEY_FIELDS))