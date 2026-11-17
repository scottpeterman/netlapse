"""
Netlapse Native REST API Router

Beyond Oxidized compatibility: structured parsed data, semantic diffs,
multi-command job management, device inventory, and scheduler control.
"""

import logging
import time
from typing import Optional, List

from fastapi import Body, APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from netlapse import __version__

logger = logging.getLogger(__name__)

# Import get_uptime lazily to avoid circular import
def _get_uptime():
    from netlapse.app import get_uptime
    return get_uptime()

router = APIRouter(prefix="/api/v1", tags=["netlapse"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class DeviceSummary(BaseModel):
    id: int
    name: str
    primary_ip4: Optional[str] = None
    platform: Optional[str] = None
    site: Optional[str] = None
    role: Optional[str] = None
    status: str = "active"
    last_collected_at: Optional[str] = None


class JobDefinition(BaseModel):
    name: str
    slug: str
    capture_type: str
    commands: List[str]
    platform_filter: Optional[str] = None
    site_filter: Optional[str] = None
    role_filter: Optional[str] = None
    interval_seconds: int = 3600
    enabled: bool = True


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    uptime_seconds: float
    storage_backend: str = ""
    devices_total: int = 0
    devices_active: int = 0
    jobs_total: int = 0
    jobs_enabled: int = 0


# ---------------------------------------------------------------------------
# Health & Status
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
def health(request: Request):
    """Service health check with key metrics."""
    db = request.app.state.db

    devices = db.list_devices()
    active = [d for d in devices if d["status"] == "active"]
    jobs = db.list_jobs()
    enabled_jobs = [j for j in jobs if j["enabled"]]

    storage = request.app.state.storage

    return HealthResponse(
        status="ok",
        version=__version__,
        uptime_seconds=round(_get_uptime(), 1),
        storage_backend=type(storage).__name__,
        devices_total=len(devices),
        devices_active=len(active),
        jobs_total=len(jobs),
        jobs_enabled=len(enabled_jobs),
    )


@router.get("/status/scheduler")
def scheduler_status(request: Request):
    """Current scheduler state."""
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return {
            "running": False,
            "uptime_seconds": 0.0,
            "active_jobs": 0,
            "queued_collections": 0,
            "reason": "vault locked or scheduler not initialized",
        }
    return scheduler.status


# ---------------------------------------------------------------------------
# Devices (DCIM)
# ---------------------------------------------------------------------------

@router.get("/devices")
def list_devices(
    request: Request,
    site: Optional[str] = None,
    platform: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
):
    """List devices with optional filters."""
    db = request.app.state.db
    devices = db.list_devices(
        site=site, platform=platform, role=role, status=status
    )
    return [
        {
            "id": d["id"],
            "name": d["name"],
            "primary_ip4": d.get("primary_ip4"),
            "platform": d.get("platform_name"),
            "platform_slug": d.get("platform_slug"),
            "site": d.get("site_name"),
            "site_slug": d.get("site_slug"),
            "role": d.get("role_name"),
            "role_slug": d.get("role_slug"),
            "status": d["status"],
            "last_collected_at": d.get("last_collected_at"),
            "last_collection_status": d.get("last_collection_status"),
            "collection_enabled": bool(d.get("collection_enabled", 1)),
        }
        for d in devices
    ]


@router.get("/devices/{device_id}")
def get_device(request: Request, device_id: int):
    """Get full device detail by ID."""
    db = request.app.state.db
    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")
    return device


@router.patch("/devices/{device_id}")
def update_device(request: Request, device_id: int, body: dict = Body(...)):
    """Update a device's mutable fields."""
    db = request.app.state.db

    if not db.get_device_by_id(device_id):
        raise HTTPException(status_code=404, detail="device not found")

    try:
        updated = db.update_device(device_id, **body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if updated is None:
        raise HTTPException(status_code=404, detail="device not found")

    logger.info("Updated device %d: %s", device_id, list(body.keys()))
    return updated


@router.post("/devices", status_code=201)
def create_device(request: Request, body: dict = Body(...)):
    """
    Create a device manually.

    Body: {"name": <required>, "site_id": <required>, ...optional columns}.
    Optional columns match the device edit form (platform_id, role_id, status,
    primary_ip4, oob_ip, credential_id, ssh_port, collection_enabled,
    legacy_ssh, serial_number, asset_tag, description, comments).
    """
    db = request.app.state.db

    name = body.pop("name", None)
    site_id = body.pop("site_id", None)

    try:
        created = db.create_device(name=name, site_id=site_id, **body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    logger.info("Created device %s (site_id=%s)", created.get("name"), site_id)
    return created


@router.delete("/devices/{device_id}")
def delete_device(request: Request, device_id: int):
    """
    Delete a device's inventory row.

    Job history is preserved (FK SET NULL + denormalized name). Stored
    captures on disk are left in place by design.
    """
    db = request.app.state.db

    deleted = db.delete_device(device_id)
    if deleted is None:
        raise HTTPException(status_code=404, detail="device not found")

    logger.info("Deleted device %d (%s)", device_id, deleted.get("name"))
    return {"deleted": True, **deleted}


# ---------------------------------------------------------------------------
# Reference data (sites, platforms, roles, credentials)
# ---------------------------------------------------------------------------

@router.get("/sites")
def list_sites(request: Request):
    """List all sites with device counts."""
    db = request.app.state.db
    rows = db.conn.execute("SELECT * FROM v_site_summary ORDER BY slug").fetchall()
    return [dict(r) for r in rows]


@router.get("/platforms")
def list_platforms(request: Request):
    """List all platforms with device counts."""
    db = request.app.state.db
    return db.list_platforms()


@router.get("/roles")
def list_roles(request: Request):
    """List device roles with device counts."""
    db = request.app.state.db
    return db.list_roles()


@router.get("/credentials")
def list_credentials(request: Request):
    """List credentials (safe — id, name, username, is_default only)."""
    db = request.app.state.db
    return db.list_credentials()


# ---------------------------------------------------------------------------
# Snapshots (raw + structured)
# ---------------------------------------------------------------------------

@router.get("/devices/{device_id}/snapshots")
def list_snapshots(
    request: Request,
    device_id: int,
    capture_type: Optional[str] = None,
    limit: int = Query(20, le=100),
):
    """List collection snapshots for a device."""
    db = request.app.state.db
    storage = request.app.state.storage

    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")

    try:
        versions = storage.list_versions(
            device["name"], device["site_slug"], limit=limit
        )
        return [
            {
                "version_id": v.version_id,
                "collected_at": v.collected_at.isoformat() if v.collected_at else None,
                "message": v.message,
                "author": v.author,
                "trigger": v.trigger,
                "capture_types": v.capture_types,
            }
            for v in versions
        ]
    except NotImplementedError:
        return []


@router.get("/devices/{device_id}/snapshots/latest")
def latest_snapshot(
    request: Request,
    device_id: int,
    capture_type: str = "config",
):
    """Get the most recent snapshot (raw + parsed)."""
    db = request.app.state.db
    storage = request.app.state.storage

    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")

    snapshot = storage.fetch_latest(
        device["name"], device["site_slug"], capture_type
    )
    if not snapshot:
        raise HTTPException(status_code=404, detail="no snapshots found")

    return {
        "device": snapshot.device,
        "site": snapshot.site,
        "capture_type": snapshot.capture_type,
        "command": snapshot.command,
        "raw_text": snapshot.raw_text,
        "parsed_data": snapshot.parsed_data,
        "collected_at": (
            snapshot.collected_at.isoformat() if snapshot.collected_at else None
        ),
        "template_name": snapshot.template_name,
        "template_score": snapshot.template_score,
    }


@router.get("/devices/{device_id}/snapshots/{commit_sha}")
def get_snapshot(
    request: Request, device_id: int, commit_sha: str
):
    """Get a specific snapshot by version ID."""
    db = request.app.state.db
    storage = request.app.state.storage

    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")

    try:
        snapshot = storage.fetch_version(
            device["name"], device["site_slug"], commit_sha, "config"
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="version retrieval not supported by current storage backend",
        )

    if not snapshot:
        raise HTTPException(status_code=404, detail="snapshot not found")

    return {
        "device": snapshot.device,
        "site": snapshot.site,
        "capture_type": snapshot.capture_type,
        "raw_text": snapshot.raw_text,
        "parsed_data": snapshot.parsed_data,
        "collected_at": (
            snapshot.collected_at.isoformat() if snapshot.collected_at else None
        ),
    }


# ---------------------------------------------------------------------------
# Structured Diffs
# ---------------------------------------------------------------------------

@router.get("/devices/{device_id}/diff")
def structured_diff(
    request: Request,
    device_id: int,
    capture_type: str = "config",
    from_commit: Optional[str] = None,
    to_commit: Optional[str] = None,
    mask: bool = False,
    ignore: Optional[str] = None,
    compact: bool = False,
):
    """
    Structured diff between two snapshots.

    Returns semantic changes: added/removed/changed records, a unified
    raw text diff for backward compatibility, and a line-aligned
    config-aware diff (`aligned`) for the side-by-side viewer.

    mask=true masks volatile lines (config-change timestamps, etc.) from
    the comparison so they don't show as drift.

    ignore: comma-separated record column names to exclude from the semantic
    comparison (e.g. ?ignore=AGE for ARP). Omitting the parameter applies the
    defaults from definitions/volatility.yaml (heuristics + per-capture rules);
    passing it empty (?ignore=) compares every field. The response echoes
    `schema` (all columns), `key_fields` (never excludable), and `ignored`
    (effective set).

    compact=true omits `aligned` (the full side-by-side file). It is forced
    for read-only API tokens, which must not be able to pull a whole config
    through the diff view.
    """
    db = request.app.state.db
    storage = request.app.state.storage

    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")

    # None (param absent) → backend applies defaults; a present value (even
    # empty) is an explicit choice from the UI.
    ignore_fields = (
        None if ignore is None
        else [f.strip() for f in ignore.split(",") if f.strip()]
    )

    try:
        result = storage.diff(
            device["name"],
            device["site_slug"],
            capture_type,
            from_version=from_commit,
            to_version=to_commit,
            mask_common=mask,
            ignore_fields=ignore_fields,
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="diffs not supported by current storage backend",
        )

    if not result:
        raise HTTPException(status_code=404, detail="not enough snapshots to diff")

    return {
        "device": result.device,
        "capture_type": result.capture_type,
        "from_version": result.from_version,
        "to_version": result.to_version,
        "from_time": result.from_time,
        "to_time": result.to_time,
        "added": result.added,
        "removed": result.removed,
        "changed": result.changed,
        "raw_diff": result.raw_diff,
        "aligned": None if (compact or (getattr(request.state, "user", None) or {}).get("token_scope") == "readonly") else result.aligned,
        "schema": result.schema,
        "key_fields": result.key_fields,
        "ignored": result.ignored,
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@router.get("/jobs")
def list_jobs(request: Request, enabled_only: bool = False):
    """List all job definitions with last-run summary."""
    db = request.app.state.db
    return db.list_jobs(enabled_only=enabled_only)


@router.get("/jobs/due")
def get_due_jobs(request: Request):
    """Return enabled jobs whose next_run_at has passed.

    Used by the scheduler poll loop via HTTP so the event loop
    never touches the database directly.
    """
    from datetime import datetime, timezone
    db = request.app.state.db
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return db.get_due_jobs(now)

@router.get("/jobs/capture-types")
def list_job_capture_types(request: Request):
    """Configured capture types (assignable set) — distinct from
    /search/capture_types, which is the collected-on-disk set."""
    db = request.app.state.db
    return db.list_job_capture_types()

@router.get("/jobs/{job_slug}")
def get_job(request: Request, job_slug: str):
    """Get a single job definition."""
    db = request.app.state.db
    job = db.get_job(job_slug)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.post("/jobs/{job_slug}/run")
def trigger_job(request: Request, job_slug: str):
    """Trigger an immediate run of a job."""
    db = request.app.state.db
    job = db.get_job(job_slug)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="scheduler not running (vault locked?)",
        )

    scheduler.enqueue_job(job_slug, trigger="api")
    logger.info("Triggered job: %s", job_slug)
    return {"job": job_slug, "status": "queued"}


@router.post("/jobs")
def create_job(request: Request, body: dict = Body(...)):
    """Create a new collection job."""
    db = request.app.state.db

    required = ["name", "slug", "capture_type", "commands"]
    for field in required:
        if field not in body:
            raise HTTPException(
                status_code=422,
                detail=f"missing required field: {field}",
            )

    try:
        job = db.create_job(
            name=body["name"],
            slug=body["slug"],
            capture_type=body["capture_type"],
            commands=body["commands"],
            command_map=body.get("command_map"),
            interval_seconds=body.get("interval_seconds", 3600),
            description=body.get("description"),
            enabled=body.get("enabled", True),
            platform_filter=body.get("platform_filter"),
            site_filter=body.get("site_filter"),
            role_filter=body.get("role_filter"),
            device_filter=body.get("device_filter"),
            status_filter=body.get("status_filter", "active"),
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"job slug already exists: {body['slug']}",
            )
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("Created job: %s", body["slug"])
    return job


@router.put("/jobs/{job_slug}")
def update_job(request: Request, job_slug: str, body: dict = Body(...)):
    """Update a job's mutable fields."""
    db = request.app.state.db

    existing = db.get_job(job_slug)
    if not existing:
        raise HTTPException(status_code=404, detail="job not found")

    # File-managed jobs are read-only through the API: their definition lives
    # in the YAML catalog and would be overwritten on the next sync anyway.
    # Only the enable/disable toggle (its own endpoint) is runtime-mutable.
    if (existing.get("source") or None) == "file":
        raise HTTPException(
            status_code=409,
            detail=(
                "job is file-managed (defined in the definition catalog); "
                "edit captures.yaml / jobs.yaml and re-sync, or use the "
                "enable/disable toggle"
            ),
        )

    # Provenance is never settable through the API — adoption is a CLI action.
    body.pop("source", None)

    try:
        updated = db.update_job(job_slug, **body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if updated is None:
        raise HTTPException(status_code=404, detail="job not found")

    logger.info("Updated job: %s", job_slug)
    return updated


@router.delete("/jobs/{job_slug}")
def delete_job(request: Request, job_slug: str):
    """Delete a job definition. History is preserved."""
    db = request.app.state.db

    existing = db.get_job(job_slug)
    if not existing:
        raise HTTPException(status_code=404, detail="job not found")

    # A file-managed job would simply be re-created on the next sync, so
    # deletion has to happen at the source: remove it from jobs.yaml and
    # re-sync (which tombstones it and keeps history).
    if (existing.get("source") or None) == "file":
        raise HTTPException(
            status_code=409,
            detail=(
                "job is file-managed; remove it from jobs.yaml and re-sync "
                "to retire it (its history is preserved)"
            ),
        )

    deleted = db.delete_job(job_slug)
    if not deleted:
        raise HTTPException(status_code=404, detail="job not found")
    logger.info("Deleted job: %s", job_slug)
    return {"slug": job_slug, "deleted": True}


@router.put("/jobs/{job_slug}/enabled")
def set_job_enabled(request: Request, job_slug: str, body: dict = Body(...)):
    """Enable or disable a job."""
    db = request.app.state.db

    enabled = body.get("enabled")
    if enabled is None:
        raise HTTPException(
            status_code=422, detail="missing 'enabled' field",
        )

    updated = db.set_job_enabled(job_slug, bool(enabled))
    if not updated:
        raise HTTPException(status_code=404, detail="job not found")

    state = "enabled" if enabled else "disabled"
    logger.info("Job %s: %s", job_slug, state)
    return {"slug": job_slug, "enabled": bool(enabled)}


# ---------------------------------------------------------------------------
# Job history
# ---------------------------------------------------------------------------

@router.get("/jobs/{job_slug}/history")
def job_history(
    request: Request,
    job_slug: str,
    limit: int = 25,
):
    """List run history for a job."""
    db = request.app.state.db
    if not db.get_job(job_slug):
        raise HTTPException(status_code=404, detail="job not found")
    return db.list_job_history(job_slug, limit=limit)


@router.get("/history/{history_id}")
def get_job_run(request: Request, history_id: int):
    """Get a single job run with per-device results."""
    db = request.app.state.db
    run = db.get_job_run(history_id)
    if not run:
        raise HTTPException(status_code=404, detail="job run not found")
    return run


# ---------------------------------------------------------------------------
# Collection control
# ---------------------------------------------------------------------------

def _parse_capture_types(capture_type: Optional[str]) -> Optional[List[str]]:
    """'' / 'all' / None → None (all applicable captures); else a CSV list."""
    if not capture_type or capture_type.strip().lower() == "all":
        return None
    types = [t.strip() for t in capture_type.split(",") if t.strip()]
    return types or None


@router.get("/collect/requests/{request_id}")
def get_collect_request(request: Request, request_id: str):
    """State of one Collect Now request (polled by the device view)."""
    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(status_code=503, detail="scheduler not running (vault locked?)")
    req = scheduler.adhoc.get(request_id)
    if not req:
        raise HTTPException(
            status_code=404,
            detail="collect request not found (expired or server restarted)",
        )
    return req


@router.get("/devices/{device_id}/collect/plan")
def get_collect_plan(
    request: Request,
    device_id: int,
    capture_type: Optional[str] = None,
):
    """What Collect Now would run on this device: capture types and the
    platform-resolved commands. No SSH, no side effects."""
    from netlapse.scheduler.adhoc import resolve_adhoc_plan

    db = request.app.state.db
    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")
    return {
        "device_id": device_id,
        "device": device["name"],
        "platform": device.get("platform_slug"),
        "captures": resolve_adhoc_plan(
            db.list_jobs(), device, _parse_capture_types(capture_type)
        ),
    }


@router.get("/devices/{device_id}/collect/latest")
def get_latest_collect_request(request: Request, device_id: int):
    """Most recent Collect Now request for a device (in flight or finished
    within the retention window), or {"request": null}. Lets the device view
    pick an in-flight request back up after navigation or reload."""
    scheduler = request.app.state.scheduler
    if scheduler is None:
        return {"request": None}
    return {"request": scheduler.adhoc.latest_for_device(device_id)}


@router.post("/collect/{device_id}")
def collect_device(
    request: Request,
    device_id: int,
    capture_type: Optional[str] = None,
):
    """
    Trigger immediate collection for a device.

    capture_type: omitted / 'all' = every capture whose enabled job applies to
    this device; otherwise one capture type or a comma-separated list.

    Returns the tracked request (poll GET /collect/requests/{id}). If the
    device already has a request in flight, that one is returned with
    created=false rather than opening a second SSH session.
    """
    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="scheduler not running (vault locked?)",
        )

    user = getattr(request.state, "user", None) or {}
    trigger = user.get("username") if user.get("is_machine") else "ui"
    try:
        req, created = scheduler.request_collection(
            device_id, _parse_capture_types(capture_type), trigger=trigger or "api",
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="device not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {**req, "created": created}


@router.post("/devices/{device_id}/test-auth")
def test_auth(request: Request, device_id: int):
    """
    Test SSH authentication to a device.

    Connects, detects prompt, captures transport negotiation details,
    and disconnects. Returns structured result with debug log.
    """
    from netlapse.vault.bridge import resolve_credentials
    from netlapse.ssh.executor import test_device_auth

    db = request.app.state.db
    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")

    scheduler = request.app.state.scheduler
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail="scheduler not running (vault locked?)",
        )

    # Resolve credentials for this specific device
    try:
        creds = resolve_credentials(scheduler.vault, device)
    except Exception as e:
        return {
            "success": False,
            "device_name": device["name"],
            "duration_seconds": 0,
            "error_category": "credential_resolution",
            "error_message": str(e),
            "debug_log": [f"✗ Credential resolution failed: {e}"],
        }

    # Already in threadpool (def handler) — call directly. The `collection:`
    # section is passed so the probe resolves the host the same way collection
    # does; otherwise test-auth can succeed against an address the scheduler
    # never dials (or vice versa).
    collection_opts = (getattr(request.app.state, "config", None) or {}).get(
        "collection", {}
    )
    result = test_device_auth(device, creds, 15, timeouts=collection_opts)

    # Update device credential test fields
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    test_status = "success" if result.success else (
        result.error_category or "failed"
    )
    db.conn.execute(
        "UPDATE dcim_device SET credential_tested_at = ?, "
        "credential_test_result = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (now_iso, test_status, device_id),
    )
    db.conn.commit()

    logger.info(
        "Auth test for %s: %s in %.1fs",
        device["name"], test_status, result.duration_seconds,
    )

    return {
        "success": result.success,
        "device_name": result.device_name,
        "duration_seconds": result.duration_seconds,
        "prompt": result.prompt,
        "hostname": result.hostname,
        "ssh_banner": result.ssh_banner,
        "kex_algorithm": result.kex_algorithm,
        "cipher": result.cipher,
        "auth_method": result.auth_method,
        "error_category": result.error_category,
        "error_message": result.error_message,
        "debug_log": result.debug_log,
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    pattern: str
    capture_type: str = "config"
    site: Optional[str] = None


@router.post("/search")
def search(request: Request, body: SearchRequest):
    """
    Search across all stored raw text for a regex pattern.

    Supports searching any capture type (config, arp, bgp, routes, etc.).
    Returns matches with line numbers for context.
    """
    storage = request.app.state.storage
    db = request.app.state.db

    results = storage.search_configs(
        body.pattern,
        site=body.site,
        capture_type=body.capture_type,
    )

    # Enrich results with device IDs for linking
    for r in results:
        device = db.get_device_by_name(r["device"])
        r["device_id"] = device["id"] if device else None

    return results


@router.get("/search/capture_types")
def list_capture_types(request: Request):
    """List all capture types that have stored data."""
    storage = request.app.state.storage
    return storage.list_capture_types()


# ---------------------------------------------------------------------------
# One Search — single-token lookup across inventory + all captures
# ---------------------------------------------------------------------------

class GlobalSearchRequest(BaseModel):
    token: str
    site: Optional[str] = None


@router.post("/search/global")
def global_search(request: Request, body: GlobalSearchRequest):
    """
    One Search: type one token (IP, MAC, ASN, hostname, serial, VLAN…) and
    get everything that references it, grouped by where it was found.

    Two halves, both keyed off the same token:
      inventory   identity — which managed device(s) this token *is*
                  (its mgmt/OOB address, name, serial, asset tag, description)
      references  where it's *seen* — a single walk of the stored working
                  tree across every capture type, grouped per capture type

    The token is classified (token_match) so the regex matches the way the
    value actually appears in device output: IPv4 is octet-anchored so
    10.0.0.1 won't match inside 10.0.0.10, MAC addresses match in every
    vendor format regardless of how they were typed, ASNs match with or
    without the AS prefix, and free text is a case-insensitive substring.

    Returned as a sync endpoint (threadpool) — the tree walk is filesystem
    bound, so it never touches the event loop and the UI just shows a spinner.
    """
    from netlapse.storage.token_match import build_matcher

    storage = request.app.state.storage
    db = request.app.state.db

    token = (body.token or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="empty search token")

    matcher = build_matcher(token)

    # Where is it referenced — one walk across every capture type.
    references = storage.search_all(matcher["regex"], site=body.site)

    # Attach device IDs for linking (memoized by name across the result set).
    name_to_id: dict = {}
    for r in references:
        name = r["device"]
        if name not in name_to_id:
            dev = db.get_device_by_name(name)
            name_to_id[name] = dev["id"] if dev else None
        r["device_id"] = name_to_id[name]

    # What is it — inventory identity matches.
    inventory = db.search_inventory(token, matcher["kind"])

    devices = {r["device"] for r in references}
    capture_types = sorted({r["capture_type"] for r in references})
    total_lines = sum(len(r["matches"]) for r in references)

    return {
        "token": token,
        "kind": matcher["kind"],
        "variants": matcher["variants"],
        "inventory": inventory,
        "references": references,
        "capture_types": capture_types,
        "counts": {
            "devices": len(devices),
            "capture_types": len(capture_types),
            "lines": total_lines,
        },
    }