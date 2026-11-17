"""
Registry loader — read + validate the definition files as one set.

The loader is the only place that turns raw YAML into model objects, and it is
all-or-nothing: it accumulates every problem across the files and then either
returns a fully-valid Catalog or raises CatalogError with the complete list. A
half-parsed catalog is never returned, so a sync built on it can be atomic by
construction.

Platforms are NOT defined here — they live in dcim_platform (the DB), already
extensible and referenced by devices via platform_slug. A capture's per-platform
command keys are validated against the real platform list (passed in as
`known_platforms`) rather than a second file, so an override key that no device
could ever match is caught against the source that actually decides matching.

Missing files are not an error — an absent captures.yaml simply yields an empty
catalog (sync then tombstones any previously file-managed jobs). This lets an
operator adopt the registry incrementally.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import yaml

from .models import (
    Catalog,
    CaptureDef,
    JobBinding,
    CatalogError,
)

logger = logging.getLogger(__name__)

DEFINITION_FILES = ("captures.yaml", "jobs.yaml")

# 'default' is reserved as the fallback key inside a capture's command map.
_DEFAULT_KEY = "default"
_VALID_STATUS = {"active", "staged", "decommissioning", "offline", "planned"}


def _read_yaml(path: Path, errors: List[str]) -> Optional[dict]:
    """Read one YAML doc. Absent → None (caller treats as empty, no error)."""
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        errors.append(f"{path.name}: invalid YAML: {e}")
        return None
    if data is None:
        return {}
    if not isinstance(data, dict):
        errors.append(f"{path.name}: top level must be a mapping, got {type(data).__name__}")
        return None
    return data


def _norm_command_value(value: Any) -> List[str]:
    """Normalize a command (string or list) to a list[str]."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(type(value).__name__)


def _parse_captures(raw: dict, errors: List[str]) -> Dict[str, CaptureDef]:
    out: Dict[str, CaptureDef] = {}
    section = raw.get("captures") or {}
    if not isinstance(section, dict):
        errors.append("captures.yaml: 'captures' must be a mapping")
        return out

    for slug, body in section.items():
        if not isinstance(body, dict):
            errors.append(f"captures.{slug}: must be a mapping")
            continue

        capture_type = body.get("capture_type")
        if not capture_type or not isinstance(capture_type, str):
            errors.append(f"captures.{slug}: 'capture_type' is required and must be a string")

        # command may be a bare string/list (→ default) or a {platform: cmd} map.
        raw_cmd = body.get("command")
        command: Dict[str, List[str]] = {}
        if raw_cmd is None:
            errors.append(f"captures.{slug}: 'command' is required")
        elif isinstance(raw_cmd, (str, list)):
            try:
                command[_DEFAULT_KEY] = _norm_command_value(raw_cmd)
            except TypeError as t:
                errors.append(f"captures.{slug}.command: unsupported type {t}")
        elif isinstance(raw_cmd, dict):
            for pkey, pval in raw_cmd.items():
                try:
                    command[pkey] = _norm_command_value(pval)
                except TypeError as t:
                    errors.append(f"captures.{slug}.command.{pkey}: unsupported type {t}")
            if _DEFAULT_KEY not in command:
                errors.append(
                    f"captures.{slug}.command: must define a 'default' "
                    f"(the fallback for platforms without an override)"
                )
        else:
            errors.append(f"captures.{slug}.command: must be a string, list, or mapping")

        interval = body.get("interval", 3600)
        if not isinstance(interval, int) or interval <= 0:
            errors.append(f"captures.{slug}: 'interval' must be a positive integer (seconds)")
            interval = 3600

        # Only construct when this capture is internally well-formed; otherwise
        # we already recorded its errors and skip to avoid a half-built object.
        if capture_type and command.get(_DEFAULT_KEY):
            out[slug] = CaptureDef(
                slug=slug,
                capture_type=capture_type,
                command=command,
                description=body.get("description"),
                parse=body.get("parse"),
                interval=interval,
            )
    return out


def _parse_jobs(raw: dict, errors: List[str]) -> Dict[str, JobBinding]:
    out: Dict[str, JobBinding] = {}
    section = raw.get("jobs") or {}
    if not isinstance(section, dict):
        errors.append("jobs.yaml: 'jobs' must be a mapping")
        return out

    for slug, body in section.items():
        body = body or {}
        if not isinstance(body, dict):
            errors.append(f"jobs.{slug}: must be a mapping")
            continue

        capture = body.get("capture")
        if not capture or not isinstance(capture, str):
            errors.append(f"jobs.{slug}: 'capture' is required and must be a capture slug")

        filters = body.get("filters") or {}
        if not isinstance(filters, dict):
            errors.append(f"jobs.{slug}.filters: must be a mapping")
            filters = {}

        status = filters.get("status", "active") or "active"
        if status not in _VALID_STATUS:
            errors.append(
                f"jobs.{slug}.filters.status: '{status}' not in {sorted(_VALID_STATUS)}"
            )

        if capture:
            out[slug] = JobBinding(
                slug=slug,
                capture=capture,
                name=body.get("name"),
                description=body.get("description"),
                enabled=bool(body.get("enabled", True)),
                site_filter=filters.get("site"),
                role_filter=filters.get("role"),
                platform_filter=filters.get("platform"),
                device_filter=filters.get("device"),
                status_filter=status,
            )
    return out


def _cross_validate(
    captures: Dict[str, CaptureDef],
    jobs: Dict[str, JobBinding],
    known_platforms: Optional[Set[str]],
    errors: List[str],
    warnings: List[str],
) -> None:
    """Referential integrity + advisory warnings.

    `known_platforms` is the set of real platform slugs from dcim_platform. When
    provided, a per-platform command key that isn't in it is warned about (the
    override can never match a device, so those devices silently get the default
    — which is harmless, hence a warning not an error, and may be intentional
    during bring-up before the platform exists). When None (offline validate
    with no DB), the check is skipped.
    """
    for cslug, cap in captures.items():
        for pkey in cap.command:
            if pkey == _DEFAULT_KEY:
                continue
            if known_platforms is not None and pkey not in known_platforms:
                warnings.append(
                    f"captures.{cslug}.command.{pkey}: platform '{pkey}' is "
                    f"not in dcim_platform — no device can match it, so those "
                    f"devices fall back to the default command (typo?)"
                )
        # Latent multi-command collision: until storage is artifact-keyed,
        # several commands under one capture_type share one on-disk file.
        if cap.max_command_count() > 1:
            warnings.append(
                f"captures.{cslug}: resolves to >1 command per platform; "
                f"these share capture_type '{cap.capture_type}' and will "
                f"collide in storage until artifact-keyed storage lands"
            )

    # Every job must reference a defined capture.
    for jslug, job in jobs.items():
        if job.capture and job.capture not in captures:
            errors.append(
                f"jobs.{jslug}: references unknown capture '{job.capture}'"
            )


def load_catalog(
    definitions_dir: str | Path,
    known_platforms: Optional[Iterable[str]] = None,
) -> Catalog:
    """
    Load and validate the catalog from a directory of definition files.

    Args:
        definitions_dir: directory holding captures.yaml / jobs.yaml.
        known_platforms: real platform slugs (from dcim_platform). When given,
            per-platform command keys are checked against it; when None the
            check is skipped (offline validate without a DB).

    Returns a fully-valid Catalog (with any non-fatal `warnings` attached) or
    raises CatalogError carrying every problem found. Missing files are treated
    as empty sections, not errors.
    """
    base = Path(definitions_dir).expanduser()
    errors: List[str] = []
    warnings: List[str] = []

    captures_raw = _read_yaml(base / "captures.yaml", errors) or {}
    jobs_raw = _read_yaml(base / "jobs.yaml", errors) or {}

    captures = _parse_captures(captures_raw, errors)
    jobs = _parse_jobs(jobs_raw, errors)

    known = set(known_platforms) if known_platforms is not None else None
    _cross_validate(captures, jobs, known, errors, warnings)

    if errors:
        raise CatalogError(errors, warnings)

    return Catalog(
        captures=captures,
        jobs=jobs,
        warnings=warnings,
    )
