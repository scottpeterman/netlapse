"""
Volatility registry — which parsed fields (and raw config lines) are "noise"
for the semantic diff.

Loaded from definitions/volatility.yaml at first use and cached for the life of
the process. This is the diff-time policy layer. It decides two things:

  - which record COLUMNS a fresh diff excludes by default (age/uptime timers,
    counters), so a parseable capture doesn't report drift every poll just
    because a timer ticked; and
  - which raw config LINES to mask on the text diff (commit timestamps, the
    IOS ntp clock-period the router auto-tunes, ...), the analogue for captures
    that have no parsed records.

Both have a code baseline (BASELINE_HEURISTICS for fields, BASELINE_LINE_MASKS
for lines) that the YAML UNIONS on top — the floor can't be dropped by a stale
or missing file, the YAML only adds. It is NOT fleet state — nothing in the
scheduler acts on it — so unlike captures and jobs it lives in memory and is
never synced to the DB. Edit the YAML and restart to change the defaults; the
per-diff UI chips still override whatever the field side resolves. Fail-soft,
mirroring the registry loader: an absent file is inert (baseline only), and an
invalid file is logged and treated as empty so a typo never blocks diffing.

Field resolution for a (capture_type, schema):
    (heuristic glob matches on schema) ∪ (explicit volatile) − (keep) − key_fields
intersected with the schema, so only columns that actually exist are returned.
Field matching is case-insensitive against the TextFSM column names. Line masks
are regexes (case-sensitive), returned global-then-per-capture with no schema to
intersect against.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml

logger = logging.getLogger(__name__)

VOLATILITY_FILE = "volatility.yaml"


# Baseline name heuristics, applied FROM CODE so a stale or missing
# volatility.yaml can never drop them. The YAML's `heuristics:` list is UNIONED
# on top (purely additive); a per-capture `keep:` still rescues specific
# columns when a heuristic over-matches. This is the zero-curation floor: a
# device's obvious timers and counters classify themselves as volatile without
# any per-install file being current — which is exactly the failure this
# prevents (a seeded ~/.netlapse/volatility.yaml drifting behind the source and
# silently losing e.g. the *_TIME heuristic that catches OSPF DEAD_TIME).
BASELINE_HEURISTICS: List[str] = [
    "AGE", "AGE_*", "UPTIME", "*_TIME", "LAST_*", "DURATION", "IDLE*",
    "ELAPSED", "*_PACKETS", "*_BYTES", "*_RATE", "*_ERRORS", "*_DROPS",
    "*_COUNTER",
]


# Baseline config-LINE masks, applied FROM CODE — the line analogue of
# BASELINE_HEURISTICS. These blank whole raw-config lines whose value the device
# rewrites on its own (not parsed fields; there are no records for a config), so
# they don't read as drift every poll. They cover the sane, common cases across
# the shipping platforms; volatility.yaml's `line_masks:` is UNIONED on top. A
# stale or missing YAML can never drop this floor. Patterns are regexes, matched
# against raw lines and blanking the whole line's comparison key.
BASELINE_LINE_MASKS: List[str] = [
    r"!\s*Last configuration change.*",       # IOS
    r"!\s*NVRAM config last updated.*",        # IOS
    r"Current configuration\s*:\s*\d+ bytes",  # IOS
    r"ntp clock-period\s+\d+",                 # IOS — auto-tuned by the router, never operator-set
    r"## Last commit:.*",                      # Junos
    r"!\s*Time:.*",                            # EOS
    r"\d{2}:\d{2}:\d{2}\.\d+\s+\w+",           # bare timestamps
]


class VolatilityError(Exception):
    """Raised by the loader when volatility.yaml is malformed."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} error(s): " + "; ".join(errors)
        )


class VolatilityCatalog:
    """
    The parsed volatility policy. Construct via load_volatility_catalog().

    BASELINE_HEURISTICS are always applied (unioned in from code), so even an
    empty catalog — absent, empty, or malformed volatility.yaml — still excludes
    the obvious timers and counters. The YAML adds to that floor; it can't drop
    it. Per-capture `keep:` is the escape hatch when a heuristic over-matches.
    """

    def __init__(
        self,
        heuristics: Optional[List[str]] = None,
        explicit: Optional[Dict[str, Dict[str, Set[str]]]] = None,
        line_masks: Optional[List[str]] = None,
    ):
        # What the YAML asked for, kept separately so a startup check can report
        # drift from the shipped baseline (see baseline_drift()).
        self.declared_heuristics: List[str] = [
            h.upper() for h in (heuristics or [])
        ]
        # Effective patterns = code baseline ∪ YAML, upper-cased for
        # case-insensitive glob matching, de-duplicated, baseline first.
        seen: Set[str] = set()
        merged: List[str] = []
        for h in [*BASELINE_HEURISTICS, *(heuristics or [])]:
            hu = h.upper()
            if hu not in seen:
                seen.add(hu)
                merged.append(hu)
        self.heuristics: List[str] = merged
        # Effective global line masks = code baseline ∪ YAML `line_masks`,
        # de-duplicated, baseline first. Regexes (case-sensitive), NOT globs.
        self.declared_line_masks: List[str] = list(line_masks or [])
        seen_lm: Set[str] = set()
        merged_lm: List[str] = []
        for p in [*BASELINE_LINE_MASKS, *(line_masks or [])]:
            if p not in seen_lm:
                seen_lm.add(p)
                merged_lm.append(p)
        self.line_masks: List[str] = merged_lm
        # capture_type -> {"volatile": {names}, "keep": {names},
        #                  "line_masks": [regexes]}
        self.explicit: Dict[str, Dict[str, Set[str]]] = explicit or {}

    def resolve(
        self,
        capture_type: str,
        field_names,
        key_fields=None,
    ) -> List[str]:
        """
        The default fields to exclude for this capture type, given the columns
        actually present. Key fields are never excluded. Returns sorted column
        names drawn from `field_names` (so the result is always real columns).
        """
        fields = list(field_names or [])
        if not fields:
            return []
        keys = set(key_fields or [])

        # Case-insensitive lookup: declared/heuristic match on upper-case,
        # but the returned names are the real (original-case) columns.
        by_upper = {f.upper(): f for f in fields}

        matched: Set[str] = set()
        for up, original in by_upper.items():
            if any(fnmatch.fnmatchcase(up, pat) for pat in self.heuristics):
                matched.add(original)

        rules = self.explicit.get(capture_type, {})
        volatile = self._resolve_names(rules.get("volatile", set()), by_upper)
        keep = self._resolve_names(rules.get("keep", set()), by_upper)

        effective = (matched | volatile) - keep - keys
        return sorted(f for f in effective if f in fields)

    def resolve_line_masks(self, capture_type: str) -> List[str]:
        """
        Regex line-masks for a raw/config capture's text diff: the global set
        (code baseline ∪ YAML `line_masks`) plus any per-capture `line_masks`.

        Unlike fields there's no schema to intersect against — these match raw
        config lines — so every configured pattern is returned, global first
        then per-capture, de-duplicated. Order is cosmetic (each mask blanks a
        line independently).
        """
        out = list(self.line_masks)
        rules = self.explicit.get(capture_type, {})
        for p in rules.get("line_masks", []):
            if p not in out:
                out.append(p)
        return out

    @staticmethod
    def _resolve_names(declared, by_upper: Dict[str, str]) -> Set[str]:
        """Map declared column names to the real schema columns, case-insensitively."""
        out: Set[str] = set()
        for name in declared:
            actual = by_upper.get(str(name).upper())
            if actual is not None:
                out.add(actual)
        return out


def _bad_regex(pattern: str) -> Optional[str]:
    """Return the compile-error message if `pattern` is an invalid regex, else None."""
    try:
        re.compile(pattern)
        return None
    except re.error as e:
        return str(e)


def load_volatility_catalog(definitions_dir) -> VolatilityCatalog:
    """
    Read + validate volatility.yaml from the definitions directory.

    Absent file → empty catalog (no exclusions). Malformed file → VolatilityError
    with the full list of problems (the CLI surfaces these; the runtime accessor
    catches them and degrades to empty).
    """
    path = Path(definitions_dir) / VOLATILITY_FILE
    if not path.exists():
        return VolatilityCatalog()

    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise VolatilityError([f"{VOLATILITY_FILE}: invalid YAML: {e}"])

    if data is None:
        return VolatilityCatalog()
    if not isinstance(data, dict):
        raise VolatilityError([
            f"{VOLATILITY_FILE}: top level must be a mapping, "
            f"got {type(data).__name__}"
        ])

    errors: List[str] = []

    heur = data.get("heuristics", []) or []
    if not isinstance(heur, list) or not all(isinstance(h, str) for h in heur):
        errors.append(f"{VOLATILITY_FILE}: 'heuristics' must be a list of strings")
        heur = []

    line_masks = data.get("line_masks", []) or []
    if not isinstance(line_masks, list) or not all(isinstance(p, str) for p in line_masks):
        errors.append(f"{VOLATILITY_FILE}: 'line_masks' must be a list of strings")
        line_masks = []
    else:
        for p in line_masks:
            bad = _bad_regex(p)
            if bad:
                errors.append(f"{VOLATILITY_FILE}: line_masks: invalid regex {p!r} ({bad})")

    explicit: Dict[str, Dict[str, Set[str]]] = {}
    caps = data.get("captures", {}) or {}
    if not isinstance(caps, dict):
        errors.append(f"{VOLATILITY_FILE}: 'captures' must be a mapping")
        caps = {}

    for ctype, body in caps.items():
        if body is None:
            explicit[str(ctype)] = {"volatile": set(), "keep": set()}
            continue
        if not isinstance(body, dict):
            errors.append(
                f"{VOLATILITY_FILE}: captures.{ctype} must be a mapping"
            )
            continue
        rule: Dict[str, Set[str]] = {}
        for key in ("volatile", "keep"):
            val = body.get(key, []) or []
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                errors.append(
                    f"{VOLATILITY_FILE}: captures.{ctype}.{key} "
                    f"must be a list of strings"
                )
                val = []
            rule[key] = set(val)
        # Per-capture line masks (regexes for the raw/config text diff). Kept as
        # an ordered list, not a set — order is cosmetic but lists are simpler
        # to reason about than a set of patterns.
        lm = body.get("line_masks", []) or []
        if not isinstance(lm, list) or not all(isinstance(x, str) for x in lm):
            errors.append(
                f"{VOLATILITY_FILE}: captures.{ctype}.line_masks "
                f"must be a list of strings"
            )
            lm = []
        else:
            for p in lm:
                bad = _bad_regex(p)
                if bad:
                    errors.append(
                        f"{VOLATILITY_FILE}: captures.{ctype}.line_masks: "
                        f"invalid regex {p!r} ({bad})"
                    )
        rule["line_masks"] = list(lm)
        unknown = set(body.keys()) - {"volatile", "keep", "line_masks"}
        if unknown:
            errors.append(
                f"{VOLATILITY_FILE}: captures.{ctype} has unknown key(s): "
                f"{', '.join(sorted(unknown))}"
            )
        explicit[str(ctype)] = rule

    if errors:
        raise VolatilityError(errors)

    return VolatilityCatalog(heuristics=heur, explicit=explicit, line_masks=line_masks)


# ---------------------------------------------------------------------------
# Cached runtime accessor
# ---------------------------------------------------------------------------

_CACHE: Dict[str, VolatilityCatalog] = {}

# Set once at daemon startup from the resolved config so the volatility loader
# reads the SAME definitions directory as captures/jobs. Without this, a
# backend call (which has no app config in scope) would fall back to
# resolve_definitions_dir(None) and could read a different directory than the
# one config['definitions_dir'] points the rest of the registry at.
_CONFIGURED_DIR: Optional[Path] = None


def set_definitions_dir(path) -> None:
    """
    Register the definitions directory the daemon resolved from its config.

    Call once at startup (app lifespan). Clears the cache so a prior no-config
    lookup can't shadow the configured directory.
    """
    global _CONFIGURED_DIR
    _CONFIGURED_DIR = Path(path).expanduser() if path else None
    _CACHE.clear()


def get_volatility_catalog(
    config: Optional[dict] = None,
    definitions_dir=None,
) -> VolatilityCatalog:
    """
    The process-wide volatility catalog, loaded once per definitions dir.

    Directory resolution order:
      1. explicit definitions_dir argument (tests / CLI)
      2. the dir registered at startup via set_definitions_dir() — i.e. exactly
         what the daemon's config resolved, including config['definitions_dir']
      3. resolve_definitions_dir(config) as a last resort

    Fail-soft: a malformed file logs a warning and yields an empty catalog, so
    the semantic diff still runs (just with no default exclusions). Edits take
    effect on restart, consistent with the rest of the definition registry.
    """
    from .service import resolve_definitions_dir

    if definitions_dir is not None:
        base = Path(definitions_dir)
    elif _CONFIGURED_DIR is not None:
        base = _CONFIGURED_DIR
    else:
        base = resolve_definitions_dir(config)
    key = str(base)

    catalog = _CACHE.get(key)
    if catalog is None:
        try:
            catalog = load_volatility_catalog(base)
        except VolatilityError as e:
            logger.warning(
                "%s invalid — no default field exclusions applied: %s",
                VOLATILITY_FILE, e,
            )
            catalog = VolatilityCatalog()
        _CACHE[key] = catalog
    return catalog


def reset_volatility_cache() -> None:
    """Clear the cached catalog (tests, or a future live-reload hook)."""
    _CACHE.clear()

def baseline_drift(catalog: "VolatilityCatalog") -> List[str]:
    """
    Baseline heuristics the loaded volatility.yaml did NOT itself declare — i.e.
    the ones now supplied only by the code baseline. A non-empty result means
    the on-disk file has drifted behind the shipped baseline (the classic
    seed-if-not-exists staleness). It is INFORMATIONAL: the baseline is applied
    regardless, so filtering is correct either way. app.py logs this at startup
    so a stale file announces itself instead of silently under-filtering.
    """
    declared = set(catalog.declared_heuristics)
    return [h for h in BASELINE_HEURISTICS if h.upper() not in declared]