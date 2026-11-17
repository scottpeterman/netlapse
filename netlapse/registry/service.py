"""
Startup sync service — the one call both the daemon and CLI make.

Wraps load_catalog + sync_definitions with the fail-soft policy that makes
file-authority safe to operate: if the catalog is missing it is inert; if the
catalog is present but invalid, the sync is skipped entirely and the process
continues on the definitions already in the DB (the DB is the last-good cache).
A sync never half-applies, so a typo degrades to "ran with last good
definitions", never to "stopped collecting".
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .loader import load_catalog, DEFINITION_FILES
from .models import CatalogError
from .sync import sync_definitions, SyncReport

logger = logging.getLogger(__name__)


def resolve_definitions_dir(config: Optional[dict] = None) -> Path:
    """
    Definitions live as siblings of config.yaml by default.

    Resolution order:
      1. config['definitions_dir'] if set
      2. directory of $NETLAPSE_CONFIG (or ~/.netlapse/config.yaml)
    """
    if config and config.get("definitions_dir"):
        return Path(config["definitions_dir"]).expanduser()
    config_path = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()
    return config_path.parent


def run_startup_sync(
    db,
    config: Optional[dict] = None,
    definitions_dir: Optional[Path] = None,
) -> Optional[SyncReport]:
    """
    Load + validate + sync the catalog. Fail-soft on validation errors.

    Returns the SyncReport on success, or None when sync was skipped (no files,
    or invalid catalog). Never raises — the daemon must start regardless.
    """
    base = definitions_dir or resolve_definitions_dir(config)

    present = [f for f in DEFINITION_FILES if (base / f).exists()]
    if not present:
        logger.info(
            "Definition registry: no files in %s — skipping sync "
            "(jobs unchanged). Drop captures.yaml/jobs.yaml "
            "there to manage jobs from files.",
            base,
        )
        return None

    try:
        known = {p["slug"] for p in db.list_platforms()}
        catalog = load_catalog(base, known_platforms=known)
    except CatalogError as e:
        logger.error(
            "Definition registry: %d error(s) in %s — SKIPPING sync, running "
            "on existing DB definitions:", len(e.errors), base,
        )
        for err in e.errors:
            logger.error("  catalog: %s", err)
        for warn in e.warnings:
            logger.warning("  catalog: %s", warn)
        return None

    for warn in catalog.warnings:
        logger.warning("Definition registry: %s", warn)

    report = sync_definitions(db, catalog)
    logger.info("Definition registry sync: %s", report.summary())
    if report.conflicts:
        logger.warning(
            "Definition registry: %d slug conflict(s) with non-file jobs: %s "
            "(run `netlapse definitions adopt <slug>` to take them over)",
            len(report.conflicts), ", ".join(report.conflicts),
        )
    return report
