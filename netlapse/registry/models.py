"""
Registry models — the in-memory shape of the validated catalog.

These dataclasses are the contract every other layer reads. The loader builds
them (and is the only thing that constructs them from raw YAML); the resolver
and sync layer consume them. Nothing here touches disk or the DB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class CatalogError(Exception):
    """
    Raised when the catalog fails to load or validate as a whole.

    Carries every problem found in one pass (`errors`) so an operator fixing a
    typo sees all of them at once rather than one-per-restart. `warnings` are
    non-fatal advisories (e.g. a capture that resolves to >1 command, which is
    latent until artifact-keyed storage lands).
    """

    def __init__(self, errors: List[str], warnings: Optional[List[str]] = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__(
            f"{len(errors)} catalog error(s): " + "; ".join(errors)
        )


@dataclass
class CaptureDef:
    """
    One collectable artifact: vendor-neutral intent + per-platform syntax.

    `command` maps a platform slug to its command list; the reserved key
    'default' is the fallback for any platform without an explicit override.
    A platform's command is always normalized to a list[str] so it matches the
    existing executor contract (one Snapshot per command).

    `capture_type` is the coarse grouping tag (config/bgp/arp/...). It is NOT a
    unique key — multiple captures may share a capture_type. Until storage is
    artifact-keyed it also doubles as the on-disk filename, which is why a
    capture resolving to >1 command earns a warning.
    """
    slug: str
    capture_type: str
    command: Dict[str, List[str]]          # {"default": [...], "<platform>": [...]}
    description: Optional[str] = None
    parse: Optional[str] = None            # optional template-name pin (advisory)
    interval: int = 3600

    @property
    def default_commands(self) -> List[str]:
        return self.command["default"]

    def command_map_json(self) -> Optional[str]:
        """
        Project the per-platform overrides into the legacy command_map JSON
        shape ({platform_slug: [cmds]}) the scheduler/collector already read.
        Excludes 'default'. Returns None when there are no overrides.
        """
        overrides = {k: v for k, v in self.command.items() if k != "default"}
        return json.dumps(overrides) if overrides else None

    def commands_json(self) -> str:
        """Default command list as the legacy `commands` JSON array string."""
        return json.dumps(self.default_commands)

    def max_command_count(self) -> int:
        return max((len(v) for v in self.command.values()), default=0)


@dataclass
class JobBinding:
    """
    A deployment binding: run capture <capture> against a device selection.

    Carries no command syntax — that's resolved from the referenced capture at
    sync time. `enabled` here is only the *seed* value used when the job is
    first inserted; thereafter the live toggle is runtime-owned in the DB.
    """
    slug: str
    capture: str
    name: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True
    site_filter: Optional[str] = None
    role_filter: Optional[str] = None
    platform_filter: Optional[str] = None
    device_filter: Optional[str] = None
    status_filter: str = "active"


@dataclass
class Catalog:
    """The fully-validated set of definitions, ready to sync."""
    captures: Dict[str, CaptureDef] = field(default_factory=dict)
    jobs: Dict[str, JobBinding] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
