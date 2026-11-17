import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any

import yaml

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Result types
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SyncResult:
    """Outcome of a map sync operation."""
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    sites_created: List[str] = field(default_factory=list)
    roles_created: List[str] = field(default_factory=list)
    devices: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped

    def summary(self) -> str:
        lines = [
            f"Sync complete: {self.total} devices processed",
            f"  Created: {self.created}",
            f"  Updated: {self.updated}",
            f"  Skipped: {self.skipped}",
        ]
        if self.sites_created:
            lines.append(f"  Sites created: {', '.join(self.sites_created)}")
        if self.roles_created:
            lines.append(f"  Roles created: {', '.join(self.roles_created)}")
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for err in self.errors[:10]:
                lines.append(f"    - {err}")
        return "\n".join(lines)




_DOT_SITE_PATTERN = re.compile(
    r'^(?P<shortname>[^.]+)\.(?P<site>[a-z]{2,5}\d{1,2})(?:\.|$)'
)

_DASH_SITE_PATTERN = re.compile(
    r'^(?P<site>[a-z]{2,5})-(?P<rest>.+)$', re.IGNORECASE
)


_IPV4_KEY_PATTERN = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')


def parse_hostname(
    fqdn: str,
    site_override: Optional[str] = None,
) -> Tuple[str, str]:

    if _IPV4_KEY_PATTERN.match(fqdn):
        return fqdn, site_override or "unknown"

    # Try dot-separated first (more specific)
    m = _DOT_SITE_PATTERN.match(fqdn)
    if m:
        shortname = m.group("shortname")
        site = m.group("site")
        # Preserve {shortname}.{site} as device name — strip only domain
        name = f"{shortname}.{site}"
        return name, site_override or site

    # Try dash-prefixed (campus convention)
    # Use the raw hostname (before any dots) as the device name
    name = fqdn.split(".")[0]
    m = _DASH_SITE_PATTERN.match(name)
    if m:
        return name, site_override or m.group("site").lower()

    # Fallback
    return name, site_override or "unknown"


# ═══════════════════════════════════════════════════════════════════════════
# Platform parsing
# ═══════════════════════════════════════════════════════════════════════════

def parse_platform(platform_str: str) -> Tuple[str, str, str]:
    """
    Parse SC2 platform string into (manufacturer_slug, platform_slug, version).

    SC2 captures the platform from show version / system info during
    discovery. The strings are free-form but follow consistent patterns:

        "Arista EOS 4.33.1.1F"                       → (arista, arista_eos, 4.33.1.1F)
        "Arista DCS-7280SRA-48C6-F EOS 4.33.1.1F"    → (arista, arista_eos, 4.33.1.1F)
        "Juniper JUNOS 14.1X53-D35" → (juniper, juniper_junos, 14.1X53-D35)
        "Cisco IOS 12.2(54)SG1"     → (cisco, cisco_ios, 12.2(54)SG1)
        "Cisco NX-OS 9.3(8)"        → (cisco, cisco_nxos, 9.3(8))
        "Cisco"                     → (cisco, cisco_ios, "")

    Returns:
        (manufacturer_slug, platform_slug, version_string)
    """
    if not platform_str:
        return "unknown", "unknown", ""

    p = platform_str.strip().rstrip(",")

    # Arista: SC2 may emit either "Arista EOS 4.33.1.1F" or the full
    # chassis string "Arista DCS-7280SRA-48C6-F EOS 4.33.1.1F". Both
    # start with "Arista" and contain "EOS" — extract version after EOS.
    if p.startswith("Arista"):
        eos_idx = p.find(" EOS ")
        if eos_idx != -1:
            ver = p[eos_idx + 5:].strip()
        else:
            # Bare "Arista" or "Arista EOS" with no version
            ver = p.replace("Arista", "").strip()
        return "arista", "arista_eos", ver

    if p.startswith("Juniper JUNOS"):
        ver = p.replace("Juniper JUNOS", "").strip()
        return "juniper", "juniper_junos", ver

    if p.startswith("Cisco NX-OS"):
        ver = p.replace("Cisco NX-OS", "").strip()
        return "cisco", "cisco_nxos", ver

    if p.startswith("Cisco IOS-XE") or p.startswith("Cisco IOS XE"):
        ver = re.sub(r"^Cisco IOS[- ]?XE\s*", "", p).strip()
        return "cisco", "cisco_ios_xe", ver

    if p.startswith("Cisco IOS-XR") or p.startswith("Cisco IOS XR"):
        ver = re.sub(r"^Cisco IOS[- ]?XR\s*", "", p).strip()
        return "cisco", "cisco_ios_xr", ver

    if p.startswith("Cisco IOS"):
        ver = p.replace("Cisco IOS", "").strip()
        return "cisco", "cisco_ios", ver

    if p.startswith("Cisco ASA") or p.startswith("Cisco Adaptive"):
        ver = re.sub(r"^Cisco (ASA|Adaptive[^,]*)", "", p).strip()
        return "cisco", "cisco_asa", ver

    if p.startswith("Cisco"):
        # Bare "Cisco 15.2(7)E5" or "Cisco 12.2(55)SE12" — SC2 sometimes
        # omits "IOS" from older device platform strings.
        ver = p.replace("Cisco", "").strip()
        return "cisco", "cisco_ios", ver

    if p.startswith("Palo Alto") or p.startswith("PAN-OS"):
        return "palo-alto", "paloalto_panos", ""

    if p.startswith("Fortinet") or p.startswith("FortiGate"):
        return "fortinet", "fortinet_fortios", ""

    if p.startswith("HP") or p.startswith("Aruba"):
        return "hp", "hp_procurve", ""

    # Linux/unknown — not a network device platform, but the map
    # may contain them if SC2 reached them via LLDP
    if "Linux" in p or "Debian" in p or "Ubuntu" in p:
        return "unknown", "linux", p

    return "unknown", "unknown", p


# ═══════════════════════════════════════════════════════════════════════════
# Role inference
# ═══════════════════════════════════════════════════════════════════════════
#
# Role inference maps a hostname to a device-role slug. It is a pure function
# of the hostname, applied once at map-import time. The built-in rules below
# encode the datacenter/SP and campus conventions netlapse was first written
# against — but naming standards are a per-operator thing, so the rules are
# overridable from a `roles.yaml` definition file (sibling of captures.yaml /
# jobs.yaml). Absent file → these built-ins apply unchanged. Present file →
# the operator's rules govern (and may opt to keep these as a fallback layer
# via `extend_builtins: true`). See load_role_rules() and definitions/roles.yaml.

# Hostname prefix → role slug mapping (datacenter/SP convention).
# Order matters: first match wins. More specific prefixes before general.
# These map to the seeded DEFAULT_DEVICE_ROLES where possible.
DEFAULT_ROLE_MAP: List[Tuple[str, str]] = [
    ("border", "router"),           # Border routers
    ("edge",   "router"),           # Edge routers
    ("peer",   "router"),           # Peering routers
    ("spine",  "spine"),            # Spine switches
    ("leaf",   "leaf"),             # Leaf switches
    ("tor",    "leaf"),             # Top-of-rack switches
    ("agg",    "distribution"),     # Aggregation switches
    ("core",   "core"),             # Core routers/switches
    ("oob",    "access"),           # OOB management switches
]

# Hostname segment → role slug mapping (campus/enterprise convention).
# Matches anywhere in dash-separated hostname segments.
# e.g. site1-core-01 contains segment "core" → core role.
DEFAULT_SEGMENT_ROLE_MAP: List[Tuple[str, str]] = [
    ("-core-",  "core"),            # Core routers/switches
    ("-rtr-",   "router"),          # Routers
    ("-wan-",   "router"),          # WAN routers
    ("-fw-",    "firewall"),        # Firewalls
    ("-sw-",    "access"),          # Switch layer (access)
    ("-dist-",  "distribution"),    # Distribution
    ("-agg-",   "distribution"),    # Aggregation
    ("-spine-", "spine"),           # Spine
    ("-leaf-",  "leaf"),            # Leaf
]

DEFAULT_FALLBACK_ROLE = "switch"


@dataclass
class RoleRules:
    """
    Hostname → role-slug inference rules.

    Two ordered strategies, tried in sequence (first match wins within each,
    prefix before segment), exactly as the original built-ins behaved:
      1. prefix:  hostname STARTS WITH the match string  ("border01" → router)
      2. segment: match string appears ANYWHERE in the hostname ("site1-core-01"
                  contains "-core-" → core)
    If neither hits, `fallback` is returned.

    The built-ins live in DEFAULT_ROLE_RULES; an operator's roles.yaml builds a
    RoleRules that either replaces or extends them (see load_role_rules).
    """
    prefix: List[Tuple[str, str]] = field(default_factory=list)
    segment: List[Tuple[str, str]] = field(default_factory=list)
    fallback: str = DEFAULT_FALLBACK_ROLE

    def role_slugs(self) -> List[str]:
        """Every role slug this ruleset can produce (incl. fallback)."""
        slugs = {r for _, r in self.prefix} | {r for _, r in self.segment}
        slugs.add(self.fallback)
        return sorted(slugs)


# The shipped defaults, as a RoleRules. This is what infer_role() uses when no
# operator override is supplied — i.e. behavior is identical to before unless a
# roles.yaml is present.
DEFAULT_ROLE_RULES = RoleRules(
    prefix=list(DEFAULT_ROLE_MAP),
    segment=list(DEFAULT_SEGMENT_ROLE_MAP),
    fallback=DEFAULT_FALLBACK_ROLE,
)


def _coerce_role_rules(
    rules: Optional["RoleRules | List[Tuple[str, str]]"],
) -> RoleRules:
    """
    Normalize the legacy and current override forms into a RoleRules.

    Historically infer_role()/parse_map() accepted `role_map`: a bare list of
    (prefix, role) tuples that overrode ONLY the prefix strategy while the
    segment strategy stayed on the built-ins. That form is still honored so
    older callers don't break: a list → prefix override + built-in segments.
    A RoleRules is used as-is. None → DEFAULT_ROLE_RULES.
    """
    if rules is None:
        return DEFAULT_ROLE_RULES
    if isinstance(rules, RoleRules):
        return rules
    if isinstance(rules, list):
        return RoleRules(
            prefix=list(rules),
            segment=list(DEFAULT_SEGMENT_ROLE_MAP),
            fallback=DEFAULT_FALLBACK_ROLE,
        )
    raise TypeError(
        f"role rules must be RoleRules, list, or None — got {type(rules).__name__}"
    )


def infer_role(
    short_name: str,
    role_rules: Optional["RoleRules | List[Tuple[str, str]]"] = None,
) -> str:
    """
    Infer device role from hostname.

    Two strategies, tried in order:
      1. Prefix matching (datacenter/SP): "border01" → router
      2. Segment matching (campus): "site1-core-01" → core

    Args:
        short_name: Device short name (e.g., "border01", "site1-core-01").
        role_rules: Optional override. Accepts a RoleRules (current form) or a
                    bare list of (prefix, role) tuples (legacy form: overrides
                    only the prefix strategy). None → built-in DEFAULT_ROLE_RULES.

    Returns:
        Role slug from the rules, or the ruleset's fallback (built-in: "switch").
    """
    rules = _coerce_role_rules(role_rules)
    lower = short_name.lower()

    # Strategy 1: prefix match (datacenter convention)
    for prefix, role in rules.prefix:
        if prefix and lower.startswith(prefix):
            return role

    # Strategy 2: segment match (campus convention) — only if prefix didn't hit
    for segment, role in rules.segment:
        if segment and segment in lower:
            return role

    return rules.fallback


# ───────────────────────────────────────────────────────────────────────────
# roles.yaml loader
# ───────────────────────────────────────────────────────────────────────────

ROLE_RULES_FILE = "roles.yaml"


def _parse_rule_list(
    raw: Any,
    section: str,
    errors: List[str],
    warnings: List[str],
) -> List[Tuple[str, str]]:
    """
    Parse one ordered rule list (prefix or segment) into [(match, role), ...].

    Accepts either form, so the file can be terse or explicit:
        - {match: "border", role: router}
        - border: router            # single-key mapping shorthand
    Order is preserved (first match wins), so a YAML *list* is required — a
    bare mapping can't guarantee order across all loaders.
    """
    out: List[Tuple[str, str]] = []
    if raw is None:
        return out
    if not isinstance(raw, list):
        errors.append(
            f"roles.yaml: '{section}' must be a list of rules "
            f"(got {type(raw).__name__}); order matters, so a list is required"
        )
        return out

    for i, item in enumerate(raw):
        match: Any
        role: Any
        if isinstance(item, dict) and "match" in item and "role" in item:
            match, role = item["match"], item["role"]
        elif isinstance(item, dict) and len(item) == 1 and \
                not ({"match", "role"} & set(item)):
            # Shorthand 'match: role' — but only when the single key isn't the
            # reserved word 'match'/'role' (that's a half-written explicit rule,
            # e.g. {match: border} with the role forgotten — catch it, don't
            # silently treat "match" as a hostname prefix).
            (match, role), = item.items()
        else:
            errors.append(
                f"roles.yaml: {section}[{i}] must be "
                f"{{match: <str>, role: <slug>}} or a single 'match: role' pair"
            )
            continue

        if not isinstance(match, str) or not match.strip():
            errors.append(f"roles.yaml: {section}[{i}].match must be a non-empty string")
            continue
        if not isinstance(role, str) or not role.strip():
            errors.append(f"roles.yaml: {section}[{i}].role must be a non-empty slug")
            continue

        # Degenerate-match guard. A 1-char match (esp. a bare "-") swallows
        # almost everything and short-circuits every rule below it — the exact
        # footgun the original built-in segment map shipped with. Allow it, but
        # make it loud.
        if section == "segment" and len(match.strip()) < 2:
            warnings.append(
                f"roles.yaml: {section}[{i}].match='{match}' is very short and "
                f"will match almost any hostname, shadowing the rules after it "
                f"(first match wins) — did you mean something more specific?"
            )

        out.append((match, role))
    return out


def load_role_rules(
    path: str | Path,
    known_roles: Optional[List[str]] = None,
) -> Optional[RoleRules]:
    """
    Load operator role-inference rules from a roles.yaml file.

    Mirrors the captures/jobs registry contract:
      • Absent file  → None (caller falls back to DEFAULT_ROLE_RULES). NOT an
                       error — the built-ins are the zero-config behavior.
      • Invalid file → CatalogError-style: raises ValueError with every problem
                       collected, so a typo never half-applies a ruleset.

    File shape (version 1)::

        version: 1
        role_inference:
          extend_builtins: false        # true → append built-ins as a fallback layer
          fallback: switch              # role when nothing matches
          prefix:                       # ordered; hostname STARTS WITH match
            - {match: "border", role: router}
            - {match: "tor",    role: leaf}
          segment:                      # ordered; match appears ANYWHERE
            - {match: "-core-", role: core}
            - {match: "-fw-",   role: firewall}

    `extend_builtins: true` appends DEFAULT_ROLE_MAP / DEFAULT_SEGMENT_ROLE_MAP
    AFTER the operator's rules (so operator rules win on conflict, built-ins
    catch the rest). When false (default), the operator's rules are the whole
    ruleset and the built-ins are not consulted.

    `known_roles` (optional, e.g. slugs from dcim_device_role) only drives an
    advisory warning when a rule names a role that doesn't exist yet — sync
    auto-creates missing roles, so it is not an error.
    """
    p = Path(path).expanduser()
    if not p.exists():
        return None

    errors: List[str] = []
    warnings: List[str] = []

    try:
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"roles.yaml: invalid YAML: {e}") from e

    if data is None:
        # Present-but-empty: treat as "no overrides", same as absent.
        logger.info("roles.yaml at %s is empty — using built-in role inference", p)
        return None
    if not isinstance(data, dict):
        raise ValueError(
            f"roles.yaml: top level must be a mapping, got {type(data).__name__}"
        )

    section = data.get("role_inference")
    if section is None:
        raise ValueError("roles.yaml: missing required 'role_inference' section")
    if not isinstance(section, dict):
        raise ValueError("roles.yaml: 'role_inference' must be a mapping")

    extend = bool(section.get("extend_builtins", False))

    fallback = section.get("fallback", DEFAULT_FALLBACK_ROLE)
    if not isinstance(fallback, str) or not fallback.strip():
        errors.append("roles.yaml: 'fallback' must be a non-empty role slug")
        fallback = DEFAULT_FALLBACK_ROLE

    prefix = _parse_rule_list(section.get("prefix"), "prefix", errors, warnings)
    segment = _parse_rule_list(section.get("segment"), "segment", errors, warnings)

    if not prefix and not segment:
        errors.append(
            "roles.yaml: define at least one 'prefix' or 'segment' rule "
            "(or remove the file to use the built-in defaults)"
        )

    if extend:
        # Operator rules first (win on conflict), built-ins appended as fallback.
        prefix = prefix + list(DEFAULT_ROLE_MAP)
        segment = segment + list(DEFAULT_SEGMENT_ROLE_MAP)

    if errors:
        raise ValueError(
            "roles.yaml has "
            f"{len(errors)} error(s):\n  - " + "\n  - ".join(errors)
        )

    rules = RoleRules(prefix=prefix, segment=segment, fallback=fallback)

    # Advisory: rules pointing at roles that don't exist yet. Harmless (sync's
    # _ensure_role creates them), but a typo'd slug is worth surfacing.
    if known_roles is not None:
        known = {r.lower() for r in known_roles}
        unknown = sorted(
            {r for r in rules.role_slugs() if r.lower() not in known}
        )
        for slug in unknown:
            warnings.append(
                f"roles.yaml: role '{slug}' is not in dcim_device_role yet — "
                f"it will be auto-created on sync (typo?)"
            )

    for w in warnings:
        logger.warning("Role inference: %s", w)

    logger.info(
        "Loaded role inference from %s: %d prefix + %d segment rule(s), "
        "fallback=%s%s",
        p, len(rules.prefix), len(rules.segment), rules.fallback,
        " (extending built-ins)" if extend else "",
    )
    return rules


# ═══════════════════════════════════════════════════════════════════════════
# Legacy SSH detection
# ═══════════════════════════════════════════════════════════════════════════

def needs_legacy_ssh(platform_slug: str, version: str) -> bool:
    """
    Heuristic: does this platform/version need legacy SSH algorithms?

    Cisco IOS 12.x and early 15.x often need DH group1, 3DES, ssh-dss.
    Old ProCurve/Comware similarly. Juniper and Arista generally don't.

    This is advisory — the platform table's legacy_ssh field is the
    authoritative setting. This just flags devices where the operator
    should check.
    """
    if platform_slug == "cisco_ios" and version:
        # IOS 12.x always needs legacy
        if version.startswith("12."):
            return True
        # IOS 15.0 often does too
        if version.startswith("15.0"):
            return True

    if platform_slug in ("hp_procurve", "hp_comware"):
        return True

    return False


# ═══════════════════════════════════════════════════════════════════════════
# Map loading and parsing
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ParsedDevice:
    """A device extracted from the topology map, ready for DCIM import."""
    fqdn: str                   # Original map key
    short_name: str             # Hostname without site/domain
    site_slug: str              # Extracted site (site1, site2, etc.)
    ip: str                     # Management IP from discovery
    platform_str: str           # Raw platform string from SC2
    manufacturer_slug: str      # Parsed manufacturer
    platform_slug: str          # Parsed platform (matches dcim_platform)
    platform_version: str       # Extracted version string
    role_slug: str              # Inferred role
    legacy_ssh_hint: bool       # Heuristic: may need legacy algorithms
    peer_count: int             # Number of discovered peers


def load_map(map_path: str | Path) -> Dict[str, Any]:
    """Load and validate a topology map.json file."""
    path = Path(map_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Map file not found: {path}")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected dict (hostname → node data), got {type(data).__name__}"
        )

    return data


def parse_map(
    data: Dict[str, Any],
    role_rules: "Optional[RoleRules | List[Tuple[str, str]]]" = None,
    exclude_prefixes: Optional[List[str]] = None,
    site_override: Optional[str] = None,
) -> List[ParsedDevice]:
    """
    Parse a topology map into ParsedDevice records.

    Args:
        data: Raw map.json dict (hostname → {node_details, peers}).
        role_rules: Optional role-inference override — a RoleRules (e.g. from
                    load_role_rules) or the legacy bare prefix list. None uses
                    the built-in DEFAULT_ROLE_RULES.
        exclude_prefixes: Hostname prefixes to skip (e.g., ["sping"]
                          to exclude sping switches).
        site_override: Force all devices to this site slug
                       (bypasses hostname-based extraction).

    Returns:
        List of ParsedDevice records, sorted by site then name.
    """
    devices = []
    exclude = [p.lower() for p in (exclude_prefixes or [])]

    for fqdn, node in data.items():
        short_name, site_slug = parse_hostname(fqdn, site_override)

        # Skip excluded prefixes
        if any(short_name.lower().startswith(p) for p in exclude):
            continue

        node_details = node.get("node_details", {})
        ip = node_details.get("ip", "")
        platform_str = node_details.get("platform", "")
        peer_count = len(node.get("peers", {}))

        mfg_slug, plat_slug, version = parse_platform(platform_str)
        role = infer_role(short_name, role_rules)
        legacy_hint = needs_legacy_ssh(plat_slug, version)

        devices.append(ParsedDevice(
            fqdn=fqdn,
            short_name=short_name,
            site_slug=site_slug,
            ip=ip,
            platform_str=platform_str,
            manufacturer_slug=mfg_slug,
            platform_slug=plat_slug,
            platform_version=version,
            role_slug=role,
            legacy_ssh_hint=legacy_hint,
            peer_count=peer_count,
        ))

    # Sort by site, then name for deterministic output
    devices.sort(key=lambda d: (d.site_slug, d.short_name))
    return devices


# ═══════════════════════════════════════════════════════════════════════════
# DCIM sync
# ═══════════════════════════════════════════════════════════════════════════

def sync_from_map(
    db,
    map_path: str | Path,
    role_rules: "Optional[RoleRules | List[Tuple[str, str]]]" = None,
    exclude_prefixes: Optional[List[str]] = None,
    site_override: Optional[str] = None,
    collection_enabled: bool = True,
    dry_run: bool = False,
) -> SyncResult:
    """
    Import/sync devices from a Secure Cartography topology map.

    Idempotent: creates sites and roles as needed, upserts devices.
    Safe to call after every SC2 discovery cycle.

    Args:
        db: NetlapseDB instance (must be initialized).
        map_path: Path to map.json file.
        role_rules: Optional role-inference override — a RoleRules (from
                    load_role_rules) or the legacy bare prefix list. None uses
                    the built-in DEFAULT_ROLE_RULES.
        exclude_prefixes: Hostname prefixes to skip.
        site_override: Force all devices to this site slug.
        collection_enabled: Default collection_enabled for new devices.
        dry_run: If True, parse and report but don't write to DB.

    Returns:
        SyncResult with counts and details.
    """
    result = SyncResult()

    # Load and parse
    data = load_map(map_path)
    devices = parse_map(data, role_rules, exclude_prefixes, site_override)
    logger.info("Parsed %d devices from %s", len(devices), map_path)

    if dry_run:
        for dev in devices:
            result.devices.append({
                "name": dev.short_name,
                "site": dev.site_slug,
                "ip": dev.ip,
                "platform": dev.platform_slug,
                "role": dev.role_slug,
                "legacy_ssh_hint": dev.legacy_ssh_hint,
                "peers": dev.peer_count,
            })
            result.skipped += 1
        return result

    conn = db.conn

    # ── Resolve platform IDs ─────────────────────────────────────────
    # Map platform_slug → platform_id from the seeded dcim_platform table
    platform_map = {}
    for row in conn.execute(
        "SELECT slug, id FROM dcim_platform"
    ).fetchall():
        platform_map[row[0]] = row[1]

    # ── Resolve role IDs ─────────────────────────────────────────────
    role_map_db = {}
    for row in conn.execute(
        "SELECT slug, id FROM dcim_device_role"
    ).fetchall():
        role_map_db[row[0]] = row[1]

    # ── Process each device ──────────────────────────────────────────
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for dev in devices:
        try:
            # Ensure site exists
            site_id = _ensure_site(conn, dev.site_slug, result)

            # Ensure role exists
            role_id = _ensure_role(conn, dev.role_slug, role_map_db, result)

            # Resolve platform_id (use seeded platforms, skip if unknown)
            platform_id = platform_map.get(dev.platform_slug)
            if platform_id is None and dev.platform_slug != "unknown":
                logger.warning(
                    "Platform '%s' not in DCIM for device %s — "
                    "device will be created without platform assignment",
                    dev.platform_slug, dev.fqdn,
                )

            # Check if device already exists (by name + site)
            existing = conn.execute(
                "SELECT id, primary_ip4, platform_id, role_id "
                "FROM dcim_device WHERE name = ? AND site_id = ?",
                (dev.short_name, site_id),
            ).fetchone()

            if existing:
                # Update if anything changed
                changes = []
                params = []

                if existing[1] != dev.ip and dev.ip:
                    changes.append("primary_ip4 = ?")
                    params.append(dev.ip)

                if platform_id and existing[2] != platform_id:
                    changes.append("platform_id = ?")
                    params.append(platform_id)

                if role_id and existing[3] != role_id:
                    changes.append("role_id = ?")
                    params.append(role_id)

                if changes:
                    changes.append("updated_at = ?")
                    params.append(now_iso)
                    params.append(existing[0])

                    conn.execute(
                        f"UPDATE dcim_device SET {', '.join(changes)} "
                        f"WHERE id = ?",
                        params,
                    )
                    result.updated += 1
                    logger.debug(
                        "Updated %s/%s: %s",
                        dev.site_slug, dev.short_name,
                        ", ".join(changes[:-1]),
                    )
                else:
                    result.skipped += 1
            else:
                # Insert new device
                conn.execute(
                    "INSERT INTO dcim_device "
                    "(name, site_id, platform_id, role_id, "
                    " primary_ip4, collection_enabled, status, "
                    " description, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        dev.short_name,
                        site_id,
                        platform_id,
                        role_id,
                        dev.ip,
                        1 if collection_enabled else 0,
                        f"SC2 discovery: {dev.platform_str}",
                        now_iso,
                        now_iso,
                    ),
                )
                result.created += 1
                logger.info(
                    "Created %s/%s (%s) ip=%s role=%s",
                    dev.site_slug, dev.short_name,
                    dev.platform_slug, dev.ip, dev.role_slug,
                )

            # Track for reporting
            result.devices.append({
                "name": dev.short_name,
                "site": dev.site_slug,
                "ip": dev.ip,
                "platform": dev.platform_slug,
                "role": dev.role_slug,
                "legacy_ssh_hint": dev.legacy_ssh_hint,
                "peers": dev.peer_count,
            })

        except Exception as e:
            error_msg = f"{dev.fqdn}: {e}"
            result.errors.append(error_msg)
            logger.error("Failed to import %s: %s", dev.fqdn, e)

    conn.commit()

    # ── Log legacy SSH hints ─────────────────────────────────────────
    legacy_devices = [d for d in devices if d.legacy_ssh_hint]
    if legacy_devices:
        logger.warning(
            "%d device(s) may need legacy SSH algorithms (DH group1, "
            "3DES). Check platform legacy_ssh setting for: %s",
            len(legacy_devices),
            ", ".join(d.short_name for d in legacy_devices),
        )

    logger.info(result.summary())
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════

# Cache site_slug → site_id within a sync run
_site_cache: Dict[str, int] = {}
_role_cache: Dict[str, int] = {}


def _ensure_site(conn, site_slug: str, result: SyncResult) -> int:
    """Get or create a site by slug. Returns site_id."""
    if site_slug in _site_cache:
        return _site_cache[site_slug]

    row = conn.execute(
        "SELECT id FROM dcim_site WHERE slug = ?", (site_slug,)
    ).fetchone()

    if row:
        _site_cache[site_slug] = row[0]
        return row[0]

    # Create the site
    # Generate a human-friendly name from the slug
    site_name = site_slug.upper()  # site1 → site1
    conn.execute(
        "INSERT OR IGNORE INTO dcim_site (name, slug, status) "
        "VALUES (?, ?, 'active')",
        (site_name, site_slug),
    )
    row = conn.execute(
        "SELECT id FROM dcim_site WHERE slug = ?", (site_slug,)
    ).fetchone()

    site_id = row[0]
    _site_cache[site_slug] = site_id
    result.sites_created.append(site_slug)
    logger.info("Created site: %s (%s)", site_name, site_slug)
    return site_id


def _ensure_role(
    conn,
    role_slug: str,
    role_map_db: Dict[str, int],
    result: SyncResult,
) -> Optional[int]:
    """Get or create a device role by slug. Returns role_id."""
    if role_slug in _role_cache:
        return _role_cache[role_slug]

    if role_slug in role_map_db:
        _role_cache[role_slug] = role_map_db[role_slug]
        return role_map_db[role_slug]

    # Create missing role
    role_name = role_slug.replace("-", " ").title()
    conn.execute(
        "INSERT OR IGNORE INTO dcim_device_role (name, slug, color) "
        "VALUES (?, ?, '95a5a6')",
        (role_name, role_slug),
    )
    row = conn.execute(
        "SELECT id FROM dcim_device_role WHERE slug = ?", (role_slug,)
    ).fetchone()

    if row:
        role_id = row[0]
        role_map_db[role_slug] = role_id
        _role_cache[role_slug] = role_id
        result.roles_created.append(role_slug)
        logger.info("Created role: %s (%s)", role_name, role_slug)
        return role_id

    return None


def clear_caches():
    """Reset module-level caches. Call between test runs."""
    _site_cache.clear()
    _role_cache.clear()


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

def cli_sync_map(args: list) -> int:
    """
    CLI handler for `netlapse sync-map`.

    Args:
        args: Remaining argv after subcommand.

    Returns:
        Exit code (0 = success).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="netlapse sync-map",
        description="Import devices from a Secure Cartography topology map.",
    )
    parser.add_argument(
        "map_path",
        help="Path to SC2 map.json file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report without writing to database",
    )
    parser.add_argument(
        "--exclude-prefix",
        action="append",
        default=[],
        dest="exclude_prefixes",
        help="Exclude devices whose hostname starts with PREFIX "
             "(repeatable, e.g. --exclude-prefix oob)",
    )
    parser.add_argument(
        "--no-collection",
        action="store_true",
        help="Import devices with collection_enabled=0",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="Force all devices to this site slug "
             "(overrides hostname-based extraction)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Database path (default: ~/.netlapse/netlapse.db)",
    )
    parser.add_argument(
        "--roles",
        default=None,
        metavar="PATH",
        help="Path to a roles.yaml defining hostname→role inference rules. "
             "If omitted, netlapse looks for roles.yaml in the definitions "
             "directory (alongside captures.yaml); if that's absent too, the "
             "built-in naming-convention defaults are used.",
    )

    opts = parser.parse_args(args)

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Initialize database
    from netlapse.dcim import NetlapseDB

    db_path = Path(opts.db).expanduser() if opts.db else None
    db = NetlapseDB(db_path)
    db.init_schema()

    # Resolve role-inference rules. Explicit --roles wins; otherwise look for a
    # roles.yaml in the definitions dir (sibling of captures.yaml/jobs.yaml).
    # Absent → None → sync_from_map falls back to the built-in defaults.
    role_rules = None
    try:
        if opts.roles:
            roles_path = Path(opts.roles).expanduser()
            if not roles_path.exists():
                print(f"✖ --roles file not found: {roles_path}")
                db.close()
                return 1
        else:
            from netlapse.registry.service import resolve_definitions_dir
            roles_path = resolve_definitions_dir() / ROLE_RULES_FILE

        known_roles = None
        try:
            known_roles = [
                row[0]
                for row in db.conn.execute(
                    "SELECT slug FROM dcim_device_role"
                ).fetchall()
            ]
        except Exception:  # pragma: no cover - advisory cross-check only
            known_roles = None

        role_rules = load_role_rules(roles_path, known_roles=known_roles)
        if role_rules is not None:
            print(
                f"Role inference: loaded {len(role_rules.prefix)} prefix + "
                f"{len(role_rules.segment)} segment rule(s) from {roles_path}"
            )
    except ValueError as e:
        # A malformed roles.yaml is fatal for the import — better to stop than
        # to silently mis-role the whole fleet.
        print(f"✖ {e}")
        db.close()
        return 1

    try:
        clear_caches()
        result = sync_from_map(
            db=db,
            map_path=opts.map_path,
            role_rules=role_rules,
            exclude_prefixes=opts.exclude_prefixes or None,
            site_override=opts.site,
            collection_enabled=not opts.no_collection,
            dry_run=opts.dry_run,
        )

        if opts.dry_run:
            print("\n=== DRY RUN — no changes written ===\n")

        print(result.summary())

        # Print legacy SSH warnings
        legacy = [d for d in result.devices if d.get("legacy_ssh_hint")]
        if legacy:
            print(f"\n⚠  {len(legacy)} device(s) may need legacy SSH:")
            for d in legacy:
                print(f"    {d['site']}/{d['name']} ({d['platform']})")

        return 0 if not result.errors else 1

    finally:
        db.close()