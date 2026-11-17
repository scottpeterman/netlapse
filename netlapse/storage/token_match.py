"""
Token matching engine for One Search (global cross-capture lookup).

Pure stdlib, zero netlapse imports — independently testable.

Given a single operator token (an IP, MAC, ASN, hostname, serial, etc.)
this builds a compiled regex that finds it *the way it actually appears in
device output*, accounting for the formatting differences that make a naive
substring search both miss real hits and report false ones:

  ipv4    octet-boundary anchored   10.0.0.1 must NOT match inside 10.0.0.10
  ipv6    canonicalized             match both as-typed and the compressed form
  mac     all vendor formats        aabb.ccdd.eeff / aa:bb:.. / aa-bb-.. / bare
  asn     with or without 'AS'      AS64512 and 'remote-as 64512'
  number  digit-boundary anchored   bare integers (vlan, etc.) without substrings
  text    case-insensitive substring hostnames, descriptions, free text

The classifier is deliberately conservative: anything it cannot confidently
type falls through to TEXT, a plain (escaped) substring match. Nothing here
raises on bad input — a token that does not parse simply becomes TEXT.

build_matcher(token) -> {
    "kind":     one of ipv4|ipv6|mac|asn|number|text,
    "regex":    compiled re.Pattern (already IGNORECASE where it matters),
    "variants": list[str] of the concrete forms being searched (for the UI),
}
"""

import ipaddress
import re
from typing import Dict, List

__all__ = ["classify", "build_matcher"]

_HEX12 = re.compile(r"^[0-9A-Fa-f]{12}$")
_MAC_SEPARATORS = re.compile(r"[.:\-\s]")
_ASN_PREFIXED = re.compile(r"^AS(\d{1,10})$", re.IGNORECASE)


def _mac_nibbles(token: str) -> str:
    """Return 12 lowercase hex nibbles if `token` is a MAC, else ''."""
    stripped = _MAC_SEPARATORS.sub("", token)
    if _HEX12.match(stripped):
        return stripped.lower()
    return ""


def classify(token: str) -> str:
    """Best-effort type of a single search token."""
    t = token.strip()
    if not t:
        return "text"

    # IP first — unambiguous when it parses.
    try:
        ip = ipaddress.ip_address(t)
        return "ipv4" if ip.version == 4 else "ipv6"
    except ValueError:
        pass

    if _mac_nibbles(t):
        return "mac"

    if _ASN_PREFIXED.match(t):
        return "asn"

    if t.isdigit():
        return "number"

    return "text"


def _ipv4_regex(t: str) -> re.Pattern:
    # An IPv4 literal must not be flanked by another digit or dot, so that
    # 10.0.0.1 does not match inside 10.0.0.10 or 10.1.1.11, yet still
    # matches 10.0.0.1/24, '10.0.0.1,' or 'host 10.0.0.1'.
    return re.compile(r"(?<![\d.])" + re.escape(t) + r"(?![\d.])")


def _ipv6_regex(t: str) -> (re.Pattern, List[str]):
    forms = {t}
    try:
        forms.add(str(ipaddress.IPv6Address(t)))  # canonical compressed form
    except ValueError:
        pass
    variants = sorted(forms, key=len)
    alt = "|".join(re.escape(f) for f in variants)
    # Hex/colon boundary so we don't match a fragment of a longer address.
    pat = r"(?<![0-9A-Fa-f:])(?:" + alt + r")(?![0-9A-Fa-f:])"
    return re.compile(pat, re.IGNORECASE), variants


def _mac_regex(nibbles: str) -> (re.Pattern, List[str]):
    h = nibbles  # 12 lowercase hex chars
    cisco = f"{h[0:4]}.{h[4:8]}.{h[8:12]}"
    colon = ":".join(h[i:i + 2] for i in range(0, 12, 2))
    dash = "-".join(h[i:i + 2] for i in range(0, 12, 2))
    bare = h
    variants = [cisco, colon, dash, bare]
    alt = "|".join(re.escape(v) for v in variants)
    pat = r"(?<![0-9A-Fa-f])(?:" + alt + r")(?![0-9A-Fa-f])"
    return re.compile(pat, re.IGNORECASE), variants


def _asn_regex(t: str) -> (re.Pattern, List[str]):
    n = _ASN_PREFIXED.match(t).group(1)
    variants = [f"AS{n}", n]
    # Match 'AS64512' and a bare '64512' (as in 'remote-as 64512'),
    # but not a substring of a longer number.
    pat = r"(?<![A-Za-z0-9])(?:AS)?" + re.escape(n) + r"(?![0-9])"
    return re.compile(pat, re.IGNORECASE), variants


def _number_regex(t: str) -> re.Pattern:
    return re.compile(r"(?<!\d)" + re.escape(t) + r"(?!\d)")


def _text_regex(t: str) -> re.Pattern:
    return re.compile(re.escape(t), re.IGNORECASE)


def build_matcher(token: str) -> Dict:
    """Classify `token` and build the regex + display variants for it."""
    t = token.strip()
    kind = classify(t)

    if kind == "ipv4":
        return {"kind": kind, "regex": _ipv4_regex(t), "variants": [t]}

    if kind == "ipv6":
        regex, variants = _ipv6_regex(t)
        return {"kind": kind, "regex": regex, "variants": variants}

    if kind == "mac":
        regex, variants = _mac_regex(_mac_nibbles(t))
        return {"kind": kind, "regex": regex, "variants": variants}

    if kind == "asn":
        regex, variants = _asn_regex(t)
        return {"kind": kind, "regex": regex, "variants": variants}

    if kind == "number":
        return {"kind": kind, "regex": _number_regex(t), "variants": [t]}

    return {"kind": "text", "regex": _text_regex(t), "variants": [t]}
