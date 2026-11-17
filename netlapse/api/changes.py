"""
Netlapse Change Feed API — fleet-wide "what actually moved" across a window.

The device detail view answers "what changed on THIS device," one capture at a
time. This router answers the aggregate: across the whole fleet, which devices'
configs changed, which interfaces changed, when — with volatility filtered out
so an ARP age counter or a config commit-timestamp never reads as drift.

It's a two-phase design, because deciding realness means diffing and that can
be a lot of work on a busy fleet:

  GET  /candidates  → cheap enumeration of candidate change events (one git-log
                      walk / _history scan; filenames only, no content). Drives
                      the progress total.
  POST /verify      → run the semantic verdict on a batch of candidates. The UI
                      calls this in chunks so it can show real progress and
                      stream confirmed changes in as they resolve.

A candidate is a capture whose raw text changed in the window. 'new' (first
collection) and 'gone' (capture disappeared) are real by definition; 'drift'
needs verification — its raw text moved, but that might be pure volatility.
Requires a versioned backend (git or file).
"""

import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, Body
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/changes", tags=["changes"])

# Shorthand window → absolute UTC ISO. Both backends accept ISO (git via
# --since, file backend via fromisoformat); a relative "N units ago" string
# would only work for git, so we resolve to a concrete instant here.
_REL_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)
_REL_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

# Guardrail: a very wide window on a noisy fleet can enumerate a lot of
# candidates. Cap the list (newest kept) so the payload and the verify loop
# stay bounded; the response flags when it truncates.
_MAX_CANDIDATES = 5000


def _resolve_since(value: str) -> str:
    """
    Turn an operator-friendly window into an absolute UTC ISO timestamp.

      "24h" → now-24h    "90m" → now-90m
      "7d"  → now-7d     "2w"  → now-2w
    A value that isn't shorthand (e.g. an ISO-8601 timestamp) passes through
    untouched, so callers can also request an exact instant.
    """
    m = _REL_RE.match(value.strip())
    if not m:
        return value
    n, unit = int(m.group(1)), m.group(2).lower()
    dt = datetime.utcnow() - timedelta(seconds=n * _REL_SECONDS[unit])
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


# ───────────────────────────────────────────────────────────────────────────
# GET /candidates — cheap enumeration
# ───────────────────────────────────────────────────────────────────────────

@router.get("/candidates")
def candidates(
    request: Request,
    since: str = "24h",
    until: Optional[str] = None,
    capture_type: Optional[str] = None,
    site: Optional[str] = None,
    kind: Optional[str] = None,
):
    """
    Enumerate candidate change events in the window — cheap, no diffing.

    Each row carries the device_id + version pair the verify step (and the
    Semantic Diff deep-link) need. 'drift' rows have needs_verify=true; 'new'
    and 'gone' are real by definition and render immediately.
    """
    storage = request.app.state.storage
    db = request.app.state.db

    try:
        changes = storage.fleet_changes(
            since=_resolve_since(since),
            until=until,
            capture_type=capture_type,
            site=site,
        )
    except NotImplementedError:
        raise HTTPException(
            status_code=501,
            detail="change feed requires a versioned storage backend",
        )

    if kind:
        changes = [c for c in changes if c.kind == kind]

    truncated = len(changes) > _MAX_CANDIDATES
    if truncated:
        changes = changes[:_MAX_CANDIDATES]   # already newest-first

    id_by_key = {
        (d["name"], d["site_slug"]): d["id"]
        for d in db.list_devices()
    }

    rows = [{
        "device": c.device,
        "device_id": id_by_key.get((c.device, c.site)),
        "site": c.site,
        "capture_type": c.capture_type,
        "kind": c.kind,
        "trigger": c.trigger,
        "at": c.at,
        "version_id": c.version_id,
        "prev_version_id": c.prev_version_id,
        "needs_verify": c.kind == "drift",
    } for c in changes]

    return {
        "window": {"since": since, "until": until or "now"},
        "total": len(rows),
        "verify_total": sum(1 for r in rows if r["needs_verify"]),
        "truncated": truncated,
        "candidates": rows,
    }


# ───────────────────────────────────────────────────────────────────────────
# POST /verify — semantic verdict on a batch
# ───────────────────────────────────────────────────────────────────────────

class VerifyItem(BaseModel):
    device_id: int
    capture_type: str
    version_id: str
    prev_version_id: Optional[str] = None


class VerifyRequest(BaseModel):
    items: List[VerifyItem]


@router.post("/verify")
def verify(request: Request, body: VerifyRequest = Body(...)):
    """
    Run the semantic verdict on a batch of candidates.

    device_id is resolved through the DB (never a client-supplied path), so
    verification can't be steered at arbitrary files. Each result echoes the
    candidate plus {real, mode, added, removed, changed}; the UI keeps the
    real ones and drops the rest (volatility only).
    """
    storage = request.app.state.storage
    db = request.app.state.db

    results = []
    for it in body.items:
        dev = db.get_device_by_id(it.device_id)
        if not dev:
            results.append({
                **it.dict(), "real": False, "mode": "unknown",
                "added": 0, "removed": 0, "changed": 0,
            })
            continue
        verdict = storage.classify_change(
            dev["name"], dev["site_slug"], it.capture_type,
            to_version=it.version_id, from_version=it.prev_version_id,
        )
        results.append({**it.dict(), **verdict})

    return {"results": results}