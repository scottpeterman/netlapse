"""
Tests for the volatility registry (netlapse/registry/volatility.py).

This is the diff-time policy layer: it decides which columns the semantic
diff ignores by default. Both directions of a bug here are silent —
over-matching hides a real change, under-matching puts age-timer noise back
into every poll — so the tests pin the set algebra rather than sampling it.

The contract under test, from the module docstring:

    (heuristic glob matches on schema) ∪ explicit volatile − keep − key_fields

intersected with the schema, so only columns that actually exist come back.

Fixtures use column names the shipped templates really emit, and lab
capture types. Nothing here reads the installed volatility.yaml — every
catalog is built in a tmp_path so the suite is independent of whatever is
seeded in a developer's home directory.
"""

import logging

import pytest
import yaml

from netlapse.registry.volatility import (
    BASELINE_HEURISTICS,
    BASELINE_LINE_MASKS,
    VolatilityCatalog,
    VolatilityError,
    baseline_drift,
    get_volatility_catalog,
    load_volatility_catalog,
    reset_volatility_cache,
    _bad_regex,
)


ARP_SCHEMA = ["IP_ADDRESS", "MAC_ADDRESS", "INTERFACE", "AGE", "TYPE"]
BGP_SCHEMA = [
    "BGP_NEIGH", "NEIGH_AS", "STATE", "UP_DOWN", "PREFIX_RECEIVED",
    "INPUT_PACKETS", "OUTPUT_PACKETS",
]
OSPF_SCHEMA = ["NEIGHBOR_ID", "INTERFACE", "STATE", "DEAD_TIME", "PRIORITY"]
MAC_SCHEMA = ["MAC_ADDRESS", "VLAN_ID", "DESTINATION_PORT", "MOVES", "LAST_MOVE"]


@pytest.fixture(autouse=True)
def _clear_cache():
    """The runtime accessor caches per directory; don't leak across tests."""
    reset_volatility_cache()
    yield
    reset_volatility_cache()


def write_yaml(tmp_path, body) -> object:
    (tmp_path / "volatility.yaml").write_text(
        body if isinstance(body, str) else yaml.safe_dump(body)
    )
    return tmp_path


# ---------------------------------------------------------------------------
# The code baseline is a floor, not a default
# ---------------------------------------------------------------------------

class TestBaselineFloor:

    def test_empty_catalog_still_excludes_age(self):
        """
        The whole point of the code baseline: a missing or stale YAML must
        not put age-timer noise back into every diff.
        """
        assert VolatilityCatalog().resolve("arp", ARP_SCHEMA) == ["AGE"]

    def test_empty_catalog_catches_ospf_dead_time_via_glob(self):
        assert VolatilityCatalog().resolve("ospf", OSPF_SCHEMA) == ["DEAD_TIME"]

    def test_empty_catalog_catches_counters(self):
        excluded = VolatilityCatalog().resolve("bgp", BGP_SCHEMA)
        assert "INPUT_PACKETS" in excluded
        assert "OUTPUT_PACKETS" in excluded

    def test_yaml_unions_onto_baseline_and_cannot_drop_it(self):
        """A YAML declaring one heuristic must not replace the floor."""
        catalog = VolatilityCatalog(heuristics=["PREFIX_RECEIVED"])
        excluded = catalog.resolve("bgp", BGP_SCHEMA)

        assert "PREFIX_RECEIVED" in excluded      # from the YAML
        assert "INPUT_PACKETS" in excluded        # still from the baseline

    def test_baseline_heuristics_are_first_and_deduplicated(self):
        catalog = VolatilityCatalog(heuristics=["AGE", "CUSTOM_*"])

        assert catalog.heuristics[:len(BASELINE_HEURISTICS)] == [
            h.upper() for h in BASELINE_HEURISTICS
        ]
        assert catalog.heuristics.count("AGE") == 1
        assert "CUSTOM_*" in catalog.heuristics

    def test_line_mask_baseline_is_a_floor_too(self):
        catalog = VolatilityCatalog(line_masks=[r"^! lab marker"])
        masks = catalog.resolve_line_masks("config")

        assert r"ntp clock-period\s+\d+" in masks
        assert r"^! lab marker" in masks


# ---------------------------------------------------------------------------
# Set algebra
# ---------------------------------------------------------------------------

class TestResolveAlgebra:

    def test_key_fields_are_never_excluded(self):
        """
        A key field excluded from the comparison would make every record
        look identical. This is the one rule with no override.
        """
        catalog = VolatilityCatalog(
            explicit={"arp": {"volatile": {"IP_ADDRESS"}, "keep": set()}}
        )
        excluded = catalog.resolve("arp", ARP_SCHEMA, key_fields=["IP_ADDRESS"])

        assert "IP_ADDRESS" not in excluded

    def test_key_field_beats_a_heuristic_match(self):
        catalog = VolatilityCatalog()
        excluded = catalog.resolve("arp", ARP_SCHEMA, key_fields=["AGE"])

        assert excluded == []

    def test_keep_rescues_a_column_a_heuristic_over_matched(self):
        """
        LAST_MOVE is genuinely interesting on a mac table even though
        LAST_* is a baseline heuristic. `keep` is the escape hatch.
        """
        catalog = VolatilityCatalog(
            explicit={"mac": {"volatile": set(), "keep": {"LAST_MOVE"}}}
        )
        assert catalog.resolve("mac", MAC_SCHEMA) == []

    def test_keep_beats_explicit_volatile(self):
        catalog = VolatilityCatalog(
            explicit={"arp": {"volatile": {"TYPE"}, "keep": {"TYPE"}}}
        )
        assert "TYPE" not in catalog.resolve("arp", ARP_SCHEMA)

    def test_explicit_volatile_adds_a_column_no_heuristic_matches(self):
        catalog = VolatilityCatalog(
            explicit={"bgp": {"volatile": {"UP_DOWN"}, "keep": set()}}
        )
        assert "UP_DOWN" in catalog.resolve("bgp", BGP_SCHEMA)

    def test_rules_are_scoped_to_their_capture_type(self):
        catalog = VolatilityCatalog(
            explicit={"bgp": {"volatile": {"TYPE"}, "keep": set()}}
        )
        assert "TYPE" not in catalog.resolve("arp", ARP_SCHEMA)

    def test_result_is_intersected_with_the_real_schema(self):
        """Only columns that actually exist may be returned."""
        catalog = VolatilityCatalog(
            explicit={"arp": {"volatile": {"NOT_A_COLUMN"}, "keep": set()}}
        )
        excluded = catalog.resolve("arp", ARP_SCHEMA)

        assert "NOT_A_COLUMN" not in excluded
        assert set(excluded).issubset(set(ARP_SCHEMA))

    def test_result_is_sorted(self):
        catalog = VolatilityCatalog()
        excluded = catalog.resolve("bgp", BGP_SCHEMA)
        assert excluded == sorted(excluded)

    def test_empty_schema_returns_empty(self):
        assert VolatilityCatalog().resolve("arp", []) == []
        assert VolatilityCatalog().resolve("arp", None) == []

    def test_unknown_capture_type_still_gets_the_baseline(self):
        assert VolatilityCatalog().resolve("brand-new", ARP_SCHEMA) == ["AGE"]


class TestCaseInsensitivity:

    def test_matching_is_case_insensitive(self):
        catalog = VolatilityCatalog()
        assert catalog.resolve("arp", ["Age", "ip_address"]) == ["Age"]

    def test_returned_names_keep_the_schema_casing(self):
        """The caller feeds these back to the diff engine as column names."""
        catalog = VolatilityCatalog()
        assert catalog.resolve("arp", ["Age"]) == ["Age"]

    def test_explicit_names_match_case_insensitively(self):
        catalog = VolatilityCatalog(
            explicit={"arp": {"volatile": {"type"}, "keep": set()}}
        )
        assert "TYPE" in catalog.resolve("arp", ARP_SCHEMA)

    def test_keep_matches_case_insensitively(self):
        catalog = VolatilityCatalog(
            explicit={"arp": {"volatile": set(), "keep": {"age"}}}
        )
        assert catalog.resolve("arp", ARP_SCHEMA) == []


# ---------------------------------------------------------------------------
# Line masks
# ---------------------------------------------------------------------------

class TestLineMasks:

    def test_global_masks_returned_for_any_capture_type(self):
        masks = VolatilityCatalog().resolve_line_masks("config")
        assert masks == BASELINE_LINE_MASKS

    def test_per_capture_masks_are_appended_after_global(self):
        catalog = VolatilityCatalog(
            explicit={"config": {"volatile": set(), "keep": set(),
                                 "line_masks": [r"^! lab only"]}}
        )
        masks = catalog.resolve_line_masks("config")

        assert masks[:len(BASELINE_LINE_MASKS)] == BASELINE_LINE_MASKS
        assert masks[-1] == r"^! lab only"

    def test_duplicate_pattern_is_not_repeated(self):
        dup = BASELINE_LINE_MASKS[0]
        catalog = VolatilityCatalog(
            explicit={"config": {"volatile": set(), "keep": set(),
                                 "line_masks": [dup]}}
        )
        assert catalog.resolve_line_masks("config").count(dup) == 1

    def test_per_capture_masks_do_not_leak_to_other_capture_types(self):
        catalog = VolatilityCatalog(
            explicit={"config": {"volatile": set(), "keep": set(),
                                 "line_masks": [r"^! lab only"]}}
        )
        assert r"^! lab only" not in catalog.resolve_line_masks("arp")

    @pytest.mark.parametrize("line", [
        "! Last configuration change at 09:14:02 UTC Mon Jul 20 2026",
        "ntp clock-period 17180291",
        "Current configuration : 4821 bytes",
        "## Last commit: 2026-07-20 09:14:02 UTC",
    ])
    def test_baseline_masks_match_the_lines_they_claim_to(self, line):
        import re
        masks = VolatilityCatalog().resolve_line_masks("config")
        assert any(re.search(p, line) for p in masks), line

    def test_baseline_masks_do_not_match_operator_set_config(self):
        import re
        masks = VolatilityCatalog().resolve_line_masks("config")
        for line in ["ntp server 192.0.2.123", "hostname eng-leaf-01",
                     "interface Ethernet1"]:
            assert not any(re.search(p, line) for p in masks), line


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

class TestLoader:

    def test_absent_file_yields_an_inert_catalog(self, tmp_path):
        catalog = load_volatility_catalog(tmp_path)
        assert catalog.declared_heuristics == []
        assert catalog.resolve("arp", ARP_SCHEMA) == ["AGE"]  # baseline intact

    def test_empty_file_yields_an_inert_catalog(self, tmp_path):
        write_yaml(tmp_path, "")
        assert load_volatility_catalog(tmp_path).declared_heuristics == []

    def test_valid_file_loads(self, tmp_path):
        write_yaml(tmp_path, {
            "heuristics": ["*_RECEIVED"],
            "line_masks": [r"^! lab"],
            "captures": {
                "mac": {"keep": ["LAST_MOVE"]},
                "bgp": {"volatile": ["UP_DOWN"]},
            },
        })
        catalog = load_volatility_catalog(tmp_path)

        assert "*_RECEIVED".upper() in catalog.heuristics
        assert catalog.explicit["mac"]["keep"] == {"LAST_MOVE"}
        assert catalog.explicit["bgp"]["volatile"] == {"UP_DOWN"}
        assert catalog.resolve("mac", MAC_SCHEMA) == []

    def test_capture_with_null_body_is_accepted(self, tmp_path):
        write_yaml(tmp_path, "captures:\n  arp:\n")
        catalog = load_volatility_catalog(tmp_path)
        assert catalog.explicit["arp"] == {"volatile": set(), "keep": set()}

    def test_invalid_yaml_raises(self, tmp_path):
        write_yaml(tmp_path, "captures:\n  arp:\n  - [unclosed\n")
        with pytest.raises(VolatilityError):
            load_volatility_catalog(tmp_path)

    def test_non_mapping_top_level_raises(self, tmp_path):
        write_yaml(tmp_path, ["not", "a", "mapping"])
        with pytest.raises(VolatilityError) as exc:
            load_volatility_catalog(tmp_path)
        assert "must be a mapping" in str(exc.value)

    def test_all_errors_are_reported_in_one_pass(self, tmp_path):
        """
        Validation is fail-soft across the whole file, not first-error-wins
        — fixing one typo shouldn't just reveal the next one.
        """
        write_yaml(tmp_path, {
            "heuristics": "not-a-list",
            "line_masks": ["[unclosed"],
            "captures": {
                "arp": {"volatile": "not-a-list"},
                "bgp": {"typo_key": ["x"]},
            },
        })
        with pytest.raises(VolatilityError) as exc:
            load_volatility_catalog(tmp_path)

        assert len(exc.value.errors) >= 4

    def test_invalid_regex_in_line_masks_is_caught(self, tmp_path):
        write_yaml(tmp_path, {"line_masks": ["(unclosed"]})
        with pytest.raises(VolatilityError) as exc:
            load_volatility_catalog(tmp_path)
        assert "invalid regex" in str(exc.value)

    def test_invalid_regex_in_per_capture_line_masks_is_caught(self, tmp_path):
        write_yaml(tmp_path, {"captures": {"config": {"line_masks": ["*bad"]}}})
        with pytest.raises(VolatilityError) as exc:
            load_volatility_catalog(tmp_path)
        assert "invalid regex" in str(exc.value)

    def test_unknown_capture_key_is_an_error(self, tmp_path):
        write_yaml(tmp_path, {"captures": {"arp": {"volatille": ["AGE"]}}})
        with pytest.raises(VolatilityError) as exc:
            load_volatility_catalog(tmp_path)
        assert "unknown key" in str(exc.value)

    @pytest.mark.parametrize("pattern,valid", [
        (r"ntp clock-period\s+\d+", True),
        (r"^! Time:.*", True),
        ("(unclosed", False),
        ("*bad", False),
    ])
    def test_bad_regex_helper(self, pattern, valid):
        assert (_bad_regex(pattern) is None) is valid


# ---------------------------------------------------------------------------
# Runtime accessor — fail-soft
# ---------------------------------------------------------------------------

class TestRuntimeAccessor:

    def test_malformed_file_degrades_to_baseline_instead_of_raising(
        self, tmp_path, caplog
    ):
        """
        A typo in the policy file must never block diffing. The diff still
        runs; it just falls back to the code baseline.
        """
        write_yaml(tmp_path, {"heuristics": "not-a-list"})

        with caplog.at_level(logging.WARNING):
            catalog = get_volatility_catalog(definitions_dir=tmp_path)

        assert catalog.resolve("arp", ARP_SCHEMA) == ["AGE"]
        assert "volatility.yaml" in caplog.text

    def test_catalog_is_cached_per_directory(self, tmp_path):
        write_yaml(tmp_path, {"heuristics": ["*_RECEIVED"]})
        first = get_volatility_catalog(definitions_dir=tmp_path)
        second = get_volatility_catalog(definitions_dir=tmp_path)
        assert first is second

    def test_reset_clears_the_cache(self, tmp_path):
        write_yaml(tmp_path, {"heuristics": ["*_RECEIVED"]})
        first = get_volatility_catalog(definitions_dir=tmp_path)
        reset_volatility_cache()
        assert get_volatility_catalog(definitions_dir=tmp_path) is not first


class TestBaselineDrift:

    def test_absent_file_reports_the_whole_baseline_as_undeclared(self, tmp_path):
        catalog = load_volatility_catalog(tmp_path)
        assert baseline_drift(catalog) == BASELINE_HEURISTICS

    def test_fully_declared_file_reports_no_drift(self, tmp_path):
        write_yaml(tmp_path, {"heuristics": list(BASELINE_HEURISTICS)})
        catalog = load_volatility_catalog(tmp_path)
        assert baseline_drift(catalog) == []

    def test_partially_declared_file_reports_only_the_gap(self, tmp_path):
        write_yaml(tmp_path, {"heuristics": ["AGE"]})
        drift = baseline_drift(load_volatility_catalog(tmp_path))

        assert "AGE" not in drift
        assert "*_TIME" in drift

    def test_drift_is_informational_only(self, tmp_path):
        """Drift must not change what gets filtered — the floor still applies."""
        write_yaml(tmp_path, {"heuristics": ["AGE"]})
        catalog = load_volatility_catalog(tmp_path)

        assert baseline_drift(catalog)
        assert catalog.resolve("ospf", OSPF_SCHEMA) == ["DEAD_TIME"]


# ---------------------------------------------------------------------------
# End-to-end with the diff engine
# ---------------------------------------------------------------------------

class TestWithDiffEngine:

    def test_arp_age_ticking_alone_is_not_a_change(self):
        """
        The pairing that justifies the whole layer: the resolver picks the
        exclusions, the diff engine applies them, and a poll where only the
        age timers moved reports nothing.
        """
        from netlapse.storage.diff import diff_records, resolve_key_fields

        v1 = [{"IP_ADDRESS": "192.0.2.10", "MAC_ADDRESS": "0200.5e00.0001",
               "INTERFACE": "Vlan10", "AGE": "0:00:11", "TYPE": "ARPA"}]
        v2 = [{"IP_ADDRESS": "192.0.2.10", "MAC_ADDRESS": "0200.5e00.0001",
               "INTERFACE": "Vlan10", "AGE": "0:24:38", "TYPE": "ARPA"}]

        keys = resolve_key_fields("arp", v1, v2)
        ignore = VolatilityCatalog().resolve("arp", ARP_SCHEMA, key_fields=keys)

        assert ignore == ["AGE"]
        assert diff_records("arp", v1, v2, ignore_fields=ignore) == ([], [], [])

    def test_a_real_change_still_surfaces_through_the_exclusions(self):
        from netlapse.storage.diff import diff_records, resolve_key_fields

        v1 = [{"IP_ADDRESS": "192.0.2.10", "MAC_ADDRESS": "0200.5e00.0001",
               "INTERFACE": "Vlan10", "AGE": "0:00:11", "TYPE": "ARPA"}]
        v2 = [{"IP_ADDRESS": "192.0.2.10", "MAC_ADDRESS": "0200.5e00.00ff",
               "INTERFACE": "Vlan10", "AGE": "0:24:38", "TYPE": "ARPA"}]

        keys = resolve_key_fields("arp", v1, v2)
        ignore = VolatilityCatalog().resolve("arp", ARP_SCHEMA, key_fields=keys)
        _, _, changed = diff_records("arp", v1, v2, ignore_fields=ignore)

        assert set(changed[0]["changes"]) == {"MAC_ADDRESS"}