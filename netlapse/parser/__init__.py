"""
Netlapse Parser — Structured CLI output parsing via tfsm-fire.

The parse engine finds the best TextFSM template for raw CLI output
and returns structured records. The "output selects template" paradigm:
no manual template selection required.

Usage:
    from netlapse.parser import ParseEngine

    engine = ParseEngine()  # auto-finds ~/.netlapse/tfsm_templates.db
    engine.enrich_snapshot(snapshot, platform_profile="cisco_ios")
"""

from netlapse.parser.engine import (
    ParseEngine, ParseResult, UNPARSED_CAPTURE_TYPES, is_unparsed_capture,
)

__all__ = [
    "ParseEngine", "ParseResult", "UNPARSED_CAPTURE_TYPES", "is_unparsed_capture",
]