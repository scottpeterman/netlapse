"""
Sync — project a validated Catalog into the jobs table.

Reconciliation is keyed on job slug and follows three rules, chosen so the
existing enable/disable API keeps working and audit history is never lost:

  in file, not in DB        → INSERT. Seed enabled from the file; leave
                              next_run_at NULL so it runs on the next tick.
  in file and in DB         → UPDATE definition columns (name, capture_type,
                              commands, command_map, interval, filters).
                              Preserve runtime: never touch last_run_at, and
                              never overwrite the live `enabled` toggle. If the
                              interval changed, re-derive next_run_at from the
                              existing last_run_at so it takes effect this cycle.
  in DB (file-managed), not in file
                            → TOMBSTONE: set enabled=0 and stop scheduling it,
                              but keep the row and its history. Re-adding it to
                              the file does NOT auto-re-enable (enabled is
                              runtime-owned once a job exists) — toggle it in
                              the UI. Purging is a separate explicit action.

API-created jobs (source != 'file') are invisible to sync: never updated,
never tombstoned. A file job whose slug collides with an existing API job is
reported as a conflict and skipped rather than silently taken over.

This module is the only writer that stamps source='file'. It is atomic at the
catalog level by construction: load_catalog() has already validated the whole
set, so by the time sync runs there is nothing left that can half-fail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Catalog, CaptureDef, JobBinding

logger = logging.getLogger(__name__)

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
SOURCE_FILE = "file"
SOURCE_SEED = "seed"


@dataclass
class SyncReport:
    """Outcome of one sync pass — logged at startup, returned for tests/API."""
    inserted: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    adopted: List[str] = field(default_factory=list)
    tombstoned: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.inserted)} inserted, {len(self.updated)} updated, "
            f"{len(self.adopted)} adopted, {len(self.tombstoned)} tombstoned, "
            f"{len(self.conflicts)} conflict(s)"
        )


def _derive_columns(binding: JobBinding, cap: CaptureDef) -> Dict[str, Any]:
    """Flatten a binding + its referenced capture into legacy job columns."""
    return {
        "name": binding.name or cap.description or binding.slug,
        "description": binding.description or cap.description,
        "capture_type": cap.capture_type,
        "commands": cap.commands_json(),
        "command_map": cap.command_map_json(),
        "interval_seconds": cap.interval,
        "platform_filter": binding.platform_filter,
        "site_filter": binding.site_filter,
        "role_filter": binding.role_filter,
        "device_filter": binding.device_filter,
        "status_filter": binding.status_filter,
    }


def _recompute_next_run(last_run_at: Optional[str], interval: int) -> Optional[str]:
    """
    next_run = last_run + interval, clamped to now (never schedule in the past).

    Returns None when the job has never run (last_run_at is NULL), which leaves
    it due on the next tick.
    """
    if not last_run_at:
        return None
    try:
        last = datetime.strptime(last_run_at, _TS_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    nxt = last.timestamp() + interval
    now = datetime.now(timezone.utc).timestamp()
    return datetime.fromtimestamp(max(nxt, now), tz=timezone.utc).strftime(_TS_FMT)


def sync_definitions(db, catalog: Catalog) -> SyncReport:
    """
    Reconcile the jobs table to the catalog. See module docstring for rules.

    Args:
        db: NetlapseDB (post-init_schema; requires the `source` column, v8+).
        catalog: a validated Catalog from load_catalog().

    Returns:
        SyncReport (also surfaces the catalog's own load-time warnings).
    """
    report = SyncReport(warnings=list(catalog.warnings))

    existing = {job["slug"]: job for job in db.list_jobs()}
    file_slugs = set(catalog.jobs)

    # ── Inserts and updates ──────────────────────────────────────────────
    for slug, binding in catalog.jobs.items():
        cap = catalog.captures[binding.capture]   # presence guaranteed by validation
        cols = _derive_columns(binding, cap)
        prior = existing.get(slug)

        if prior is None:
            db.create_job(
                slug=slug,
                enabled=binding.enabled,
                source=SOURCE_FILE,
                **cols,
            )
            report.inserted.append(slug)
            logger.info("sync: inserted job '%s' (capture=%s)", slug, binding.capture)
            continue

        # Slug already exists. Manage it if it's file-owned OR a shipped
        # default ('seed') that this binding now claims; a 'seed' match is an
        # adoption (promote seed → file). NULL/'api' jobs are hand-made and
        # never taken over.
        prior_source = (prior.get("source") or None)
        if prior_source not in (SOURCE_FILE, SOURCE_SEED):
            report.conflicts.append(slug)
            logger.warning(
                "sync: job '%s' exists with source=%r — not file-managed, "
                "skipping (rename the file binding or the API job to resolve)",
                slug, prior_source,
            )
            continue

        adopting = prior_source == SOURCE_SEED

        # Definition update — enabled and schedule columns deliberately omitted.
        # source=file promotes a seed; it's a no-op for an already-file job.
        db.update_job(slug, source=SOURCE_FILE, **cols)
        if adopting:
            report.adopted.append(slug)
            logger.info(
                "sync: adopted seeded default '%s' into the catalog "
                "(seed → file)", slug,
            )
        else:
            report.updated.append(slug)

        # Interval change takes effect this cycle, not next.
        old_interval = prior.get("interval_seconds")
        if old_interval != cols["interval_seconds"]:
            new_next = _recompute_next_run(
                prior.get("last_run") or prior.get("last_run_at"),
                cols["interval_seconds"],
            )
            db.update_job_schedule(
                slug,
                last_run=prior.get("last_run") or prior.get("last_run_at"),
                next_run=new_next,
            )
            logger.info(
                "sync: job '%s' interval %s→%ss, next_run re-derived to %s",
                slug, old_interval, cols["interval_seconds"], new_next,
            )

    # ── Tombstones (file-managed jobs that vanished from the catalog) ─────
    for slug, prior in existing.items():
        if slug in file_slugs:
            continue
        if (prior.get("source") or None) != SOURCE_FILE:
            continue  # API-created job — leave it alone
        if prior.get("enabled"):
            db.update_job(slug, enabled=False)
        report.tombstoned.append(slug)
        logger.info(
            "sync: tombstoned job '%s' (removed from catalog; history kept)", slug
        )

    return report