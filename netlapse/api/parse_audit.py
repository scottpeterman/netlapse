"""
Netlapse Parse Audit API — collection-centric parse inspection + reparse.

The Template Lab tests parsing one paste at a time, and the device detail
view shows parsed data one device at a time. Neither answers the operator's
real question: "for capture type X, how is the parser doing across the whole
fleet, and where are the misses?"

This router provides that view, plus the ability to reparse stored raw output
after a template is added/edited/removed — without re-running SSH collection.

Endpoints (prefix /api/v1/parse-audit):

  GET  /                      → fleet-wide parse status for one capture type
  POST /reparse               → preview (dry-run) or apply (persist) a reparse

Read path is cheap: it reads already-stored parsed JSON via the storage
backend, no SSH and no template matching. The reparse path re-runs the parse
engine against stored raw text and (when persist=true) writes the new parsed
artifact back through storage.update_parsed().
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from netlapse.parser.engine import ParseResult, is_unparsed_capture
from netlapse.storage.backend import Snapshot

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/parse-audit", tags=["parse-audit"])


# Default classification thresholds (overridable per request).
# Mirrors parser.engine.DEFAULT_MIN_SCORE (15.0) for the miss boundary.
DEFAULT_MIN_SCORE = 15.0
DEFAULT_OK_SCORE = 40.0


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────

def _parser_or_error(request: Request):
    """Return (parser, None) or (None, JSONResponse)."""
    parser = getattr(request.app.state, "parser", None)
    if parser is None:
        return None, JSONResponse(
            status_code=503,
            content={
                "error": "Parse engine not available",
                "detail": "tfsm_templates.db not found — place at "
                          "~/.netlapse/tfsm_templates.db to enable",
            },
        )
    return parser, None


def _classify(
    capture_type: str,
    parsed_data: Optional[dict],
    score: Optional[float],
    min_score: float,
    ok_score: float,
) -> tuple[str, int, int]:
    """
    Return (status, record_count, field_count) for a stored snapshot.

    Status values:
      ok        — parsed well (score >= ok_score)
      low       — parsed but weak (min_score <= score < ok_score)
      miss      — has raw output but parsing failed / scored below min
      raw_only  — capture type isn't expected to parse (e.g. config)
    """
    records = (parsed_data or {}).get("records") if parsed_data else None
    record_count = len(records) if records else 0
    field_count = len(records[0].keys()) if records else 0

    if record_count == 0:
        # No structured records. Config is stored raw by design, so a missing
        # parse there is expected, not a failure.
        if capture_type == "config":
            return "raw_only", 0, 0
        return "miss", 0, 0

    s = score if score is not None else 0.0
    if s >= ok_score:
        return "ok", record_count, field_count
    if s >= min_score:
        return "low", record_count, field_count
    return "miss", record_count, field_count


def _family_mismatch(capture_type: str, template_name: Optional[str]) -> bool:
    """
    Heuristic flag for a parse that matched the wrong template family.

    A 'show bgp summary' that scored on 'juniper_junos_show_chassis_firmware'
    is the classic false positive: the template name carries no token from the
    capture type. Used purely as a UI hint, never to suppress data.
    """
    if not template_name:
        return False
    ct = capture_type.lower()
    name = template_name.lower()
    if ct == "config":
        return False
    # Direct token hit, or common command synonyms per capture type.
    synonyms = {
        "arp": ("arp",),
        "bgp": ("bgp",),
        "routes": ("route", "routing", "rib", "fib"),
        "interfaces": ("interface", "intf", "port"),
    }
    tokens = synonyms.get(ct, (ct,))
    return not any(tok in name for tok in tokens)


# ───────────────────────────────────────────────────────────────────────────
# GET / — fleet-wide audit for one capture type
# ───────────────────────────────────────────────────────────────────────────

@router.get("")
def audit(
    request: Request,
    capture_type: str,
    site: Optional[str] = None,
    platform: Optional[str] = None,
    enabled_only: bool = True,
    min_score: float = DEFAULT_MIN_SCORE,
    ok_score: float = DEFAULT_OK_SCORE,
):
    """
    Report how every device's latest snapshot of `capture_type` parsed.

    Read-only and cheap: reads stored parsed JSON, no SSH, no re-matching.
    Rows are returned worst-first (miss → low → ok), then by score, so the
    failures the operator is hunting for sit at the top.
    """
    db = request.app.state.db
    storage = request.app.state.storage

    devices = db.list_devices(
        site=site,
        platform=platform,
        collection_enabled=True if enabled_only else None,
    )

    rows: List[dict] = []
    summary = {"ok": 0, "low": 0, "miss": 0, "raw_only": 0, "no_data": 0}

    for d in devices:
        name = d["name"]
        site_slug = d["site_slug"]
        snap = storage.fetch_latest(name, site_slug, capture_type)

        if snap is None:
            summary["no_data"] += 1
            rows.append({
                "device_id": d["id"],
                "device": name,
                "site": site_slug,
                "platform": d.get("platform_name"),
                "platform_profile": d.get("platform_profile"),
                "has_data": False,
                "template_name": None,
                "template_score": None,
                "record_count": 0,
                "field_count": 0,
                "collected_at": None,
                "status": "no_data",
                "family_mismatch": False,
            })
            continue

        status, record_count, field_count = _classify(
            capture_type, snap.parsed_data, snap.template_score,
            min_score, ok_score,
        )
        summary[status] += 1

        rows.append({
            "device_id": d["id"],
            "device": name,
            "site": site_slug,
            "platform": d.get("platform_name"),
            "platform_profile": d.get("platform_profile"),
            "has_data": True,
            "template_name": snap.template_name,
            "template_score": snap.template_score,
            "record_count": record_count,
            "field_count": field_count,
            "collected_at": (
                snap.collected_at.isoformat() if snap.collected_at else None
            ),
            "status": status,
            "family_mismatch": _family_mismatch(
                capture_type, snap.template_name
            ),
        })

    # Worst-first ordering: miss/no_data, then low, then raw_only, then ok.
    order = {"miss": 0, "no_data": 1, "low": 2, "raw_only": 3, "ok": 4}
    rows.sort(key=lambda r: (
        order.get(r["status"], 9),
        r["template_score"] if r["template_score"] is not None else -1,
    ))

    return {
        "capture_type": capture_type,
        "total": len(rows),
        "summary": summary,
        "thresholds": {"min_score": min_score, "ok_score": ok_score},
        "rows": rows,
    }


# ───────────────────────────────────────────────────────────────────────────
# POST /reparse — preview (dry-run) or apply (persist)
# ───────────────────────────────────────────────────────────────────────────

class ReparseRequest(BaseModel):
    capture_type: str
    device_ids: Optional[List[int]] = None   # None = every device with data
    persist: bool = False                     # False = dry-run preview
    site: Optional[str] = None
    platform: Optional[str] = None


def _delta(old_score, new_score, old_records, new_records, new_success) -> str:
    """Label what a reparse did to one device, relative to the old parse."""
    old_s = old_score or 0.0
    new_s = new_score or 0.0
    had_old = bool(old_records) or bool(old_score)

    if not new_success:
        return "cleared" if had_old else "still_miss"
    if not had_old:
        return "recovered"
    if new_s > old_s + 0.05:
        return "improved"
    if new_s < old_s - 0.05:
        return "regressed"
    return "unchanged"


@router.post("/reparse")
def reparse(request: Request, body: ReparseRequest):
    """
    Reparse stored raw output for a capture type against the current
    template database.

    persist=false (default): dry run. Computes what the parse engine would
    produce now and returns old→new for each device. Writes nothing.

    persist=true: writes the new parsed artifact back via
    storage.update_parsed() (overwrite for file backend, reparse commit for
    git backend). Raw text is never touched.
    """
    parser, err = _parser_or_error(request)
    if err:
        return err

    db = request.app.state.db
    storage = request.app.state.storage
    ct = body.capture_type

    # Resolve the target device set.
    if body.device_ids:
        targets = [
            db.get_device_by_id(i) for i in body.device_ids
        ]
        targets = [t for t in targets if t]
    else:
        targets = db.list_devices(
            site=body.site,
            platform=body.platform,
            collection_enabled=True,
        )

    results: List[dict] = []
    summary = {
        "improved": 0, "regressed": 0, "unchanged": 0,
        "recovered": 0, "cleared": 0, "still_miss": 0, "skipped": 0,
    }
    persisted_count = 0

    for d in targets:
        name = d["name"]
        site_slug = d["site_slug"]
        profile = d.get("platform_profile") or ""

        snap = storage.fetch_latest(name, site_slug, ct)
        if snap is None or not snap.raw_text:
            summary["skipped"] += 1
            continue

        old_template = snap.template_name
        old_score = snap.template_score
        old_records = 0
        if snap.parsed_data and snap.parsed_data.get("records"):
            old_records = len(snap.parsed_data["records"])

        # Re-run the parse engine. Prefer the originally captured command;
        # fall back to the capture type as a filter hint when none was stored
        # (which is exactly the case for snapshots that never parsed).
        command = snap.command or ct
        if is_unparsed_capture(ct):
            # Raw-only capture type: never parsed. A persisted reparse writes
            # a records-free envelope, clearing any parse stored before this
            # type was excluded.
            result = ParseResult(success=False, error="capture type is not parsed")
        else:
            result = parser.parse(
                raw_output=snap.raw_text,
                platform_profile=profile,
                command=command,
            )
        if not result.success and not is_unparsed_capture(ct):
            result = parser.parse_vendor_fallback(
                raw_output=snap.raw_text,
                platform_profile=profile,
            )

        new_template = result.template if result.success else None
        new_score = result.score if result.success else result.score
        new_records = result.record_count if result.success else 0

        label = _delta(
            old_score, result.score, old_records, new_records, result.success
        )
        summary[label] += 1

        persisted = False
        if body.persist:
            # Build a snapshot carrying the new parse and write parsed-only.
            new_snap = Snapshot(
                device=name,
                site=site_slug,
                capture_type=ct,
                command=command,
                raw_text=snap.raw_text,
                collected_at=snap.collected_at,
                parsed_data=(
                    {"records": result.records} if result.success else None
                ),
                template_name=new_template,
                template_score=(result.score if result.success else None),
            )
            try:
                storage.update_parsed(new_snap)
                persisted = True
                persisted_count += 1
            except NotImplementedError:
                return JSONResponse(
                    status_code=501,
                    content={
                        "error": "Storage backend does not support reparse",
                        "detail": type(storage).__name__,
                    },
                )

        results.append({
            "device_id": d["id"],
            "device": name,
            "site": site_slug,
            "old": {
                "template": old_template,
                "score": old_score,
                "records": old_records,
            },
            "new": {
                "template": new_template,
                "score": (result.score if result.success else None),
                "records": new_records,
            },
            "delta": label,
            "persisted": persisted,
        })

    logger.info(
        "Reparse %s: %d devices, persist=%s, persisted=%d (%s)",
        ct, len(results), body.persist, persisted_count, summary,
    )

    return {
        "capture_type": ct,
        "persist": body.persist,
        "total": len(results),
        "persisted": persisted_count,
        "summary": summary,
        "results": results,
    }