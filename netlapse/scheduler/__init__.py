"""
Netlapse Scheduler — Periodic collection with real-time progress.

Two components:
  - ConnectionManager: WebSocket broadcast hub
  - Scheduler: asyncio-based job loop with ThreadPoolExecutor for SSH

The scheduler polls `get_due_jobs()` on an interval, queues them,
and executes each in a thread (SSH is blocking). API routes push
ad-hoc triggers onto the same queue.

WebSocket events emitted during collection:
  collection_start    { job, device_count, history_id }
  device_collected    { device, status, command, bytes, duration }
  collection_progress { collected, total }
  collection_complete { job, collected, failed, device_count, duration, history_id }
  adhoc_complete      { request }   — a Collect Now request finished

Collect Now (single-device, ad-hoc) runs on its own small thread pool so it
is never stuck behind a long scheduled job, and every request is tracked in
an AdhocTracker (see scheduler/adhoc.py) that the device view polls.

Usage (wired in app.py lifespan):
    from netlapse.scheduler import Scheduler, ConnectionManager

    ws_manager = ConnectionManager()
    scheduler = Scheduler(db, vault, storage, ws_manager, loop)
    await scheduler.start()
    # ...
    await scheduler.stop()
"""

import asyncio
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import WebSocket


import multiprocessing


from netlapse.core.collector import _record_result
from netlapse.ssh.executor import collect_device, DeviceResult
from netlapse.vault.bridge import resolve_shared_credentials, resolve_credential_chain, resolve_collection_chain
from netlapse.registry.resolver import resolve_commands
from netlapse.scheduler.adhoc import AdhocTracker, resolve_adhoc_plan
from netlapse.parser.engine import is_unparsed_capture

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Subprocess parse — each ParseText() gets its own process + GIL
# ═══════════════════════════════════════════════════════════════════════════

def _parse_worker(result_queue, db_path, raw_text, platform_profile,
                   command, min_score):
    """
    Worker function for subprocess parsing.

    Runs in a forked process with its own GIL, so regex catastrophic
    backtracking can't freeze the parent. Must be a top-level function
    (pickle requirement for multiprocessing).
    """
    import sys
    try:
        from netlapse.parser.engine import ParseEngine

        engine = ParseEngine(db_path=db_path, min_score=min_score)

        # Attempt 1: specific filter (platform + command)
        result = engine.parse(raw_text, platform_profile, command)

        # Attempt 2: vendor-only fallback
        if not result.success:
            result = engine.parse_vendor_fallback(raw_text, platform_profile)

        result_queue.put({
            "success": result.success,
            "template": result.template,
            "records": result.records if result.success else None,
            "score": result.score,
        })
    except Exception as e:
        # Print to stderr so it's visible in parent's console
        print(f"[parse_worker] ERROR: {e}", file=sys.stderr, flush=True)
        result_queue.put({"success": False, "error": str(e)})


def subprocess_parse(db_path, raw_text, platform_profile, command,
                     min_score=15.0, timeout=90):
    """
    Parse CLI output in a subprocess with a hard kill timeout.

    Forks a new process, runs tfsm-fire template matching, and returns
    the result. If the process exceeds the timeout (regex backtracking),
    it's killed with SIGKILL — the only signal that stops _sre.c.

    Args:
        db_path:          Path to tfsm_templates.db.
        raw_text:         Raw CLI output to parse.
        platform_profile: Platform hint (e.g. 'cisco_ios').
        command:          CLI command that produced the output.
        min_score:        Minimum score to accept a parse.
        timeout:          Hard timeout in seconds per snapshot.

    Returns:
        dict with keys: success, template, records, score, error, timeout.
    """
    q = multiprocessing.Queue()
    p = multiprocessing.Process(
        target=_parse_worker,
        args=(q, str(db_path), raw_text, platform_profile,
              command, min_score),
    )
    p.start()
    p.join(timeout=timeout)

    if p.is_alive():
        p.kill()
        p.join(timeout=5)
        logger.warning("subprocess_parse: killed after %ds", timeout)
        return {"success": False, "timeout": True,
                "error": f"killed after {timeout}s"}

    exit_code = p.exitcode
    try:
        result = q.get_nowait()
        return result
    except Exception:
        logger.warning(
            "subprocess_parse: no result (exit_code=%s)", exit_code,
        )
        return {"success": False,
                "error": f"no result (exit_code={exit_code})"}


# ═══════════════════════════════════════════════════════════════════════════
# WebSocket broadcast hub
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    """
    Manages WebSocket connections and broadcasts events.

    app.py adds/removes connections via connect()/disconnect().
    The scheduler broadcasts events via broadcast().
    """

    def __init__(self):
        self._connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Accept and track a new WebSocket connection."""
        await websocket.accept()
        self._connections.append(websocket)
        logger.debug("WebSocket connected (%d total)", len(self._connections))

    def disconnect(self, websocket: WebSocket):
        """Remove a disconnected WebSocket."""
        try:
            self._connections.remove(websocket)
        except ValueError:
            pass
        logger.debug(
            "WebSocket disconnected (%d remaining)", len(self._connections)
        )

    async def broadcast(self, data: dict):
        """Send a JSON event to all connected clients."""
        if not self._connections:
            return

        dead: List[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)

        for ws in dead:
            try:
                self._connections.remove(ws)
            except ValueError:
                pass

    @property
    def client_count(self) -> int:
        return len(self._connections)


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler
# ═══════════════════════════════════════════════════════════════════════════

class Scheduler:
    """
    Async job scheduler backed by a thread pool.

    Poll loop:  HTTP GET /api/v1/jobs/due → queue
    Queue:      (trigger, job_slug) items — from poll or API trigger
    Executor:   ThreadPoolExecutor runs blocking SSH collection
    Broadcast:  thread-safe push to WebSocket ConnectionManager

    The poll loop checks for due jobs via the REST API (not the DB
    directly) so the event loop never blocks on database I/O. Job
    execution still accesses the DB from worker threads via the
    thread-safe proxy.
    """

    def __init__(
        self,
        db,
        vault,
        storage,
        ws_manager: ConnectionManager,
        loop: asyncio.AbstractEventLoop,
        config: Optional[Dict[str, Any]] = None,
        parser=None,
        collection_config=None,
        api_port: int = 8888,
        credential_fallback: Optional[List[str]] = None,
        proxy_config: Optional[Dict[str, Any]] = None,
        internal_token: Optional[str] = None,
    ):
        self.db = db                    # shared thread-safe instance
        self.vault = vault
        self.storage = storage
        self.ws = ws_manager
        self.loop = loop
        self.parser = parser            # ParseEngine (optional, thread-safe)
        self._credential_fallback = credential_fallback or []

        cfg = config or {}
        # Use `or default` rather than dict.get's default: a key present but
        # null/empty (e.g. `max_workers:` with no value in YAML) parses to None,
        # which dict.get would pass straight through. None workers makes
        # ThreadPoolExecutor pick a large default pool and breaks the %d log;
        # 0 is invalid. Coalesce to the documented defaults.
        self._poll_interval = cfg.get("poll_interval") or 15
        self._max_workers = cfg.get("max_workers") or 2
        self._adhoc_workers = cfg.get("adhoc_workers") or 2
        self._api_base = f"http://127.0.0.1:{api_port}"
        self._parse_timeout = cfg.get("parse_timeout") or 10
        # Resolved `collection:` section from config.yaml, handed to the
        # executor per collection. Empty dict = documented defaults.
        self._timeouts = collection_config or {}
        # Token for the loopback self-call to the (now auth-gated) REST API.
        self._internal_token = internal_token

        self.job_queue: asyncio.Queue = asyncio.Queue()
        self.executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="netlapse-collector",
        )
        # Collect Now gets its own pool: with only the shared pool, a click
        # sat invisibly behind whatever multi-minute scheduled jobs held the
        # workers.
        self.adhoc_executor = ThreadPoolExecutor(
            max_workers=self._adhoc_workers,
            thread_name_prefix="netlapse-adhoc",
        )
        self.adhoc = AdhocTracker()
        self._adhoc_lock = threading.Lock()   # serializes dedupe check + create
        self._running = False
        self._active_jobs: Dict[str, int] = {}   # slug → history_id
        self._tasks: List[asyncio.Task] = []
        self._start_time: Optional[float] = None

        # Jump-host (ProxyJump) resolver from config.yaml jump_hosts/proxy_rules.
        # None when no jump-host config is present. A malformed config raises
        # here, at startup, rather than silently misrouting collection.
        from netlapse.ssh.proxy import build_proxy_resolver
        self._proxy = build_proxy_resolver(proxy_config, self.vault)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Start the poll loop and queue consumer."""
        self._running = True
        self._start_time = time.time()

        # Reconcile orphaned 'running' rows from a previous process that
        # exited mid-collection — otherwise they mask the last good run.
        try:
            self.db.reap_running_jobs()
        except Exception:
            logger.exception("Failed to reap orphaned job runs at startup")

        self._tasks = [
            asyncio.create_task(self._poll_loop()),
            asyncio.create_task(self._queue_consumer()),
        ]
        logger.info(
            "Scheduler started (poll=%ds, workers=%d)",
            self._poll_interval, self._max_workers,
        )

    async def stop(self):
        """Stop the scheduler and drain the thread pool."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        self.executor.shutdown(wait=False)
        self.adhoc_executor.shutdown(wait=False)
        self._tasks.clear()
        logger.info("Scheduler stopped")

    # ------------------------------------------------------------------
    # Public interface (called by API routes)
    # ------------------------------------------------------------------

    @property
    def status(self) -> Dict[str, Any]:
        """Current scheduler state for /api/v1/status/scheduler."""
        uptime = (
            round(time.time() - self._start_time, 1)
            if self._start_time else 0
        )
        return {
            "running": self._running,
            "uptime_seconds": uptime,
            "active_jobs": len(self._active_jobs),
            "active_job_slugs": list(self._active_jobs.keys()),
            "queued_collections": self.job_queue.qsize(),
            "poll_interval": self._poll_interval,
            "max_workers": self._max_workers,
            "adhoc_workers": self._adhoc_workers,
            "adhoc": self.adhoc.counts(),
            "ws_clients": self.ws.client_count,
        }

    def enqueue_job(self, job_slug: str, trigger: str = "api"):
        """
        Queue a job for execution. Safe from async context.

        Args:
            job_slug: Job slug from the jobs table.
            trigger: What triggered it — api, manual, syslog.
        """
        self.job_queue.put_nowait((trigger, job_slug))
        logger.info("Queued job '%s' (trigger=%s)", job_slug, trigger)

    def enqueue_device(
        self, device_id: int, capture_type: Optional[str] = "config",
        trigger: str = "api",
    ) -> Dict[str, Any]:
        """
        Queue a single-device ad-hoc collection of one capture type (or all
        applicable captures when capture_type is None). Kept for the Oxidized
        /node/next trigger; see request_collection() for the full contract.
        """
        req, _ = self.request_collection(
            device_id, [capture_type] if capture_type else None, trigger=trigger,
        )
        return req

    def request_collection(
        self,
        device_id: int,
        capture_types: Optional[List[str]] = None,
        trigger: str = "ui",
    ):
        """
        Start a tracked Collect Now request for one device.

        Safe to call from any thread. If the device already has a request in
        flight, that request is returned instead of starting a second SSH
        session to the same box.

        Args:
            device_id: DCIM device ID.
            capture_types: Capture types to collect; None/empty = every capture
                whose enabled job applies to this device.
            trigger: Free-form origin label (ui, api, oxidized).

        Returns:
            (request_dict, created) — created is False when an in-flight
            request was returned instead.

        Raises:
            LookupError: device not found.
            ValueError: nothing to collect (no job defines the capture types).
        """
        with self._adhoc_lock:
            active = self.adhoc.active_for_device(device_id)
            if active:
                logger.info(
                    "Collect Now for device %d: request %s already in flight",
                    device_id, active["id"],
                )
                return active, False

            device = self.db.get_device_by_id(device_id)
            if not device:
                raise LookupError(f"device {device_id} not found")

            plan = resolve_adhoc_plan(self.db.list_jobs(), device, capture_types)
            if not any(e["commands"] for e in plan):
                wanted = ", ".join(capture_types) if capture_types else "any capture"
                raise ValueError(
                    f"no job defines commands for {wanted} on "
                    f"platform '{device.get('platform_slug') or '?'}'"
                )

            req = self.adhoc.create(device, plan, trigger=trigger)

        self.adhoc_executor.submit(self._execute_adhoc, req["id"], device_id, plan)
        logger.info(
            "Collect Now queued for %s: %s (request %s)",
            device["name"], ", ".join(req["capture_types"]), req["id"],
        )
        return req, True

    # ------------------------------------------------------------------
    # Internal: poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self):
        """Check for due jobs via the REST API every poll_interval seconds.

        Uses HTTP GET /api/v1/jobs/due so the event loop never touches
        the database — the request is handled by FastAPI in its own
        threadpool worker.
        """
        import urllib.request

        url = f"{self._api_base}/api/v1/jobs/due"
        headers = {}
        if self._internal_token:
            headers["X-API-Key"] = self._internal_token

        # Let uvicorn finish binding before the first poll
        await asyncio.sleep(2)

        while self._running:
            try:
                # HTTP call runs in default executor → off the event loop
                req = urllib.request.Request(url, headers=headers)
                raw = await self.loop.run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(req, timeout=5).read(),
                )
                due = json.loads(raw)

                for job in due:
                    slug = job["slug"]
                    if slug in self._active_jobs:
                        continue
                    await self.job_queue.put(("scheduled", slug))
                    logger.debug("Queued due job: %s", slug)

            except Exception:
                logger.exception("Poll loop error")

            await asyncio.sleep(self._poll_interval)

    # ------------------------------------------------------------------
    # Internal: queue consumer
    # ------------------------------------------------------------------

    async def _queue_consumer(self):
        """Process queued job requests.

        Jobs are submitted to the ThreadPoolExecutor without awaiting
        completion — the consumer stays responsive so job triggers aren't
        blocked behind a multi-minute scheduled job. The executor's
        max_workers naturally limits concurrency. (Single-device Collect Now
        does not use this queue; see request_collection().)

        Race prevention: _active_jobs[slug] is set HERE (event loop
        thread) before submit, and cleared in the worker thread on
        completion. The check + set both happen on the event loop,
        so they're serialized by the loop itself.
        """
        while self._running:
            try:
                trigger, slug = await self.job_queue.get()

                # Skip if already running
                if slug in self._active_jobs:
                    logger.info(
                        "Job '%s' already active, skipping", slug
                    )
                    continue

                # Mark active BEFORE submit to prevent double-execution
                self._active_jobs[slug] = 0

                # Fire-and-forget: submit to thread pool, don't await.
                # _execute_job handles its own errors and cleanup.
                self.loop.run_in_executor(
                    self.executor,
                    self._execute_job,
                    trigger,
                    slug,
                )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Queue consumer error")

    # ------------------------------------------------------------------
    # Internal: job execution (runs in ThreadPoolExecutor)
    # ------------------------------------------------------------------

    def _execute_job(self, trigger: str, job_slug: str):
        """
        Execute a full job. Runs in a worker thread.

        The shared db instance is thread-safe via _ThreadSafeConnection
        proxy — no need for per-thread connections.
        """
        try:
            job = self.db.get_job(job_slug)
            if not job:
                logger.warning("Job '%s' not found, skipping", job_slug)
                return
            if not job["enabled"]:
                logger.info("Job '%s' disabled, skipping", job_slug)
                return

            self._active_jobs[job_slug] = 0
            self._run_job(self.db, job, trigger)
        except Exception:
            logger.exception("Job '%s' failed unexpectedly", job_slug)
            # If start_job_run already created a history row, _run_job stored
            # its id in _active_jobs. Finalize it as failed so it doesn't
            # linger as an orphaned 'running' row in the dashboard.
            hid = self._active_jobs.get(job_slug)
            if hid:
                try:
                    self.db.complete_job_run(
                        history_id=hid,
                        status="failed",
                        error_message="job crashed mid-run",
                    )
                except Exception:
                    logger.exception(
                        "Could not finalize history for crashed job '%s'",
                        job_slug,
                    )
        finally:
            self._active_jobs.pop(job_slug, None)

    def _run_job(self, db, job: dict, trigger: str):
        """Core job execution logic. Uses thread-local db connection."""
        job_slug = job["slug"]
        default_commands = json.loads(job["commands"])
        command_map = (
            json.loads(job["command_map"])
            if job.get("command_map")
            else {}
        )
        capture_type = job["capture_type"]

        # ── Resolve targets ──────────────────────────────────────────
        targets = db.list_collection_targets(
            platform_filter=job.get("platform_filter"),
            site_filter=job.get("site_filter"),
            role_filter=job.get("role_filter"),
            device_filter=job.get("device_filter"),
            status_filter=job.get("status_filter", "active"),
        )

        if not targets:
            logger.info("Job '%s' matched no devices", job_slug)
            return

        # ── Resolve credentials (ordered chain: default + config fallbacks) ──
        try:
            creds = resolve_credential_chain(
                self.vault, fallback_names=self._credential_fallback
            )
        except Exception as e:
            logger.error(
                "Job '%s': credential resolution failed: %s",
                job_slug, e,
            )
            return

        # ── Start history ────────────────────────────────────────────
        history_id = db.start_job_run(
            job_slug, trigger=trigger, total_devices=len(targets),
        )
        self._active_jobs[job_slug] = history_id

        self._broadcast("collection_start", {
            "job": job_slug,
            "device_count": len(targets),
            "history_id": history_id,
        })

        if command_map:
            platforms_with_overrides = ", ".join(sorted(command_map.keys()))
            logger.info(
                "Job '%s': collecting from %d devices "
                "(trigger=%s, history=%d, platform overrides: %s)",
                job_slug, len(targets), trigger, history_id,
                platforms_with_overrides,
            )
        else:
            logger.info(
                "Job '%s': collecting from %d devices "
                "(trigger=%s, history=%d)",
                job_slug, len(targets), trigger, history_id,
            )

        # ── Collect device by device ─────────────────────────────────
        start_time = time.time()
        success_count = 0
        failed_count = 0

        for i, device in enumerate(targets, 1):
            # ── Per-platform command resolution (shared resolver) ──
            platform_slug = device.get("platform_slug", "")
            commands = resolve_commands(
                default_commands, command_map, platform_slug
            )

            # Per-device credential chain: the device's assigned credential is
            # tried first, then the global fallbacks. Falls back to the default
            # chain when the device has none. (The pre-loop resolution above is
            # the vault-validity / early-abort guard.)
            creds = resolve_collection_chain(
                self.vault, device, self._credential_fallback
            )

            result = collect_device(
                timeouts=self._timeouts,
                device=device,
                commands=commands,
                capture_type=capture_type,
                credentials=creds,
                jump=self._proxy.resolve(device) if self._proxy else None,
            )

            # ── Parse snapshots (subprocess — safe from regex hangs) ──
            if self.parser and result.success and result.snapshots:
                platform_profile = device.get("platform_profile", "")
                for snap in result.snapshots:
                    if is_unparsed_capture(snap.capture_type):
                        # {} (not None): storage writes a records-free
                        # envelope, keeping the command metadata and
                        # replacing any parse stored before config was
                        # excluded.
                        snap.parsed_data = {}
                        continue
                    logger.info(
                        "  %s: parsing %s (%d bytes, platform=%s, cmd=%s)...",
                        result.device_name, snap.capture_type,
                        len(snap.raw_text), platform_profile,
                        snap.command,
                    )
                    parse_result = subprocess_parse(
                        db_path=self.parser.db_path,
                        raw_text=snap.raw_text,
                        platform_profile=platform_profile,
                        command=snap.command,
                        min_score=self.parser.min_score,
                        timeout=self._parse_timeout,
                    )
                    if parse_result.get("success"):
                        snap.parsed_data = {
                            "records": parse_result["records"],
                        }
                        snap.template_name = parse_result["template"]
                        snap.template_score = parse_result["score"]
                        logger.info(
                            "  %s: parsed %s → %s (%.0f)",
                            result.device_name, snap.capture_type,
                            snap.template_name, snap.template_score,
                        )
                    elif parse_result.get("timeout"):
                        logger.warning(
                            "  %s: PARSE KILLED after timeout — "
                            "regex backtracking on %s (%d bytes)",
                            result.device_name, snap.capture_type,
                            len(snap.raw_text),
                        )
                    else:
                        logger.info(
                            "  %s: parse result for %s: %s",
                            result.device_name, snap.capture_type,
                            parse_result,
                        )

            # Store snapshots + update device status
            _record_result(db, self.storage, result, job_slug)

            # Record per-device result in job history
            dev_status = "success" if result.success else (
                result.error_category or "failed"
            )
            db.insert_device_result(
                history_id=history_id,
                device_id=result.device_id,
                device_name=result.device_name,
                status=dev_status,
                error_category=result.error_category,
                error_message=result.error_message,
                duration_seconds=result.duration_seconds,
            )

            if result.success:
                success_count += 1
            else:
                failed_count += 1

            # ── Broadcast per-device progress ────────────────────────
            total_bytes = sum(
                len(s.raw_text) for s in result.snapshots
            ) if result.snapshots else 0

            # Include parse status if any snapshot was parsed
            parsed_templates = [
                s.template_name for s in (result.snapshots or [])
                if s.parsed_data
            ]

            self._broadcast("device_collected", {
                "device": result.device_name,
                "status": "success" if result.success else "failed",
                "command": commands[0] if commands else "",
                "commands": commands,
                "platform": platform_slug,
                "bytes": total_bytes,
                "duration": round(result.duration_seconds, 1),
                "parsed": bool(parsed_templates),
                "template": parsed_templates[0] if parsed_templates else None,
            })

            self._broadcast("collection_progress", {
                "collected": i,
                "total": len(targets),
            })

        # ── Complete history ─────────────────────────────────────────
        duration = round(time.time() - start_time, 1)

        db.complete_job_run(
            history_id=history_id,
            status="completed",
            success_count=success_count,
            failed_count=failed_count,
        )

        # ── Advance schedule ─────────────────────────────────────────
        now_iso = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        interval = job.get("interval_seconds", 3600)
        next_run = (
            datetime.now(timezone.utc) + timedelta(seconds=interval)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.update_job_schedule(job_slug, now_iso, next_run)

        # ── Broadcast completion ─────────────────────────────────────
        self._broadcast("collection_complete", {
            "job": job_slug,
            "collected": success_count,
            "failed": failed_count,
            "device_count": len(targets),
            "duration": duration,
            "history_id": history_id,
        })

        logger.info(
            "Job '%s' complete: %d/%d in %.1fs (history=%d)",
            job_slug, success_count, len(targets), duration, history_id,
        )

    # ------------------------------------------------------------------
    # Internal: Collect Now execution (runs in adhoc_executor)
    # ------------------------------------------------------------------

    _RUNNING_RE = re.compile(r"running command (\d+)/(\d+)")

    def _execute_adhoc(
        self, request_id: str, device_id: int, plan: List[Dict[str, Any]],
    ):
        """
        Run one tracked Collect Now request. Worker thread.

        Every capture in the plan runs in ONE SSH session (one login, one
        prompt detection) via collect_device(command_capture_types=...), so
        "all captures" costs one authentication against the box, not one per
        capture type.
        """
        tracker = self.adhoc
        tracker.start(request_id)
        try:
            device = self.db.get_device_by_id(device_id)
            if not device:
                tracker.finish(request_id, "failed", "unknown",
                               "device no longer exists")
                return

            commands: List[str] = []
            ctypes: List[str] = []
            for entry in plan:
                for cmd in entry["commands"]:
                    commands.append(cmd)
                    ctypes.append(entry["capture_type"])
            total = len(commands)

            try:
                creds = resolve_collection_chain(
                    self.vault, device, self._credential_fallback
                )
            except Exception as e:
                logger.error(
                    "Collect Now %s (%s): credential resolution failed: %s",
                    request_id, device["name"], e,
                )
                tracker.finish(request_id, "failed", "credentials", str(e))
                return

            def on_progress(_device_name: str, msg: str):
                m = self._RUNNING_RE.match(msg)
                if m:
                    idx = int(m.group(1)) - 1
                    tracker.running_command(request_id, idx)
                    if 0 <= idx < total:
                        tracker.phase(
                            request_id,
                            f"running {commands[idx]} ({idx + 1}/{total})",
                        )
                else:
                    tracker.phase(request_id, msg)

            result = collect_device(
                timeouts=self._timeouts,
                device=device,
                commands=commands,
                capture_type=ctypes[0],
                command_capture_types=ctypes,
                credentials=creds,
                progress_callback=on_progress,
                jump=self._proxy.resolve(device) if self._proxy else None,
            )

            # ── Per-command outcome. Snapshots exist only for successful
            #    commands, in command order. ────────────────────────────
            snaps = iter(result.snapshots or [])
            snap_at: Dict[int, Any] = {}
            failed_cmds = 0
            for i, cr in enumerate(result.command_results or []):
                if cr.success:
                    snap_at[i] = next(snaps, None)
                    tracker.capture(
                        request_id, i, status="ok",
                        bytes=len(cr.raw_output or ""),
                        duration=round(cr.duration_seconds, 1),
                    )
                else:
                    failed_cmds += 1
                    tracker.capture(
                        request_id, i, status="failed", error=cr.error,
                        duration=round(cr.duration_seconds, 1),
                    )

            # ── Parse (subprocess — safe from regex hangs) ─────────────
            if self.parser and result.success:
                platform_profile = device.get("platform_profile", "")
                for i, snap in snap_at.items():
                    if snap is None:
                        continue
                    if is_unparsed_capture(snap.capture_type):
                        snap.parsed_data = {}   # records-free envelope; see _run_job
                        continue
                    tracker.phase(request_id, f"parsing {snap.capture_type}")
                    pr = subprocess_parse(
                        db_path=self.parser.db_path,
                        raw_text=snap.raw_text,
                        platform_profile=platform_profile,
                        command=snap.command,
                        min_score=self.parser.min_score,
                        timeout=self._parse_timeout,
                    )
                    if pr.get("success"):
                        snap.parsed_data = {"records": pr["records"]}
                        snap.template_name = pr["template"]
                        snap.template_score = pr["score"]
                        tracker.capture(request_id, i, parsed=True,
                                        template=pr["template"])
                    else:
                        tracker.capture(request_id, i, parsed=False)

            tracker.phase(request_id, "storing")
            stored = _record_result(self.db, self.storage, result)

            # ── Outcome ────────────────────────────────────────────────
            if not result.success:
                first_err = next(
                    (cr.error for cr in (result.command_results or [])
                     if not cr.success and cr.error),
                    None,
                )
                tracker.finish(
                    request_id, "failed",
                    result.error_category or "command_error",
                    result.error_message or first_err or "collection failed",
                    stored=stored,
                )
            elif stored is False:
                tracker.finish(
                    request_id, "failed", "storage",
                    "collected, but writing snapshots failed — see server log",
                    stored=False,
                )
            elif failed_cmds:
                tracker.finish(
                    request_id, "partial", "command_error",
                    f"{failed_cmds} of {total} command(s) failed",
                    stored=stored,
                )
            else:
                tracker.finish(request_id, "success", stored=stored)

            final = tracker.get(request_id) or {}
            logger.info(
                "Collect Now %s for %s: %s (%d/%d commands, %.1fs)",
                request_id, device["name"], final.get("status"),
                total - failed_cmds if result.success else 0, total,
                result.duration_seconds,
            )
        except Exception as e:
            logger.exception("Collect Now %s crashed", request_id)
            tracker.finish(request_id, "failed", "unknown", str(e))
        finally:
            done = tracker.get(request_id)
            if done:
                self._broadcast("adhoc_complete", {"request": done})

    # ------------------------------------------------------------------
    # Internal: thread-safe broadcast
    # ------------------------------------------------------------------

    def _broadcast(self, event_type: str, data: dict):
        """
        Broadcast a WebSocket event from a worker thread.

        Uses loop.call_soon_threadsafe to schedule the async
        broadcast on the main event loop.
        """
        event = {"type": event_type, **data}
        try:
            self.loop.call_soon_threadsafe(
                asyncio.ensure_future,
                self.ws.broadcast(event),
            )
        except RuntimeError:
            # Event loop closed during shutdown
            pass