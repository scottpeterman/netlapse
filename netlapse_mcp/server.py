"""
netlapse MCP server — read access to collected network state, parsed-first.

Rules this server enforces (netlapse enforces the same at the token level):

  * Parsed data first. Captures with parsed records are only reachable as
    filtered, paged records (get_records). Raw lines are served only for
    captures that have no parse, and only for a pattern or a line range,
    capped (get_raw_lines).
  * Config is raw-only (never parsed) and is reachable only through
    search_config: matching lines with context, never the whole file.
  * No ad hoc commands. There is no SSH path here; collect_now runs only the
    capture types defined by netlapse jobs, and every call goes through the
    MCP client's tool-approval prompt (keep it off your auto-allow list).

Environment:
  NETLAPSE_URL        e.g. http://jumphost:8888   (default http://127.0.0.1:8888)
  NETLAPSE_TOKEN      a read-only token from `python -m netlapse token`
  NETLAPSE_TIMEOUT    seconds per HTTP call (default 60)
  NETLAPSE_VERIFY_TLS 0 to skip TLS verification for a self-signed https URL
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Dict, List, Optional

import anyio

try:                                                   # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

try:                                                   # mcp >= 2
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:                                    # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError

from .client import NetlapseClient, NetlapseError, one_line, trim

# stdout is the MCP transport — logs must go to stderr only.
logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="netlapse-mcp %(levelname)s %(message)s")
log = logging.getLogger("netlapse_mcp")

INSTRUCTIONS = """\
netlapse holds periodic snapshots of network device state (config, ARP, BGP,
interfaces, LLDP, MAC, OSPF, routes, inventory, version) with history and
semantic diffs. Workflow:

1. Locate devices: find_devices (or inventory_overview for the big picture).
   Device arguments accept a full name, a short name prefix, or an IP.
2. See what a device has: device_summary → capture types, record counts,
   whether each is parsed, and when it was collected.
3. Read parsed data with get_records. Look at `schema` in the first response,
   then narrow with `where` filters (FIELD=value, FIELD~regex, FIELD!=value,
   FIELD!~regex) and `fields`. Page with offset; never try to pull everything.
4. Config is not parsed: use search_config with a regex (fleet-wide, or one
   device with context lines and the enclosing stanza).
5. get_raw_lines is only for captures with no parse (template miss) and
   requires a pattern or line range.
6. History: list_versions → diff (semantic for parsed captures, unified for
   config). Fleet-wide "what changed": recent_changes.
7. Value lookup across everything (IP, MAC, ASN, hostname, serial):
   one_search.
8. Fresh data: collect_now runs the device's configured captures (no
   arbitrary commands) and needs human approval each time.
"""

mcp = _Server(name="netlapse", instructions=INSTRUCTIONS)
_client = NetlapseClient()

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False) \
    if "read_only_hint" in ToolAnnotations.model_fields else \
    ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True) \
    if "read_only_hint" in ToolAnnotations.model_fields else \
    ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


# netlapse API wording → the tool the model should use instead
_HINTS = (
    ("query them with /records (filters + paging)", "use get_records (where filters + paging)"),
    ("search it with a pattern instead", "use search_config"),
    ("raw lines are available with a pattern", "use get_raw_lines with a pattern"),
    ("not permitted for a read-only API token", "not permitted for this token (collect_now needs allow_collect: true on the token in netlapse config)"),
)


async def _run(fn, *args, **kwargs):
    """Run a blocking client call off the event loop. Expected failures
    (gates, not-found, bad filters) are raised as the SDK's ToolError so their
    message — which tells the model what to do instead — reaches it intact;
    any other exception would be reduced to a generic error by the SDK."""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except NetlapseError as e:
        msg = str(e)
        for api_hint, tool_hint in _HINTS:
            msg = msg.replace(api_hint, tool_hint)
        raise ToolError(msg) from None
    except Exception as e:                              # noqa: BLE001
        log.exception("unexpected error")
        raise ToolError(f"{type(e).__name__}: {e}") from None


def _clamp(v: Optional[int], lo: int, hi: int, default: int) -> int:
    try:
        v = int(v) if v is not None else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# ═══════════════════════════════════════════════════════════════════════════
# Inventory
# ═══════════════════════════════════════════════════════════════════════════

def _find_devices(query, site, role, platform, status, limit):
    devs = _client.devices(refresh=True)
    q = (query or "").strip().lower()
    out = []
    for d in devs:
        if site and (d.get("site_slug") or "").lower() != site.lower():
            continue
        if role and (d.get("role_slug") or "").lower() != role.lower():
            continue
        if platform and platform.lower() not in (d.get("platform_slug") or "").lower():
            continue
        if status and (d.get("status") or "").lower() != status.lower():
            continue
        if q and q not in (d.get("name") or "").lower() and q not in (d.get("primary_ip4") or ""):
            continue
        out.append(d)
    rows = [{
        "name": d["name"], "site": d.get("site_slug"), "role": d.get("role_slug"),
        "platform": d.get("platform_slug"), "ip": d.get("primary_ip4"),
        "status": d.get("status"), "collection_enabled": d.get("collection_enabled"),
        "last_collected_at": d.get("last_collected_at"),
        "last_collection_status": d.get("last_collection_status"),
    } for d in sorted(out, key=lambda d: (d.get("site_slug") or "", d.get("name") or ""))]
    return {"matched": len(rows), "returned": min(len(rows), limit),
            "devices": rows[:limit],
            **({"note": f"{len(rows) - limit} more — narrow the filters"} if len(rows) > limit else {})}


@mcp.tool(annotations=RO)
async def find_devices(
    query: Optional[str] = None,
    site: Optional[str] = None,
    role: Optional[str] = None,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Search the device inventory.

    query: substring of the device name or management IP.
    site / role / platform / status: exact slugs (platform is a substring
    match, e.g. "junos"). limit: max rows (≤ 200).
    """
    return await _run(_find_devices, query, site, role, platform, status, _clamp(limit, 1, 200, 50))


def _overview():
    devs = _client.devices(refresh=True)

    def count(key):
        c: Dict[str, int] = {}
        for d in devs:
            k = d.get(key) or "—"
            c[k] = c.get(k, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))
    failing = [d["name"] for d in devs
               if d.get("last_collection_status") not in (None, "success")
               and d.get("collection_enabled")]
    return {
        "devices": len(devs),
        "by_site": count("site_slug"), "by_role": count("role_slug"),
        "by_platform": count("platform_slug"),
        "collection_disabled": sum(1 for d in devs if not d.get("collection_enabled")),
        "last_collection_failed": {"count": len(failing), "devices": sorted(failing)[:40]},
        "capture_types": _client.get("/search/capture_types"),
    }


@mcp.tool(annotations=RO)
async def inventory_overview() -> Dict[str, Any]:
    """Fleet summary: device counts by site/role/platform, devices whose last
    collection failed, and which capture types exist."""
    return await _run(_overview)


def _summary(device):
    d = _client.resolve(device)
    return _client.get(f"/devices/{d['id']}/captures") | {"ip": d.get("primary_ip4")}


@mcp.tool(annotations=RO)
async def device_summary(device: str) -> Dict[str, Any]:
    """One device: identity, last collection result, and every capture it has
    — capture type, command, collected_at, parsed or not, record count,
    template, raw size. Start here before reading data."""
    return await _run(_summary, device)


# ═══════════════════════════════════════════════════════════════════════════
# Parsed records
# ═══════════════════════════════════════════════════════════════════════════

def _records(device, capture_type, where, fields, sort, limit, offset):
    d = _client.resolve(device)
    params: List[tuple] = [("limit", limit), ("offset", offset)]
    for w in where or []:
        params.append(("where", w))
    if fields:
        params.append(("fields", ",".join(fields)))
    if sort:
        params.append(("sort", sort))
    return _client.get(f"/devices/{d['id']}/captures/{capture_type}/records", params)


@mcp.tool(annotations=RO)
async def get_records(
    device: str,
    capture_type: str,
    where: Optional[List[str]] = None,
    fields: Optional[List[str]] = None,
    sort: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    """Parsed (structured) records for one capture on one device.

    where: filters, all must match — "FIELD=value" (case-insensitive),
      "FIELD!=value", "FIELD~regex", "FIELD!~regex". Field names come from
      `schema` in the response (case-insensitive). List-valued fields match if
      any element matches.
    fields: return only these columns. sort: FIELD or -FIELD (numeric aware).
    limit ≤ 200, offset for paging. Response has total, matched, has_more.
    Large tables (full route tables) must be filtered — e.g.
    where=["NETWORK~^10\\.20\\."] — rather than paged through.
    """
    return await _run(_records, device, capture_type, where, fields, sort,
                      _clamp(limit, 1, 200, 50), _clamp(offset, 0, 10**9, 0))


# ═══════════════════════════════════════════════════════════════════════════
# Raw lines (gated) and config search
# ═══════════════════════════════════════════════════════════════════════════

def _fmt_blocks(res: dict) -> dict:
    out = []
    for b in res.get("blocks") or []:
        lines = []
        if b.get("parent"):
            lines.append(f"{b['parent']['n']:>6}  {b['parent']['text']}")
            lines.append("   ...")
        for l in b["lines"]:
            lines.append(f"{l['n']:>6}{'>' if l.get('match') else ' '} {l['text']}")
        out.append("\n".join(lines))
    return {k: v for k, v in res.items() if k != "blocks"} | {"excerpt": "\n--\n".join(out)}


def _raw_lines(device, capture_type, pattern, start, end, context, max_lines):
    if capture_type == "config":
        raise NetlapseError("config is searched with search_config, not get_raw_lines")
    d = _client.resolve(device)
    res = _client.get(f"/devices/{d['id']}/captures/{capture_type}/lines", {
        "pattern": pattern, "start": start, "end": end,
        "context": context, "max_lines": max_lines,
    })
    return _fmt_blocks(res)


@mcp.tool(annotations=RO)
async def get_raw_lines(
    device: str,
    capture_type: str,
    pattern: Optional[str] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    context: int = 2,
    max_lines: int = 100,
) -> Dict[str, Any]:
    """Raw output lines — ONLY for captures that have no parsed records
    (device_summary shows parsed=false). Refused when parsed data exists:
    use get_records instead. Needs a regex `pattern` or a `start`/`end` line
    range. context 0–10 lines around each match; max_lines ≤ 200. Matching
    lines are marked with '>'. Not for config — use search_config.
    """
    return await _run(_raw_lines, device, capture_type, pattern, start, end,
                      _clamp(context, 0, 10, 2), _clamp(max_lines, 1, 200, 100))


def _search_config(pattern, device, site, context, max_devices, max_lines_per_device):
    if not pattern or not pattern.strip():
        raise NetlapseError("pattern is required")
    if device:
        d = _client.resolve(device)
        res = _client.get(f"/devices/{d['id']}/captures/config/lines", {
            "pattern": pattern, "context": context, "max_lines": max_lines_per_device * (2 * context + 1),
        })
        return _fmt_blocks(res)
    results = _client.post("/search", {"pattern": pattern, "capture_type": "config", "site": site}) or []
    results.sort(key=lambda r: (r.get("site") or "", r.get("device") or ""))
    rows = []
    for r in results[:max_devices]:
        m = r.get("matches") or []
        rows.append({
            "device": r.get("device"), "site": r.get("site"), "match_count": len(m),
            "lines": [f"{x['line_number']:>6}  {trim(x['text'], 200)}" for x in m[:max_lines_per_device]],
            **({"more_lines": len(m) - max_lines_per_device} if len(m) > max_lines_per_device else {}),
        })
    total_lines = sum(len(r.get("matches") or []) for r in results)
    return {
        "pattern": pattern, "site": site, "devices_matched": len(results),
        "lines_matched": total_lines, "returned_devices": len(rows),
        **({"note": f"{len(results) - max_devices} more devices — add site= or refine the pattern"}
           if len(results) > max_devices else {}),
        "note_caps": "per-device matches cap at 50 server-side; pass device= for context lines",
        "results": rows,
    }


@mcp.tool(annotations=RO)
async def search_config(
    pattern: str,
    device: Optional[str] = None,
    site: Optional[str] = None,
    context: int = 2,
    max_devices: int = 40,
    max_lines_per_device: int = 15,
) -> Dict[str, Any]:
    """Regex search of current device configs (case-insensitive).

    Without device: fleet-wide (optionally one site) — matching lines per
    device. With device: matching lines with `context` lines around them and,
    for indented configs, the enclosing stanza line (e.g. `interface Et1`).
    Junos configs are stored as `display set`, so each line is self-contained
    — search e.g. "interfaces et-0/0/1 " or "protocols bgp group TRANSIT".
    Never returns a whole config.
    """
    return await _run(_search_config, pattern, device, site, _clamp(context, 0, 10, 2),
                      _clamp(max_devices, 1, 200, 40), _clamp(max_lines_per_device, 1, 50, 15))


# ═══════════════════════════════════════════════════════════════════════════
# One Search
# ═══════════════════════════════════════════════════════════════════════════

def _one_search(token, site, max_devices, max_lines):
    res = _client.post("/search/global", {"token": token, "site": site})
    refs = res.get("references") or []
    grouped: Dict[str, List[dict]] = {}
    for r in refs:
        grouped.setdefault(r["capture_type"], []).append(r)
    by_ct = {}
    for ct, rows in sorted(grouped.items()):
        by_ct[ct] = {
            "devices": len(rows),
            "hits": [{
                "device": r["device"], "site": r.get("site"),
                "lines": [trim(m.get("text") if isinstance(m, dict) else str(m), 200)
                          for m in (r.get("matches") or [])[:max_lines]],
            } for r in rows[:max_devices]],
        }
    inv = res.get("inventory") or []
    return {
        "token": res.get("token"), "kind": res.get("kind"), "counts": res.get("counts"),
        "inventory": inv[:20] if isinstance(inv, list) else inv,
        "references": by_ct,
    }


@mcp.tool(annotations=RO)
async def one_search(token: str, site: Optional[str] = None,
                     max_devices: int = 15, max_lines: int = 3) -> Dict[str, Any]:
    """Find every place a single value appears: IP (octet-anchored), MAC (any
    vendor format), ASN (with or without 'AS'), hostname, serial, VLAN, or
    free text. Returns inventory identity matches plus references grouped by
    capture type (which devices see it in ARP, MAC, BGP, routes, config…)."""
    return await _run(_one_search, token, site, _clamp(max_devices, 1, 100, 15), _clamp(max_lines, 1, 20, 3))


# ═══════════════════════════════════════════════════════════════════════════
# History and diffs
# ═══════════════════════════════════════════════════════════════════════════

def _versions(device, capture_type, limit):
    d = _client.resolve(device)
    vs = _client.get(f"/devices/{d['id']}/snapshots", {"limit": 100}) or []
    if capture_type:
        vs = [v for v in vs if not v.get("capture_types") or capture_type in v["capture_types"]]
    return {"device": d["name"], "capture_type": capture_type,
            "versions": [{"version_id": v["version_id"], "collected_at": v.get("collected_at"),
                          "capture_types": v.get("capture_types"), "trigger": v.get("trigger")}
                         for v in vs[:limit]]}


@mcp.tool(annotations=RO)
async def list_versions(device: str, capture_type: Optional[str] = None, limit: int = 20) -> Dict[str, Any]:
    """Stored snapshot versions for a device (newest first), optionally only
    those containing capture_type. Use version_id values with diff."""
    return await _run(_versions, device, capture_type, _clamp(limit, 1, 100, 20))


def _diff(device, capture_type, from_version, to_version, max_items, max_diff_lines):
    d = _client.resolve(device)
    res = _client.get(f"/devices/{d['id']}/diff", {
        "capture_type": capture_type, "from_commit": from_version,
        "to_commit": to_version, "compact": "true",
    })
    out = {k: res.get(k) for k in ("device", "capture_type", "from_version", "to_version",
                                    "from_time", "to_time", "key_fields", "ignored")}
    added, removed, changed = res.get("added") or [], res.get("removed") or [], res.get("changed") or []
    out["counts"] = {"added": len(added), "removed": len(removed), "changed": len(changed)}
    if added or removed or changed:
        out["added"] = added[:max_items]
        out["removed"] = removed[:max_items]
        out["changed"] = changed[:max_items]
        if max(len(added), len(removed), len(changed)) > max_items:
            out["note"] = f"lists capped at {max_items} each"
    raw = (res.get("raw_diff") or "").splitlines()
    if raw and not (added or removed or changed):
        # Unparsed capture (config): the unified diff is the change.
        out["unified_diff"] = "\n".join(raw[:max_diff_lines])
        if len(raw) > max_diff_lines:
            out["unified_diff_truncated"] = len(raw) - max_diff_lines
    return out


@mcp.tool(annotations=RO)
async def diff(
    device: str,
    capture_type: str,
    from_version: Optional[str] = None,
    to_version: Optional[str] = None,
    max_items: int = 50,
    max_diff_lines: int = 200,
) -> Dict[str, Any]:
    """Diff two versions of one capture (default: previous vs latest).
    Parsed captures give a semantic diff — added / removed / changed records
    keyed on identity fields, with volatile fields (ages, counters) ignored.
    Config gives a unified diff (changed lines with context only)."""
    return await _run(_diff, device, capture_type, from_version, to_version,
                      _clamp(max_items, 1, 200, 50), _clamp(max_diff_lines, 10, 500, 200))


def _recent_changes(since, site, capture_type, verify, limit):
    cand = _client.get("/changes/candidates", {"since": since, "site": site, "capture_type": capture_type})
    rows = cand.get("candidates") or []
    real: List[dict] = []
    unverified = 0
    to_verify = [r for r in rows if r.get("needs_verify") and r.get("device_id")]
    if verify:
        budget = to_verify[:300]
        unverified = len(to_verify) - len(budget)
        verdicts: Dict[tuple, dict] = {}
        for i in range(0, len(budget), 50):
            batch = budget[i:i + 50]
            res = _client.post("/changes/verify", {"items": [{
                "device_id": r["device_id"], "capture_type": r["capture_type"],
                "version_id": r["version_id"], "prev_version_id": r.get("prev_version_id"),
            } for r in batch]})
            for v in res.get("results") or []:
                verdicts[(v["device_id"], v["capture_type"], v["version_id"])] = v
        for r in rows:
            if r.get("needs_verify"):
                v = verdicts.get((r.get("device_id"), r["capture_type"], r["version_id"]))
                if v is None:
                    continue
                if not v.get("real"):
                    continue
                r = r | {"added": v.get("added"), "removed": v.get("removed"), "changed": v.get("changed")}
            real.append(r)
    else:
        real = rows
    slim = [{k: r.get(k) for k in ("at", "device", "site", "capture_type", "kind",
                                   "added", "removed", "changed", "version_id", "prev_version_id")
             if r.get(k) is not None} for r in real]
    by_ct: Dict[str, int] = {}
    for r in slim:
        by_ct[r["capture_type"]] = by_ct.get(r["capture_type"], 0) + 1
    return {
        "window": cand.get("window"), "candidates": len(rows),
        "real_changes": len(slim), "by_capture_type": by_ct,
        **({"unverified_skipped": unverified} if unverified else {}),
        **({"truncated_by_server": True} if cand.get("truncated") else {}),
        "changes": slim[:limit],
        **({"note": f"{len(slim) - limit} more — narrow by site or capture_type"} if len(slim) > limit else {}),
    }


@mcp.tool(annotations=RO)
async def recent_changes(
    since: str = "24h",
    site: Optional[str] = None,
    capture_type: Optional[str] = None,
    verify: bool = True,
    limit: int = 100,
) -> Dict[str, Any]:
    """Fleet-wide change feed: which devices' captures actually changed in
    the window. since: '90m', '24h', '7d', '2w' or an ISO timestamp.
    verify=true runs the semantic check so volatility (ARP ages, counters,
    config timestamps) is dropped and only real changes remain (up to 300
    verified per call). Follow up with diff(device, capture_type,
    from_version=prev_version_id, to_version=version_id)."""
    return await _run(_recent_changes, since, site, capture_type, verify, _clamp(limit, 1, 500, 100))


# ═══════════════════════════════════════════════════════════════════════════
# Jobs / collection health
# ═══════════════════════════════════════════════════════════════════════════

def _jobs():
    jobs = _client.get("/jobs") or []
    sched = _client.get("/status/scheduler")
    return {
        "scheduler": {k: sched.get(k) for k in ("running", "active_jobs", "active_job_slugs",
                                                 "queued_collections", "adhoc", "reason") if k in sched},
        "jobs": [{
            "slug": j.get("slug"), "capture_type": j.get("capture_type"),
            "enabled": bool(j.get("enabled")), "interval_seconds": j.get("interval_seconds"),
            "last_run": j.get("last_run"), "next_run": j.get("next_run_at"),
            "last_status": j.get("last_status"), "devices_collected": j.get("devices_collected"),
            "devices_failed": j.get("devices_failed"), "duration_seconds": j.get("duration_seconds"),
        } for j in jobs],
    }


@mcp.tool(annotations=RO)
async def collection_status() -> Dict[str, Any]:
    """Scheduler state and every collection job: capture type, interval, last
    run, success/failure counts, next run."""
    return await _run(_jobs)


# ═══════════════════════════════════════════════════════════════════════════
# Collect Now (requires approval in the MCP client)
# ═══════════════════════════════════════════════════════════════════════════

FINAL = {"success", "partial", "failed"}


def _slim_request(r: dict) -> dict:
    return {
        "request_id": r.get("id"), "device": r.get("device"), "status": r.get("status"),
        "phase": r.get("phase"), "elapsed": r.get("elapsed"),
        "error_category": r.get("error_category"), "error_message": r.get("error_message"),
        "captures": [{
            "capture_type": c.get("capture_type"), "command": c.get("command"),
            "status": c.get("status"), "bytes": c.get("bytes"), "parsed": c.get("parsed"),
            **({"error": one_line(c["error"])} if c.get("error") else {}),
        } for c in r.get("captures") or []],
        **({"unresolved_capture_types": r["unresolved"]} if r.get("unresolved") else {}),
    }


def _collect(device, capture_types, wait_seconds):
    d = _client.resolve(device)
    params = {"capture_type": ",".join(capture_types)} if capture_types else None
    req = _client.post(f"/collect/{d['id']}", params=params)
    note = None if req.get("created", True) else "a collection for this device was already running — returning it"
    deadline = time.time() + wait_seconds
    while req.get("status") not in FINAL and time.time() < deadline:
        time.sleep(1.5)
        req = _client.get(f"/collect/requests/{req['id']}")
    out = _slim_request(req)
    if note:
        out["note"] = note
    if out["status"] not in FINAL:
        out["note"] = (out.get("note") or "") + f" still running after {wait_seconds}s — poll collect_status"
    return out


@mcp.tool(annotations=ACT)
async def collect_now(device: str, capture_types: Optional[List[str]] = None,
                      wait_seconds: int = 90) -> Dict[str, Any]:
    """Collect fresh data from one device NOW over SSH, then return per-capture
    results. Runs only the capture types netlapse jobs define for the
    device's platform — there is no way to send other commands. capture_types:
    e.g. ["bgp","interfaces"]; omit for all of the device's captures (one SSH
    session). Waits up to wait_seconds (≤ 300) for completion. Touches a
    production device — use only when stored data is too old for the question.
    """
    return await _run(_collect, device, capture_types, _clamp(wait_seconds, 0, 300, 90))


@mcp.tool(annotations=RO)
async def collect_status(request_id: str) -> Dict[str, Any]:
    """Status of a collect_now request: per-capture progress and the outcome."""
    return await _run(lambda: _slim_request(_client.get(f"/collect/requests/{request_id}")))


def main() -> None:
    mcp.run()          # stdio


if __name__ == "__main__":
    main()
