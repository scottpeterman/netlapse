"""
Ad-hoc (Collect Now) collection — planning and request tracking.

Two pieces, both free of SSH/DB side effects so the API can use them without
the scheduler running:

  resolve_adhoc_plan()  Which captures run for one device, and with which
                        commands. Mirrors the scheduler: a job applies to a
                        device when its site/role/platform/device filters
                        match, and commands come from the shared resolver
                        (resolve_commands) so Collect Now runs exactly what
                        the scheduled job would for that platform.

  AdhocTracker          In-memory, thread-safe state for each Collect Now
                        request: queued → running → success/partial/failed,
                        with a per-command row (pending/running/ok/failed).
                        The API serves it to the device view, which polls it.

Plan rules
----------
"All captures" (no capture types requested) = every ENABLED job whose filters
match the device, one entry per capture_type. A disabled or tombstoned job is
left out on purpose: it is off for a reason (a full-table route dump on an
edge box, say), and Collect Now should not quietly override that.

An explicitly requested capture_type is honored even when its job is disabled
or its filters don't select the device: the operator asked for that capture
on this device by name. Preference order: enabled+matching, any matching,
enabled, any.

The collection_enabled flag and the job's status filter are ignored for
ad-hoc runs — both are scheduling gates, and Collect Now is an explicit
operator action on one device.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from netlapse.registry.resolver import resolve_commands


# Terminal request states. Anything else is in flight.
FINAL_STATES = {"success", "partial", "failed"}


# ═══════════════════════════════════════════════════════════════════════════
# Planning
# ═══════════════════════════════════════════════════════════════════════════

def _csv(value: Optional[str]) -> List[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _like_to_regex(pattern: str) -> "re.Pattern[str]":
    """SQLite LIKE → regex. % = any run, _ = one char, ASCII case-insensitive
    (SQLite's default LIKE), no escape character — same semantics the
    scheduler's list_collection_targets() query applies."""
    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE | re.DOTALL)


def job_matches_device(job: Dict[str, Any], device: Dict[str, Any]) -> bool:
    """True when the job's site/role/platform/device filters select `device`."""
    checks = (
        ("site_filter", "site_slug"),
        ("role_filter", "role_slug"),
        ("platform_filter", "platform_slug"),
    )
    for job_key, dev_key in checks:
        wanted = _csv(job.get(job_key))
        if wanted and (device.get(dev_key) or "") not in wanted:
            return False
    pattern = (job.get("device_filter") or "").strip()
    if pattern and not _like_to_regex(pattern).match(device.get("name") or ""):
        return False
    return True


def _job_commands(job: Dict[str, Any], platform_slug: str) -> List[str]:
    raw = job.get("commands")
    try:
        default_commands = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        default_commands = []
    if not isinstance(default_commands, list):
        default_commands = []
    return [
        c for c in resolve_commands(
            default_commands, job.get("command_map"), platform_slug
        ) if c
    ]


def resolve_adhoc_plan(
    jobs: Iterable[Dict[str, Any]],
    device: Dict[str, Any],
    capture_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Build the ad-hoc collection plan for one device.

    Args:
        jobs: Rows from db.list_jobs() (v_job_summary).
        device: Row from db.get_device_by_id() (v_device_detail).
        capture_types: Capture types to collect. None/empty = all captures
            whose enabled job applies to this device.

    Returns:
        [{capture_type, job_slug, job_name, enabled, matched, commands}]
        sorted by capture_type, one entry per capture_type. Requested types
        with no job at all are returned with commands=[] and job_slug=None so
        the caller can report them instead of silently dropping them.
    """
    platform_slug = device.get("platform_slug") or ""
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        ct = (job.get("capture_type") or "").strip()
        if ct:
            by_type.setdefault(ct, []).append(job)

    explicit = bool(capture_types)
    wanted = list(dict.fromkeys(capture_types)) if explicit else sorted(by_type)

    plan: List[Dict[str, Any]] = []
    for ct in wanted:
        candidates = sorted(by_type.get(ct, []), key=lambda j: j.get("slug") or "")
        scored = []
        for job in candidates:
            enabled = bool(job.get("enabled"))
            matched = job_matches_device(job, device)
            if not explicit and not (enabled and matched):
                continue
            # Lower is better: enabled+matched, matched, enabled, anything.
            rank = (0 if matched else 2) + (0 if enabled else 1)
            scored.append((rank, job, enabled, matched))

        if not scored:
            if explicit:
                plan.append({
                    "capture_type": ct, "job_slug": None, "job_name": None,
                    "enabled": False, "matched": False, "commands": [],
                })
            continue

        scored.sort(key=lambda t: t[0])
        _, job, enabled, matched = scored[0]
        plan.append({
            "capture_type": ct,
            "job_slug": job.get("slug"),
            "job_name": job.get("name"),
            "enabled": enabled,
            "matched": matched,
            "commands": _job_commands(job, platform_slug),
        })

    plan.sort(key=lambda p: p["capture_type"])
    return plan


# ═══════════════════════════════════════════════════════════════════════════
# Request tracking
# ═══════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AdhocTracker:
    """
    Thread-safe registry of Collect Now requests.

    Written from scheduler worker threads, read from API threadpool threads.
    Every read returns a deep copy, so callers never see a half-updated
    request. Finished requests are kept for `retain_seconds` so a page that
    polls a little late (or reloads) still sees the outcome; the registry is
    also capped at `max_entries`, oldest first.

    State is process-local and not persisted: a restart forgets in-flight
    and recent requests (the collection itself is still recorded normally
    in storage and on the device row).
    """

    def __init__(self, retain_seconds: int = 900, max_entries: int = 500):
        self._lock = threading.Lock()
        self._requests: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._retain = retain_seconds
        self._max = max_entries

    # ── creation ──────────────────────────────────────────────────────

    def create(
        self,
        device: Dict[str, Any],
        plan: List[Dict[str, Any]],
        trigger: str = "ui",
    ) -> Dict[str, Any]:
        """Register a new request in 'queued' state. One row per command."""
        rows = []
        runnable = [e for e in plan if e["commands"]]
        for entry in runnable:
            for cmd in entry["commands"]:
                rows.append({
                    "capture_type": entry["capture_type"],
                    "command": cmd,
                    "status": "pending",
                    "bytes": None,
                    "duration": None,
                    "parsed": None,
                    "template": None,
                    "error": None,
                })
        req = {
            "id": uuid.uuid4().hex[:12],
            "device_id": device.get("id"),
            "device": device.get("name"),
            "trigger": trigger,
            "status": "queued",
            "phase": "queued",
            "capture_types": [e["capture_type"] for e in runnable],
            # Requested capture types that no job defines — reported, not run.
            "unresolved": [e["capture_type"] for e in plan if not e["commands"]],
            "captures": rows,
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
            "duration": None,
            "error_category": None,
            "error_message": None,
            "stored": None,
            "_t0": None,
        }
        with self._lock:
            self._prune_locked()
            self._requests[req["id"]] = req
            return self._public(req)

    # ── updates (worker thread) ───────────────────────────────────────

    def start(self, request_id: str) -> None:
        with self._lock:
            req = self._requests.get(request_id)
            if req:
                req["status"] = "running"
                req["phase"] = "starting"
                req["started_at"] = _now_iso()
                req["_t0"] = time.time()

    def phase(self, request_id: str, text: str) -> None:
        with self._lock:
            req = self._requests.get(request_id)
            if req and req["status"] not in FINAL_STATES:
                req["phase"] = text

    def capture(self, request_id: str, index: int, **fields: Any) -> None:
        with self._lock:
            req = self._requests.get(request_id)
            if req and 0 <= index < len(req["captures"]):
                req["captures"][index].update(fields)

    def running_command(self, request_id: str, index: int) -> None:
        """Mark command `index` running and any earlier still-'running' row
        as no longer current (its outcome is set at the end of the session)."""
        with self._lock:
            req = self._requests.get(request_id)
            if not req:
                return
            for i, row in enumerate(req["captures"]):
                if i == index:
                    row["status"] = "running"
                elif row["status"] == "running":
                    row["status"] = "pending"

    def finish(
        self,
        request_id: str,
        status: str,
        error_category: Optional[str] = None,
        error_message: Optional[str] = None,
        stored: Optional[bool] = None,
    ) -> None:
        with self._lock:
            req = self._requests.get(request_id)
            if not req:
                return
            req["status"] = status
            req["phase"] = status
            req["finished_at"] = _now_iso()
            if req["_t0"] is not None:
                req["duration"] = round(time.time() - req["_t0"], 1)
            req["error_category"] = error_category
            req["error_message"] = error_message
            req["stored"] = stored
            # A row still pending/running at the end never produced output.
            for row in req["captures"]:
                if row["status"] in ("pending", "running"):
                    row["status"] = "failed"

    # ── reads (API thread) ────────────────────────────────────────────

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            req = self._requests.get(request_id)
            return self._public(req) if req else None

    def active_for_device(self, device_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            for req in reversed(self._requests.values()):
                if req["device_id"] == device_id and req["status"] not in FINAL_STATES:
                    return self._public(req)
        return None

    def latest_for_device(self, device_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._prune_locked()
            for req in reversed(self._requests.values()):
                if req["device_id"] == device_id:
                    return self._public(req)
        return None

    def counts(self) -> Dict[str, int]:
        with self._lock:
            queued = sum(1 for r in self._requests.values() if r["status"] == "queued")
            running = sum(1 for r in self._requests.values() if r["status"] == "running")
        return {"queued": queued, "running": running}

    # ── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _public(req: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(req)
        t0 = out.pop("_t0", None)
        # Server-side elapsed so the UI timer isn't subject to client clock skew.
        if out["duration"] is not None:
            out["elapsed"] = out["duration"]
        else:
            out["elapsed"] = round(time.time() - t0, 1) if t0 is not None else 0.0
        return out

    def _prune_locked(self) -> None:
        cutoff = time.time() - self._retain
        stale = []
        for rid, req in self._requests.items():
            if req["status"] in FINAL_STATES and req["finished_at"]:
                finished = datetime.strptime(
                    req["finished_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=timezone.utc).timestamp()
                if finished < cutoff:
                    stale.append(rid)
        for rid in stale:
            self._requests.pop(rid, None)
        # Hard cap — drop oldest finished first, then oldest overall.
        while len(self._requests) > self._max:
            victim = next(
                (rid for rid, r in self._requests.items() if r["status"] in FINAL_STATES),
                next(iter(self._requests)),
            )
            self._requests.pop(victim, None)
