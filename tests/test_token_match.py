"""
Tests for the One Search token matcher (netlapse/storage/token_match.py).

The module's whole value is that it matches a token *the way it appears in
device output* — so the tests assert against strings shaped like real CLI
output rather than against the regex source.

Fixtures use lab hostnames and documentation address space (192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32) with locally-administered
MACs and private-range ASNs.
"""

import pytest

from netlapse.storage.token_match import build_matcher, classify


def matches(token: str, haystack: str) -> bool:
    return bool(build_matcher(token)["regex"].search(haystack))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestClassify:

    @pytest.mark.parametrize("token", [
        "192.0.2.1", "198.51.100.254", "0.0.0.0", "255.255.255.255",
    ])
    def test_ipv4(self, token):
        assert classify(token) == "ipv4"

    @pytest.mark.parametrize("token", [
        "2001:db8::1", "2001:0db8:0000:0000:0000:0000:0000:0001", "::1",
    ])
    def test_ipv6(self, token):
        assert classify(token) == "ipv6"

    @pytest.mark.parametrize("token", [
        "0200.5e00.0001",     # cisco dotted
        "02:00:5e:00:00:01",  # colon
        "02-00-5e-00-00-01",  # dash
        "02005e000001",       # bare
        "02005E000001",       # bare, uppercase
    ])
    def test_mac_in_every_vendor_format(self, token):
        assert classify(token) == "mac"

    @pytest.mark.parametrize("token", ["AS64512", "as64512", "As65001"])
    def test_asn(self, token):
        assert classify(token) == "asn"

    @pytest.mark.parametrize("token", ["10", "4094", "64512"])
    def test_number(self, token):
        assert classify(token) == "number"

    @pytest.mark.parametrize("token", [
        "eng-leaf-01", "Ethernet49/1", "uplink to spine", "LAB0001XYZ",
    ])
    def test_text(self, token):
        assert classify(token) == "text"

    @pytest.mark.parametrize("token", ["", "   ", "\t"])
    def test_blank_is_text(self, token):
        assert classify(token) == "text"

    @pytest.mark.parametrize("token", [
        "192.0.2.256", "192.0.2", "192.0.2.1.5", "not-an-ip",
    ])
    def test_malformed_input_never_raises(self, token):
        assert classify(token) in {"text", "number"}
        build_matcher(token)  # must not raise

    def test_ip_is_classified_before_mac(self):
        """Ordering matters — an address must never be typed as a MAC."""
        assert classify("192.0.2.1") == "ipv4"

    def test_twelve_hex_chars_are_typed_as_mac(self):
        """
        Documented consequence of the format-agnostic MAC rule: a serial or
        chassis id that happens to be 12 hex characters types as a MAC. It
        still matches itself, so search works; only the `kind` label (and
        the extra format variants offered) are affected.
        """
        assert classify("deadbeefcafe") == "mac"
        assert matches("deadbeefcafe", "Serial: deadbeefcafe")


# ---------------------------------------------------------------------------
# IPv4 — octet-boundary anchoring
# ---------------------------------------------------------------------------

class TestIPv4Matching:

    @pytest.mark.parametrize("haystack", [
        "192.0.2.1",
        "  192.0.2.1  ",
        "ip address 192.0.2.1/24",
        "neighbor 192.0.2.1 remote-as 64512",
        "Internet  192.0.2.1   0   0200.5e00.0001  ARPA  Vlan10",
        "next-hop 192.0.2.1,",
    ])
    def test_matches_real_output_shapes(self, haystack):
        assert matches("192.0.2.1", haystack)

    @pytest.mark.parametrize("haystack", [
        "192.0.2.10",    # longer, same prefix
        "192.0.2.100",
        "1192.0.2.1",    # extra leading digit
        "192.0.2.11",
    ])
    def test_does_not_match_inside_a_longer_address(self, haystack):
        assert not matches("192.0.2.1", haystack)

    def test_does_not_match_a_different_address(self):
        assert not matches("192.0.2.1", "198.51.100.1")

    def test_matches_the_masked_form(self):
        assert matches("198.51.100.0", "198.51.100.0/24 via 192.0.2.1")


# ---------------------------------------------------------------------------
# IPv6 — canonicalization
# ---------------------------------------------------------------------------

class TestIPv6Matching:

    def test_expanded_token_matches_compressed_output(self):
        token = "2001:0db8:0000:0000:0000:0000:0000:0001"
        assert matches(token, "neighbor 2001:db8::1 remote-as 64512")

    def test_compressed_token_matches_itself(self):
        assert matches("2001:db8::1", "peer 2001:db8::1 established")

    def test_case_insensitive(self):
        assert matches("2001:db8::abcd", "route 2001:DB8::ABCD/128")

    def test_does_not_match_a_longer_address(self):
        assert not matches("2001:db8::1", "2001:db8::11")

    def test_variants_include_the_canonical_form(self):
        variants = build_matcher(
            "2001:0db8:0000:0000:0000:0000:0000:0001"
        )["variants"]
        assert "2001:db8::1" in variants


# ---------------------------------------------------------------------------
# MAC — one keystroke, every representation
# ---------------------------------------------------------------------------

class TestMACMatching:

    CISCO = "0200.5e00.0001"
    COLON = "02:00:5e:00:00:01"
    DASH = "02-00-5e-00-00-01"
    BARE = "02005e000001"

    @pytest.mark.parametrize("token", [CISCO, COLON, DASH, BARE])
    @pytest.mark.parametrize("form", [CISCO, COLON, DASH, BARE])
    def test_any_input_format_finds_any_output_format(self, token, form):
        assert matches(token, f"MAC {form} learned on Ethernet1")

    def test_case_insensitive(self):
        assert matches("02005E000001", "0200.5e00.0001  DYNAMIC  Et1")

    def test_variants_expose_all_four_forms(self):
        variants = build_matcher(self.CISCO)["variants"]
        assert set(variants) == {self.CISCO, self.COLON, self.DASH, self.BARE}

    def test_does_not_match_a_different_mac(self):
        assert not matches(self.CISCO, "0200.5e00.0002 DYNAMIC Et2")

    def test_bare_form_does_not_match_inside_a_longer_hex_run(self):
        assert not matches(self.BARE, "ff02005e000001ff")


# ---------------------------------------------------------------------------
# ASN
# ---------------------------------------------------------------------------

class TestASNMatching:

    def test_prefixed_token_matches_prefixed_output(self):
        assert matches("AS64512", "peer group AS64512")

    def test_prefixed_token_matches_bare_output(self):
        assert matches("AS64512", "neighbor 192.0.2.1 remote-as 64512")

    def test_case_insensitive_prefix(self):
        assert matches("as64512", "AS64512")

    def test_does_not_match_inside_a_longer_asn(self):
        assert not matches("AS6451", "remote-as 64512")

    def test_does_not_match_a_different_asn(self):
        assert not matches("AS64512", "remote-as 65001")

    def test_variants_offer_both_forms(self):
        assert build_matcher("AS64512")["variants"] == ["AS64512", "64512"]


# ---------------------------------------------------------------------------
# Number and text
# ---------------------------------------------------------------------------

class TestNumberMatching:

    def test_matches_a_standalone_vlan_id(self):
        assert matches("10", "Vlan 10   ENG-LAB   active")

    def test_does_not_match_inside_a_longer_number(self):
        assert not matches("10", "Vlan 100  active")
        assert not matches("10", "Vlan 4010 active")

    def test_matches_when_adjacent_to_letters(self):
        """A vlan id shows up welded to a name in plenty of output."""
        assert matches("10", "Vlan10")


class TestTextMatching:

    def test_case_insensitive_substring(self):
        assert matches("eng-leaf", "Neighbor ENG-LEAF-01 on Ethernet1")

    def test_regex_metacharacters_are_escaped(self):
        """A token is a literal, not a pattern the operator has to escape."""
        assert matches("Ethernet49/1", "port Ethernet49/1 up")
        assert matches("uplink (primary)", "description: uplink (primary)")
        assert not matches("a.c", "abc")

    def test_no_match_returns_falsy(self):
        assert not matches("eng-spine", "Neighbor eng-leaf-01")


# ---------------------------------------------------------------------------
# Matcher contract
# ---------------------------------------------------------------------------

class TestMatcherContract:

    @pytest.mark.parametrize("token", [
        "192.0.2.1", "2001:db8::1", "0200.5e00.0001", "AS64512",
        "10", "eng-leaf-01", "",
    ])
    def test_shape_is_stable_for_every_kind(self, token):
        m = build_matcher(token)
        assert set(m) == {"kind", "regex", "variants"}
        assert m["kind"] in {"ipv4", "ipv6", "mac", "asn", "number", "text"}
        assert isinstance(m["variants"], list) and m["variants"]
        assert hasattr(m["regex"], "search")

    def test_surrounding_whitespace_is_stripped(self):
        assert build_matcher("  192.0.2.1  ")["kind"] == "ipv4"
        assert matches("  192.0.2.1  ", "host 192.0.2.1")