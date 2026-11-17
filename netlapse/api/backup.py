"""
Backup / restore admin API (prefix /api/v1/admin/backup — admin only).

  GET    /                         backups, pending/last restore, running job
  POST   /                         start a backup job  {data, label}
  GET    /job                      current/last job state (polled by the UI)
  GET    /files/{name}             download an archive
  DELETE /files/{name}             delete an archive
  POST   /upload?filename=NAME     upload an archive (raw body, streamed)
  POST   /files/{name}/restore     validate + stage a restore (job)
  DELETE /pending                  discard a staged restore

Backups and staging can take minutes with data included, so they run as a
single background job (one at a time) rather than inside the request. A staged
restore is applied on the next daemon start — the databases cannot be
replaced under the running process.
"""

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from netlapse.backup import (
    ARCHIVE_RE, BackupError, DATA_SCOPES, cancel_restore, create_backup,
    daemon_running, last_restore, list_backups, pending_restore, read_manifest,
    resolve_state_paths, stage_restore,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/backup", tags=["admin-backup"])

_job_lock = threading.Lock()
_job: Dict[str, Any] = {}


def _paths():
    return resolve_state_paths()


def _job_view() -> Optional[Dict[str, Any]]:
    with _job_lock:
        if not _job:
            return None
        out = dict(_job)
    if out.get("status") == "running":
        out["elapsed"] = round(time.time() - out["_t0"], 1)
    out.pop("_t0", None)
    return out


def _start_job(kind: str, target, **kwargs) -> Dict[str, Any]:
    with _job_lock:
        if _job.get("status") == "running":
            raise HTTPException(status_code=409, detail=f"a {_job['kind']} job is already running")
        _job.clear()
        _job.update({"kind": kind, "status": "running", "phase": "starting",
                     "result": None, "error": None, "_t0": time.time(),
                     "elapsed": None, **kwargs})

    def progress(msg: str):
        with _job_lock:
            _job["phase"] = msg

    def run():
        try:
            res = target(progress)
            with _job_lock:
                _job.update(status="done", phase="done", result=res)
        except BackupError as e:
            with _job_lock:
                _job.update(status="failed", phase="failed", error=str(e))
        except Exception as e:                       # noqa: BLE001
            logger.exception("%s job crashed", kind)
            with _job_lock:
                _job.update(status="failed", phase="failed", error=f"{type(e).__name__}: {e}")
        finally:
            with _job_lock:
                _job["elapsed"] = round(time.time() - _job["_t0"], 1)

    threading.Thread(target=run, name=f"netlapse-{kind}", daemon=True).start()
    return _job_view()


def _archive_path(name: str) -> Path:
    if not ARCHIVE_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid backup name")
    p = _paths().backups_dir / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="backup not found")
    return p


class BackupRequest(BaseModel):
    data: str = "none"
    label: Optional[str] = None


@router.get("")
def backup_status(request: Request):
    paths = _paths()
    return {
        "backups_dir": str(paths.backups_dir),
        "paths": paths.as_dict(),
        "backups": list_backups(paths.backups_dir),
        "pending": pending_restore(paths.config_dir),
        "last_restore": last_restore(paths.config_dir),
        "job": _job_view(),
        "daemon_pid_ok": daemon_running(paths.config_dir),
    }


@router.post("")
def start_backup(request: Request, body: BackupRequest):
    if body.data not in DATA_SCOPES:
        raise HTTPException(status_code=422, detail=f"data must be one of {', '.join(DATA_SCOPES)}")
    paths = _paths()
    actor = getattr(request.state, "user", {}) or {}
    logger.info("Backup requested by %s (data=%s)", actor.get("username"), body.data)

    def work(progress):
        res = create_backup(paths, body.data, label=body.label, progress=progress)
        return {"name": res["name"], "size": res["size"]}

    return _start_job("backup", work, data=body.data)


@router.get("/job")
def job_status():
    return {"job": _job_view()}


@router.get("/files/{name}")
def download_backup(name: str):
    p = _archive_path(name)
    return FileResponse(p, media_type="application/gzip", filename=name)


@router.delete("/files/{name}")
def delete_backup(name: str):
    p = _archive_path(name)
    pend = pending_restore(_paths().config_dir) or {}
    if pend.get("archive_name") == name:
        raise HTTPException(status_code=409, detail="this backup is staged for restore — cancel it first")
    p.unlink()
    return {"deleted": name}


@router.post("/upload")
async def upload_backup(request: Request, filename: str):
    """Stream an archive into the backups directory. Raw body
    (application/octet-stream) — no multipart dependency."""
    if not ARCHIVE_RE.match(filename):
        raise HTTPException(status_code=400, detail="file name must end in .tar.gz and use [A-Za-z0-9._-]")
    paths = _paths()
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    dst = paths.backups_dir / filename
    if dst.exists():
        raise HTTPException(status_code=409, detail="a backup with that name already exists")
    tmp = dst.with_name(filename + ".partial")
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
        read_manifest(tmp)                       # reject non-netlapse archives
    except BackupError as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(e))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dst)
    return {"uploaded": filename, "size": dst.stat().st_size}


@router.post("/files/{name}/restore")
def restore_backup(request: Request, name: str):
    p = _archive_path(name)
    paths = _paths()
    actor = getattr(request.state, "user", {}) or {}
    logger.warning("Restore of %s staged by %s", name, actor.get("username"))

    def work(progress):
        pend = stage_restore(p, paths, progress=progress)
        return {"archive": name, "vault_password": pend["vault_password"],
                "restart_required": True}

    return _start_job("restore", work, archive=name)


@router.delete("/pending")
def cancel_pending():
    with _job_lock:
        if _job.get("status") == "running" and _job.get("kind") == "restore":
            raise HTTPException(status_code=409, detail="restore is still being staged")
    return {"cancelled": cancel_restore(_paths().config_dir)}
