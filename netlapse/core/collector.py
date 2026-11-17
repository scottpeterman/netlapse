"""
Netlapse Collector — End-to-end collection pipeline.

Wires together: DCIM → Vault → Executor → Storage → History.

This is the bridge between "we have devices and credentials" and
"config backups land in git." The scheduler calls run_job(); the CLI
calls collect_now(). Both converge here.

Pipeline:
    1. Resolve job → target devices (DCIM list_collection_targets)
    2. Resolve credentials (vault bridge)
    3. Execute collection (SSH executor)
    4. Store results (storage backend)
    5. Record history (DCIM update_device_collection_status)

Usage (one-shot CLI):
    from netlapse.core.collector import collect_now

    results = collect_now(
        db=db,
        vault=vault,
        storage=storage,
        credential_name="lab",
        site_filter="cal",
    )

Usage (job-based):
    result = run_job(db, vault, storage, job_slug="config-backup")
"""

import json
import logging
import time
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from netlapse.ssh.executor import (
    collect_device,
    collect_devices,
    DeviceResult,
)
from netlapse.storage.backend import Snapshot
from netlapse.vault.bridge import resolve_shared_credentials, resolve_credential_chain, ENV_VAULT_PASSWORD

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Job-based collection
# ═══════════════════════════════════════════════════════════════════════════

def run_job(
    db,
    vault,
    storage,
    job_slug: str,
    credential_name: Optional[str] = None,
    credential_fallback: Optional[List[str]] = None,
    progress_callback=None,
) -> Dict[str, Any]:
    """
    Run a collection job from the jobs table.

    Resolves the job definition, finds target devices, collects,
    stores results, and updates history.

    Args:
        db: NetlapseDB instance.
        vault: Unlocked CredentialVault.
        storage: StorageBackend instance.
        job_slug: Job slug from the jobs table.
        credential_name: Override credential (default: vault default).
        progress_callback: Optional (device_name, status) callback.

    Returns:
        Summary dict with counts and timing.
    """
    start = time.time()

    # ── Resolve job ──────────────────────────────────────────────────
    job = db.get_job(job_slug)
    if not job:
        raise ValueError(f"Job not found: {job_slug}")

    if not job["enabled"]:
        logger.info("Job '%s' is disabled, skipping", job_slug)
        return {"status": "skipped", "reason": "disabled"}

    commands = json.loads(job["commands"])
    capture_type = job["capture_type"]
    # Per-platform command overrides — honored here so the CLI/job path resolves
    # dialects identically to the scheduler (previously only the scheduler did).
    command_map = json.loads(job["command_map"]) if job.get("command_map") else None

    logger.info(
        "Running job '%s': capture_type=%s, commands=%s",
        job_slug, capture_type, commands,
    )

    # ── Resolve targets ──────────────────────────────────────────────
    targets = db.list_collection_targets(
        platform_filter=job.get("platform_filter"),
        site_filter=job.get("site_filter"),
        role_filter=job.get("role_filter"),
        device_filter=job.get("device_filter"),
        status_filter=job.get("status_filter", "active"),
    )

    if not targets:
        logger.warning("Job '%s' matched no devices", job_slug)
        return {"status": "complete", "devices": 0, "reason": "no targets"}

    logger.info("Job '%s' targets %d devices", job_slug, len(targets))

    # ── Resolve credentials (ordered chain: primary + config fallbacks) ──
    creds = resolve_credential_chain(
        vault, primary_name=credential_name, fallback_names=credential_fallback
    )

    # ── Build jump-host resolver from config.yaml (None = all direct) ──
    from netlapse.ssh.proxy import build_proxy_resolver
    proxy_resolver = build_proxy_resolver(_load_proxy_config(), vault)

    # ── Collect ──────────────────────────────────────────────────────
    results = collect_devices(
        devices=targets,
        commands=commands,
        capture_type=capture_type,
        credentials=creds,
        progress_callback=progress_callback,
        proxy_resolver=proxy_resolver,
        command_map=command_map,
    )

    # ── Store results and update history ─────────────────────────────
    stored = 0
    errors = 0

    for result in results:
        _record_result(db, storage, result, job_slug)
        if result.success:
            stored += 1
        else:
            errors += 1

    # NOTE: Schedule advancement (last_run_at, next_run_at) is handled
    # by the scheduler after run_job() returns. The scheduler computes
    # next_run = last_run + interval_seconds.

    duration = time.time() - start

    summary = {
        "status": "complete",
        "job": job_slug,
        "devices": len(targets),
        "collected": stored,
        "failed": errors,
        "duration_seconds": round(duration, 2),
    }

    logger.info(
        "Job '%s' complete: %d/%d collected in %.1fs",
        job_slug, stored, len(targets), duration,
    )

    return summary


# ═══════════════════════════════════════════════════════════════════════════
# Ad-hoc collection (CLI / manual trigger)
# ═══════════════════════════════════════════════════════════════════════════

def collect_now(
    db,
    vault,
    storage,
    credential_name: Optional[str] = None,
    credential_fallback: Optional[List[str]] = None,
    commands: Optional[List[str]] = None,
    capture_type: str = "config",
    site_filter: Optional[str] = None,
    role_filter: Optional[str] = None,
    platform_filter: Optional[str] = None,
    device_filter: Optional[str] = None,
    progress_callback=None,
) -> Dict[str, Any]:
    """
    Run an ad-hoc collection (not from the jobs table).

    For CLI one-shot or manual API trigger. Defaults to
    'show running-config' if no commands specified.

    Args:
        db: NetlapseDB instance.
        vault: Unlocked CredentialVault.
        storage: StorageBackend instance.
        credential_name: Credential name (default: vault default).
        commands: CLI commands to run (default: platform-appropriate
                  show running-config).
        capture_type: Capture type label.
        site_filter: Filter by site slug(s).
        role_filter: Filter by role slug(s).
        platform_filter: Filter by platform slug(s).
        device_filter: Filter by device name pattern.
        progress_callback: Optional (device_name, status) callback.

    Returns:
        Summary dict with counts and timing.
    """
    start = time.time()

    # ── Default commands ─────────────────────────────────────────────
    if commands is None:
        commands = ["show running-config"]

    # ── Resolve targets ──────────────────────────────────────────────
    targets = db.list_collection_targets(
        platform_filter=platform_filter,
        site_filter=site_filter,
        role_filter=role_filter,
        device_filter=device_filter,
    )

    if not targets:
        logger.warning("No devices match the specified filters")
        return {"status": "complete", "devices": 0, "reason": "no targets"}

    logger.info("Collecting from %d devices", len(targets))

    # ── Resolve credentials (ordered chain: primary + config fallbacks) ──
    creds = resolve_credential_chain(
        vault, primary_name=credential_name, fallback_names=credential_fallback
    )

    # ── Build jump-host resolver from config.yaml (None = all direct) ──
    from netlapse.ssh.proxy import build_proxy_resolver
    proxy_resolver = build_proxy_resolver(_load_proxy_config(), vault)

    # ── Collect ──────────────────────────────────────────────────────
    results = collect_devices(
        devices=targets,
        commands=commands,
        capture_type=capture_type,
        credentials=creds,
        progress_callback=progress_callback,
        proxy_resolver=proxy_resolver,
    )

    # ── Store results and update history ─────────────────────────────
    stored = 0
    errors = 0

    for result in results:
        _record_result(db, storage, result)
        if result.success:
            stored += 1
        else:
            errors += 1

    duration = time.time() - start

    summary = {
        "status": "complete",
        "devices": len(targets),
        "collected": stored,
        "failed": errors,
        "duration_seconds": round(duration, 2),
    }

    logger.info(
        "Collection complete: %d/%d collected in %.1fs",
        stored, len(targets), duration,
    )

    return summary


# ═══════════════════════════════════════════════════════════════════════════
# Result processing
# ═══════════════════════════════════════════════════════════════════════════

def _record_result(
    db,
    storage,
    result: DeviceResult,
    job_slug: Optional[str] = None,
) -> Optional[bool]:
    """
    Process a DeviceResult: store snapshots and update DCIM history.

    Args:
        db: NetlapseDB instance.
        storage: StorageBackend instance.
        result: DeviceResult from the executor.
        job_slug: Optional job slug for history context.

    Returns:
        True if snapshots were stored, False if storing them raised,
        None if there was nothing to store (failed collection / no output).
    """
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Update device collection status ──────────────────────────────
    status = "success" if result.success else (
        result.error_category if result.error_category else "error"
    )

    try:
        db.update_device_collection_status(
            device_id=result.device_id,
            status=status,
            collected_at=now_iso,
        )
    except Exception as e:
        logger.warning(
            "Failed to update collection status for device %s: %s",
            result.device_name, e,
        )

    # ── Store snapshots ──────────────────────────────────────────────
    if result.success and result.snapshots:
        try:
            # Normalize line endings — devices send \r\n, we store \n only
            for snap in result.snapshots:
                if snap.raw_text:
                    snap.raw_text = snap.raw_text.replace("\r", "")

            storage.store_batch(
                snapshots=result.snapshots,
            )
            logger.info(
                "Stored %d snapshot(s) for %s (parsed=%s)",
                len(result.snapshots), result.device_name,
                any(s.parsed_data for s in result.snapshots),
            )
            return True
        except Exception as e:
            logger.error(
                "Failed to store snapshots for %s: %s",
                result.device_name, e,
            )
            return False
    elif not result.success:
        logger.warning(
            "Collection failed for %s: [%s] %s",
            result.device_name,
            result.error_category if result.error_category else "unknown",
            result.error_message or "",
        )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

def _load_fallback_order(db_arg: Optional[str] = None) -> List[str]:
    """
    Read credentials.fallback_order from config.yaml for the CLI path.

    Mirrors the daemon's config loading (NETLAPSE_CONFIG or the default
    ~/.netlapse/config.yaml) without importing the FastAPI app, so the CLI
    honors the same ordered fallback the scheduler uses. Returns [] if the
    file or key is absent — never raises on a missing/empty config.
    """
    import os
    import yaml
    from pathlib import Path

    config_path = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()

    if not config_path.exists():
        return []
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Could not read config %s for fallback order: %s",
                       config_path, e)
        return []

    order = (cfg.get("credentials") or {}).get("fallback_order") or []
    if not isinstance(order, list):
        logger.warning("credentials.fallback_order must be a list, ignoring")
        return []
    return [str(n) for n in order]


def _load_proxy_config(db_arg: Optional[str] = None) -> Dict[str, Any]:
    """
    Read top-level jump_hosts/proxy_rules from config.yaml for the CLI path.

    Mirrors the daemon's lifespan loader so `netlapse collect` honors the same
    jump-host routing the scheduler uses. Returns an empty slice when the file
    or keys are absent — build_proxy_resolver() then yields None (direct).
    """
    import os
    import yaml
    from pathlib import Path

    config_path = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()

    if not config_path.exists():
        return {}
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Could not read config %s for proxy config: %s",
                       config_path, e)
        return {}

    return {
        "jump_hosts": cfg.get("jump_hosts") or {},
        "proxy_rules": cfg.get("proxy_rules") or [],
    }


def cli_collect(args: list) -> int:
    """
    CLI handler for `netlapse collect`.

    Runs an ad-hoc collection or a named job.

    Args:
        args: Remaining argv after subcommand.

    Returns:
        Exit code.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="netlapse collect",
        description="Run a collection cycle against network devices.",
    )
    parser.add_argument(
        "--job",
        default=None,
        help="Run a named job from the jobs table (e.g. config-backup)",
    )
    parser.add_argument(
        "--credential",
        default=None,
        help="Credential name from vault (default: vault default)",
    )
    parser.add_argument(
        "--credential-fallback",
        default=None,
        metavar="NAME[,NAME...]",
        help="Ordered fallback credential names, comma-separated. Tried in "
             "order only on auth failure. Overrides config.yaml "
             "credentials.fallback_order when given.",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="Filter by site slug (comma-separated)",
    )
    parser.add_argument(
        "--role",
        default=None,
        help="Filter by role slug (comma-separated)",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="Filter by platform slug (comma-separated)",
    )
    parser.add_argument(
        "--commands",
        default=None,
        help="CLI commands (JSON array string, for ad-hoc only)",
    )
    parser.add_argument(
        "--capture-type",
        default="config",
        help="Capture type label (default: config)",
    )
    parser.add_argument(
        "--storage",
        default="file",
        choices=["file", "git"],
        help="Storage backend (default: file)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="DCIM database path",
    )
    parser.add_argument(
        "--vault-db",
        default=None,
        help="Vault database path",
    )
    parser.add_argument(
        "--emulate",
        nargs="?",
        const=True,
        default=None,
        metavar="LOOKUP_PATH",
        help="Enable NetEmulate mode (optionally specify ip_lookup.json path)",
    )

    opts = parser.parse_args(args)

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # ── Enable emulation if requested ────────────────────────────────
    if opts.emulate:
        from netlapse.ssh.emulation import enable_emulation
        lookup_path = opts.emulate if isinstance(opts.emulate, str) else None
        count = enable_emulation(lookup_path=lookup_path)
        print(f"Emulation enabled: {count} device IPs loaded")
    # ── Initialize components ────────────────────────────────────────
    from pathlib import Path
    from netlapse.dcim import NetlapseDB
    from netlapse.vault.bridge import get_vault

    db_path = Path(opts.db).expanduser() if opts.db else None
    db = NetlapseDB(db_path)

    vault_path = Path(opts.vault_db).expanduser() if opts.vault_db else None
    vault = get_vault(vault_path)

    if not vault.is_unlocked:
        print(
            f"Vault is locked. Set {ENV_VAULT_PASSWORD} env var "
            f"or run: netlapse vault unlock"
        )
        return 1

    # ── Initialize storage ───────────────────────────────────────────
    if opts.storage == "git":
        from netlapse.storage.git_backend import GitStorageBackend
        storage = GitStorageBackend()
    else:
        from netlapse.storage import create_backend
        storage = create_backend({"storage": {"backend": opts.storage}})
        storage.initialize()

    # ── Progress callback ────────────────────────────────────────────
    def on_progress(device_name: str, status: str):
        print(f"  {device_name}: {status}")

    # ── Resolve credential fallback order ────────────────────────────
    # CLI flag wins; otherwise read config.yaml credentials.fallback_order.
    if opts.credential_fallback:
        fallback_order = [
            n.strip() for n in opts.credential_fallback.split(",") if n.strip()
        ]
    else:
        fallback_order = _load_fallback_order(opts.db)
    if fallback_order:
        logger.info("Credential fallback order: %s", ", ".join(fallback_order))

    # ── Run ──────────────────────────────────────────────────────────
    try:
        if opts.job:
            summary = run_job(
                db=db,
                vault=vault,
                storage=storage,
                job_slug=opts.job,
                credential_name=opts.credential,
                credential_fallback=fallback_order,
                progress_callback=on_progress,
            )
        else:
            commands = None
            if opts.commands:
                commands = json.loads(opts.commands)

            summary = collect_now(
                db=db,
                vault=vault,
                storage=storage,
                credential_name=opts.credential,
                credential_fallback=fallback_order,
                commands=commands,
                capture_type=opts.capture_type,
                site_filter=opts.site,
                role_filter=opts.role,
                platform_filter=opts.platform,
                progress_callback=on_progress,
            )

        # ── Print summary ────────────────────────────────────────────
        print(f"\n{'='*50}")
        print(f"Status:    {summary['status']}")
        print(f"Devices:   {summary.get('devices', 0)}")
        print(f"Collected: {summary.get('collected', 0)}")
        print(f"Failed:    {summary.get('failed', 0)}")
        if "duration_seconds" in summary:
            print(f"Duration:  {summary['duration_seconds']}s")
        print(f"{'='*50}")

        return 0 if summary.get("failed", 0) == 0 else 1

    except Exception as e:
        logger.error("Collection failed: %s", e, exc_info=True)
        print(f"\nError: {e}")
        return 1

    finally:
        db.close()