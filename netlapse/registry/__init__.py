"""
Netlapse Definition Registry — data-driven capture/job catalog.

The registry replaces hardcoded DEFAULT_JOBS + per-job command_map authoring
with two editable, file-authoritative YAML documents that live as siblings of
config.yaml (default ~/.netlapse/):

    captures.yaml    — what to collect, and how each platform speaks it
    jobs.yaml        — bindings: which capture runs where, enabled by default

Platforms are NOT in files: they live in dcim_platform (the DB), which is
already extensible and is what devices reference by platform_slug. A capture's
per-platform command keys are validated against that real list, not a second
file, so there is one source of platform truth and no drift.

Authority model (files-authoritative, DB-synced):
    - Definitions (capture syntax, bindings/filters, interval) are authoritative
      in the files. On startup the catalog is validated as a whole and projected
      into the jobs table.
    - Runtime state (last_run_at / next_run_at / job_history and the *live*
      enabled toggle) is authoritative in the DB and is never clobbered by a
      sync.

The DB is therefore both the runtime store and the last-good cache: if the
catalog fails validation on a restart, sync is skipped entirely and the daemon
runs on the definitions already in the DB.

Public API:
    load_catalog(definitions_dir, known_platforms=None) -> Catalog
    sync_definitions(db, catalog)      -> SyncReport
    resolve_commands(default, cmap, p) -> list[str]         (the one resolver)
"""

from .models import (
    Catalog,
    CaptureDef,
    JobBinding,
    CatalogError,
)
from .loader import load_catalog, DEFINITION_FILES
from .resolver import resolve_commands
from .sync import sync_definitions, SyncReport

__all__ = [
    "Catalog",
    "CaptureDef",
    "JobBinding",
    "CatalogError",
    "load_catalog",
    "DEFINITION_FILES",
    "resolve_commands",
    "sync_definitions",
    "SyncReport",
]
