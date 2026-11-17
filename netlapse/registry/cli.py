"""
`netlapse definitions` — operate the definition registry from the CLI.

Subcommands:
  validate   Load + validate the catalog without touching the DB. Exit 1 on
             any error (useful as a pre-restart / CI check).
  sync       Validate then project into the jobs table (same as startup sync).
  adopt      Stamp existing jobs as source='file' so the registry manages them.
             Use this once to take over the seeded default jobs on an upgraded
             install (their slugs match the shipped captures/jobs).
  list       Show the resolved catalog (captures, platforms, bindings).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .loader import load_catalog
from .models import CatalogError
from .service import resolve_definitions_dir, run_startup_sync
from .sync import SOURCE_FILE


def _open_db(db_arg):
    from netlapse.dcim import NetlapseDB
    db = NetlapseDB(Path(db_arg).expanduser() if db_arg else None)
    db.init_schema()
    return db


def cli_definitions(args: list) -> int:
    parser = argparse.ArgumentParser(
        prog="netlapse definitions",
        description="Manage the data-driven definition registry.",
    )
    parser.add_argument(
        "action",
        choices=["validate", "sync", "adopt", "list"],
        help="What to do",
    )
    parser.add_argument(
        "slugs", nargs="*",
        help="For 'adopt': job slugs to take over (or --all)",
    )
    parser.add_argument("--dir", default=None, help="Definitions directory")
    parser.add_argument("--db", default=None, help="DCIM database path")
    parser.add_argument("--all", action="store_true",
                        help="adopt: take over every job whose slug is in the catalog")
    opts = parser.parse_args(args)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    base = Path(opts.dir).expanduser() if opts.dir else resolve_definitions_dir()

    # ── validate / list ───────────────────────────────────────────────
    if opts.action in ("validate", "list"):
        # Validate per-platform keys against the real dcim_platform list when a
        # DB is reachable; fall back to a file-only check otherwise.
        known = None
        try:
            db = _open_db(opts.db)
            known = {p["slug"] for p in db.list_platforms()}
            db.close()
        except Exception:
            print("  (note: DB unavailable — skipping platform-key validation)")
        try:
            catalog = load_catalog(base, known_platforms=known)
        except CatalogError as e:
            print(f"INVALID — {len(e.errors)} error(s):")
            for err in e.errors:
                print(f"  ✗ {err}")
            for warn in e.warnings:
                print(f"  ! {warn}")
            return 1
        for warn in catalog.warnings:
            print(f"  ! {warn}")

        # volatility.yaml — diff-time policy, not part of the DB-synced catalog,
        # but validated here so a typo surfaces before it silently degrades to
        # "no default exclusions" at runtime.
        from .volatility import load_volatility_catalog, VolatilityError, VOLATILITY_FILE
        vol = None
        try:
            vol = load_volatility_catalog(base)
        except VolatilityError as e:
            print(f"INVALID — {VOLATILITY_FILE}: {len(e.errors)} error(s):")
            for err in e.errors:
                print(f"  ✗ {err}")
            return 1

        if opts.action == "validate":
            print(f"OK — {len(catalog.captures)} capture(s), "
                  f"{len(catalog.jobs)} binding(s), "
                  f"{len(vol.heuristics)} volatility heuristic(s), "
                  f"{len(vol.explicit)} volatility rule(s)")
            return 0
        # list
        print("Captures:")
        for slug, c in catalog.captures.items():
            plats = ",".join(k for k in c.command if k != "default")
            print(f"  {slug}  [{c.capture_type}] every {c.interval}s"
                  f"{'  overrides: ' + plats if plats else ''}")
        print("Bindings:")
        for slug, j in catalog.jobs.items():
            print(f"  {slug} → capture={j.capture} enabled={j.enabled}")
        print("Volatility:")
        if vol.heuristics:
            print(f"  heuristics: {', '.join(vol.heuristics)}")
        for ctype, rule in sorted(vol.explicit.items()):
            parts = []
            if rule.get("volatile"):
                parts.append("volatile=" + ",".join(sorted(rule["volatile"])))
            if rule.get("keep"):
                parts.append("keep=" + ",".join(sorted(rule["keep"])))
            print(f"  {ctype}: {'  '.join(parts) if parts else '(no rules)'}")
        return 0

    # ── sync ───────────────────────────────────────────────────────────
    if opts.action == "sync":
        db = _open_db(opts.db)
        try:
            report = run_startup_sync(db, definitions_dir=base)
            if report is None:
                print("Sync skipped (no files or invalid catalog — see log).")
                return 1
            print(f"Sync complete: {report.summary()}")
            for s in report.inserted:
                print(f"  + {s}")
            for s in report.tombstoned:
                print(f"  - {s} (tombstoned)")
            for s in report.conflicts:
                print(f"  ! {s} (conflict — not file-managed)")
            return 0
        finally:
            db.close()

    # ── adopt ──────────────────────────────────────────────────────────
    if opts.action == "adopt":
        db = _open_db(opts.db)
        try:
            try:
                catalog = load_catalog(base)
            except CatalogError as e:
                print(f"Cannot adopt — catalog invalid ({len(e.errors)} error(s)). "
                      f"Run `netlapse definitions validate`.")
                return 1

            existing = {j["slug"]: j for j in db.list_jobs()}
            if opts.all:
                targets = [s for s in catalog.jobs if s in existing]
            else:
                targets = opts.slugs
                if not targets:
                    print("Specify slug(s) to adopt, or --all.")
                    return 1

            adopted = 0
            for slug in targets:
                if slug not in existing:
                    print(f"  ? {slug}: no such job, skipping")
                    continue
                if slug not in catalog.jobs:
                    print(f"  ? {slug}: not in catalog, skipping")
                    continue
                db.update_job(slug, source=SOURCE_FILE)
                print(f"  ✓ {slug}: now file-managed")
                adopted += 1
            print(f"Adopted {adopted} job(s). Run `netlapse definitions sync` "
                  f"(or restart) to apply file definitions to them.")
            return 0
        finally:
            db.close()

    return 1