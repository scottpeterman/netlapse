"""
Capture read API — metadata, filtered parsed records, and gated raw lines.

Built for programmatic consumers (the MCP server, scripts) that must not pull
whole captures: a full-table route capture can be hundreds of MB raw and
millions of parsed records. Every endpoint here filters and pages on the
server and returns a bounded response.

  GET /api/v1/devices/{id}/captures
        What this device has: capture type, command, template, record count,
        sizes, collected time. No content.

  GET /api/v1/devices/{id}/captures/{capture_type}/records
        Parsed records, filtered server-side.
          where   repeatable: FIELD=value | FIELD!=value | FIELD~regex | FIELD!~regex
                  (field names case-insensitive; '=' is case-insensitive equality;
                  list-valued fields match if any element matches)
          fields  comma-separated subset of columns to return
          sort    FIELD or -FIELD
          limit   ≤ 500 (default 50)   offset  for paging

  GET /api/v1/devices/{id}/captures/{capture_type}/lines
        Raw lines — gated. Refused when the capture has parsed records (use
        /records) unless it is a raw-only type (config). Needs a pattern
        (regex) or a start/end line range; config requires a pattern. Matches
        come back with `context` lines around them, and for indented configs
        the enclosing parent line (e.g. `interface Ethernet1`). Capped at
        max_lines (≤ 400).
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from netlapse.parser.engine import is_unparsed_capture

router = APIRouter(prefix="/api/v1/devices", tags=["captures"])

MAX_RECORDS = 500
MAX_LINES = 400
_HEADER_BYTES = 4096
_CT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _device_and_dir(request: Request, device_id: int):
    db = request.app.state.db
    storage = request.app.state.storage
    device = db.get_device_by_id(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="device not found")
    ddir = storage._device_dir(device["site_slug"], device["name"])
    return device, storage, ddir


def _check_ct(capture_type: str) -> None:
    if not _CT_RE.match(capture_type or ""):
        raise HTTPException(status_code=400, detail="invalid capture type")


def _envelope_header(path: Path) -> Dict[str, Any]:
    """Read the small metadata keys from a parsed envelope without loading
    the records (they come last in the file)."""
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(_HEADER_BYTES)
    out: Dict[str, Any] = {}
    for key in ("command", "template", "template_score", "collected_at", "record_count"):
        m = re.search(r'"%s":\s*("(?:[^"\\]|\\.)*"|-?[0-9.]+|null)' % key, head)
        if m:
            try:
                out[key] = json.loads(m.group(1))
            except ValueError:
                pass
    return out


def _iso_mtime(p: Path) -> Optional[str]:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return None


@router.get("/{device_id}/captures")
def list_device_captures(request: Request, device_id: int):
    device, storage, ddir = _device_and_dir(request, device_id)
    caps = []
    if ddir.is_dir():
        for raw in sorted(ddir.glob("*.txt")):
            ct = raw.stem
            parsed = ddir / storage._parsed_filename(ct)
            hdr = _envelope_header(parsed)
            caps.append({
                "capture_type": ct,
                "command": hdr.get("command"),
                "collected_at": _iso_mtime(raw),
                "raw_bytes": raw.stat().st_size,
                "parsed": bool(hdr.get("record_count")),
                "record_count": hdr.get("record_count") or 0,
                "template": hdr.get("template"),
                "template_score": hdr.get("template_score"),
                "raw_only": is_unparsed_capture(ct),
            })
    return {
        "device": device["name"], "device_id": device_id,
        "site": device.get("site_slug"), "platform": device.get("platform_slug"),
        "role": device.get("role_slug"), "status": device.get("status"),
        "last_collected_at": device.get("last_collected_at"),
        "last_collection_status": device.get("last_collection_status"),
        "captures": caps,
    }


# ── records ───────────────────────────────────────────────────────────────

_WHERE_RE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*(!=|!~|=|~)\s*(.*)$")


def _compile_where(clauses: List[str], schema: List[str]):
    by_lower = {c.lower(): c for c in schema}
    preds = []
    for raw in clauses:
        m = _WHERE_RE.match(raw or "")
        if not m:
            raise HTTPException(status_code=400,
                                detail=f"bad where clause {raw!r} — use FIELD=value, FIELD~regex, FIELD!=value or FIELD!~regex")
        field, op, val = m.group(1), m.group(2), m.group(3).strip()
        col = by_lower.get(field.lower())
        if col is None:
            raise HTTPException(status_code=400,
                                detail=f"unknown field {field!r}; fields: {', '.join(schema)}")
        if op in ("~", "!~"):
            try:
                rx = re.compile(val, re.IGNORECASE)
            except re.error as e:
                raise HTTPException(status_code=400, detail=f"bad regex for {field}: {e}")
            test = (lambda rx: lambda v: bool(rx.search(v)))(rx)
        else:
            low = val.lower()
            test = (lambda low: lambda v: v.lower() == low)(low)
        negate = op.startswith("!")

        def pred(rec, col=col, test=test, negate=negate):
            v = rec.get(col)
            vals = [str(x) for x in v] if isinstance(v, list) else ["" if v is None else str(v)]
            hit = any(test(x) for x in vals)
            return not hit if negate else hit
        preds.append(pred)
    return preds


@router.get("/{device_id}/captures/{capture_type}/records")
def capture_records(
    request: Request,
    device_id: int,
    capture_type: str,
    where: List[str] = Query(default=[]),
    fields: Optional[str] = None,
    sort: Optional[str] = None,
    limit: int = Query(50, ge=1, le=MAX_RECORDS),
    offset: int = Query(0, ge=0),
):
    _check_ct(capture_type)
    device, storage, ddir = _device_and_dir(request, device_id)
    if is_unparsed_capture(capture_type):
        raise HTTPException(status_code=409,
                            detail=f"'{capture_type}' is stored raw only and never parsed — search it with a pattern instead")
    path = ddir / storage._parsed_filename(capture_type)
    if not path.is_file():
        raw_exists = (ddir / storage._raw_filename(capture_type)).is_file()
        raise HTTPException(status_code=404, detail=(
            f"no parsed data for {capture_type} (template miss) — raw lines are available with a pattern"
            if raw_exists else f"{device['name']} has no {capture_type} capture"))
    try:
        env = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="stored parsed data is corrupt")
    records = env.get("records") or []
    if not records:
        raise HTTPException(status_code=404, detail=(
            f"no parsed data for {capture_type} (template miss) — raw lines are available with a pattern"))

    schema: List[str] = list(records[0].keys())
    seen = set(schema)
    for r in records[1:50]:
        for k in r.keys():
            if k not in seen:
                schema.append(k)
                seen.add(k)

    preds = _compile_where(where, schema)
    matched = [r for r in records if all(p(r) for p in preds)] if preds else records

    if sort:
        desc = sort.startswith("-")
        key = sort.lstrip("-")
        col = {c.lower(): c for c in schema}.get(key.lower())
        if col is None:
            raise HTTPException(status_code=400, detail=f"unknown sort field {key!r}")

        def sk(r):
            v = r.get(col)
            v = v[0] if isinstance(v, list) and v else v
            s = "" if v is None else str(v)
            try:
                return (0, float(s), "")
            except ValueError:
                return (1, 0.0, s.lower())
        matched = sorted(matched, key=sk, reverse=desc)

    cols = schema
    if fields:
        want = [f.strip() for f in fields.split(",") if f.strip()]
        by_lower = {c.lower(): c for c in schema}
        bad = [f for f in want if f.lower() not in by_lower]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown field(s) {', '.join(bad)}; fields: {', '.join(schema)}")
        cols = [by_lower[f.lower()] for f in want]

    page = matched[offset:offset + limit]
    if cols is not schema:
        page = [{c: r.get(c) for c in cols} for r in page]
    return {
        "device": device["name"], "capture_type": capture_type,
        "command": env.get("command"), "collected_at": env.get("collected_at"),
        "template": env.get("template"), "template_score": env.get("template_score"),
        "schema": schema, "total": len(records), "matched": len(matched),
        "offset": offset, "limit": limit, "returned": len(page),
        "has_more": offset + len(page) < len(matched),
        "records": page,
    }


# ── raw lines (gated) ─────────────────────────────────────────────────────

def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


@router.get("/{device_id}/captures/{capture_type}/lines")
def capture_lines(
    request: Request,
    device_id: int,
    capture_type: str,
    pattern: Optional[str] = None,
    start: Optional[int] = Query(None, ge=1),
    end: Optional[int] = Query(None, ge=1),
    context: int = Query(2, ge=0, le=10),
    max_lines: int = Query(200, ge=1, le=MAX_LINES),
):
    _check_ct(capture_type)
    device, storage, ddir = _device_and_dir(request, device_id)
    raw_only = is_unparsed_capture(capture_type)

    if not raw_only:
        hdr = _envelope_header(ddir / storage._parsed_filename(capture_type))
        if hdr.get("record_count"):
            raise HTTPException(status_code=409, detail=(
                f"{capture_type} has {hdr['record_count']} parsed records — query them with "
                "/records (filters + paging); raw lines are only served for captures without a parse"))
    if raw_only and not pattern:
        raise HTTPException(status_code=400, detail=f"{capture_type} is searched by pattern only")
    if not pattern and not (start and end):
        raise HTTPException(status_code=400, detail="give a pattern, or a start and end line")

    path = ddir / storage._raw_filename(capture_type)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"{device['name']} has no {capture_type} capture")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    blocks: List[Dict[str, Any]] = []
    emitted = 0
    truncated = False
    total_hits = 0

    if pattern:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"bad regex: {e}")
        hits = [i for i, l in enumerate(lines) if rx.search(l)]
        total_hits = len(hits)
        last_end = -1
        for i in hits:
            lo, hi = max(0, i - context), min(len(lines) - 1, i + context)
            if lo <= last_end:                 # merge into the previous block
                if not blocks:
                    continue
                add = range(last_end + 1, hi + 1)
                if emitted + len(add) > max_lines:
                    truncated = True
                    break
                blocks[-1]["lines"].extend({"n": j + 1, "text": lines[j], "match": bool(rx.search(lines[j]))} for j in add)
                emitted += len(add)
                last_end = hi
                continue
            parent = None
            if raw_only and _indent(lines[i]) > 0:
                ind = _indent(lines[i])
                for j in range(i - 1, -1, -1):
                    if lines[j].strip() and _indent(lines[j]) < ind:
                        if j < lo:
                            parent = {"n": j + 1, "text": lines[j]}
                        break
            size = hi - lo + 1
            if emitted + size > max_lines:
                truncated = True
                break
            blocks.append({
                "parent": parent,
                "lines": [{"n": j + 1, "text": lines[j], "match": bool(rx.search(lines[j]))}
                          for j in range(lo, hi + 1)],
            })
            emitted += size
            last_end = hi
    else:
        lo, hi = start - 1, min(end, len(lines)) - 1
        if hi < lo:
            raise HTTPException(status_code=400, detail="end must be ≥ start")
        if hi - lo + 1 > max_lines:
            hi = lo + max_lines - 1
            truncated = True
        blocks.append({"parent": None, "lines": [{"n": j + 1, "text": lines[j], "match": False}
                                                 for j in range(lo, hi + 1)]})
        emitted = hi - lo + 1

    return {
        "device": device["name"], "capture_type": capture_type,
        "collected_at": _iso_mtime(path), "total_lines": len(lines),
        "pattern": pattern, "matches": total_hits if pattern else None,
        "returned_lines": emitted, "truncated": truncated, "blocks": blocks,
    }
