"""
Netlapse — Network State Time-Lapse

A headless network state backup daemon with structured CLI output parsing.
Oxidized-compatible API for drop-in replacement, plus a native API for
structured diffs, multi-command jobs, and parsed operational state.

Architecture:
  /                        → Web UI (FastAPI + WebSocket, vanilla JS)
  /nodes, /node/*, /reload → Oxidized-compatible REST API
  /api/v1/*                → Native Netlapse REST API
  /ws                      → WebSocket for live collection progress
"""

import asyncio
import logging
import os
import shutil
import sys
import time
import yaml
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from netlapse import __version__
from netlapse.api.oxidized_compat import router as oxidized_router
from netlapse.api.native import router as native_router
from netlapse.dcim import NetlapseDB
from netlapse.storage import create_backend
from netlapse.scheduler import ConnectionManager, Scheduler
from netlapse.web import router as web_router, STATIC_DIR
from netlapse.api.admin import router as admin_router
from netlapse.api.backup import router as backup_router
from netlapse.api.setup import router as setup_router
from netlapse.api.captures import router as captures_router
from netlapse.api.dcim_admin import router as dcim_admin_router
from netlapse.api.parse_audit import router as parse_audit_router
from netlapse.api.changes import router as changes_router
from netlapse.api.topology import router as topology_router
from netlapse.auth import (
    AuthManager,
    auth_router,
    users_router,
    install_auth_gate,
    resolve_ws_user,
)

logger = logging.getLogger(__name__)

_start_time = time.time()


def get_uptime() -> float:
    return time.time() - _start_time


def _packaged_definitions_dir() -> Path:
    """The default definition catalog shipped inside the installed package."""
    return Path(__file__).parent / "definitions"


TEMPLATE_DB_URL = (
    "https://github.com/scottpeterman/netlapse/raw/refs/heads/main/"
    "tfsm_templates.db"
)
_SQLITE_MAGIC = b"SQLite format 3\x00"


def fetch_template_db(
    dest, url: str = TEMPLATE_DB_URL, timeout: int = 30, force: bool = False
) -> bool:
    """
    Download the TextFSM template database (~53 MB) to ``dest``.

    The DB is too large to ship in the wheel and versions independently of the
    code, so it's staged on demand. Returns True if a usable DB is present
    afterward. **Non-fatal by contract**: a False return only means structured
    parsing stays disabled — raw captures are still collected and stored.

    Safe by construction: streams to a ``.part`` temp file, validates the
    SQLite header and that the file opens, then atomically renames into place.
    Never clobbers an existing DB unless ``force=True``. Honors
    ``NETLAPSE_SKIP_TEMPLATE_DOWNLOAD`` for air-gapped/offline installs.
    """
    import sqlite3
    import urllib.request

    dest = Path(dest).expanduser()
    if dest.exists() and not force:
        return True
    if os.environ.get("NETLAPSE_SKIP_TEMPLATE_DOWNLOAD"):
        logger.info("Skipping template DB download "
                    "(NETLAPSE_SKIP_TEMPLATE_DOWNLOAD set).")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(dest) + ".part")
    try:
        logger.info("Downloading TextFSM template DB (~53 MB) to enable "
                    "structured parsing — source: %s", url)
        req = urllib.request.Request(url, headers={"User-Agent": "netlapse"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, length=1 << 20)
        # Validate: SQLite magic header + actually opens as a database.
        with open(tmp, "rb") as f:
            if f.read(16) != _SQLITE_MAGIC:
                raise ValueError("downloaded file is not a SQLite database")
        con = sqlite3.connect(tmp)
        try:
            con.execute("PRAGMA schema_version")
        finally:
            con.close()
        os.replace(tmp, dest)  # atomic
        logger.info("Template DB ready: %s (%.1f MB)",
                    dest, dest.stat().st_size / 1e6)
        return True
    except Exception as e:
        logger.warning(
            "Template DB download failed (%s). Structured parsing stays "
            "disabled — raw captures are still collected. Retry with "
            "`netlapse fetch-templates`, or copy the file to %s.", e, dest)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def scaffold_user_dir(config_path: Path) -> None:
    """
    First-run scaffold so a fresh `pip install netlapse` boots into a working
    state. Creates the config directory, writes an annotated ``config.yaml``
    from the template, and seeds the default definition catalog (captures,
    jobs, roles, volatility) alongside it — which is where ``resolve_definitions_dir``
    looks by default.

    Idempotent and non-clobbering: only files that don't already exist are
    written, so it never overwrites an operator's edits.
    """
    from netlapse.config_template import default_config_yaml

    cfg_dir = config_path.parent
    cfg_dir.mkdir(parents=True, exist_ok=True)

    if not config_path.exists():
        config_path.write_text(default_config_yaml(), encoding="utf-8")
        logger.info("Wrote starter config: %s", config_path)

    src = _packaged_definitions_dir()
    copied = []
    if src.is_dir():
        for f in sorted(src.glob("*.yaml")):
            dst = cfg_dir / f.name
            if not dst.exists():
                shutil.copyfile(f, dst)
                copied.append(f.name)
    if copied:
        logger.info("Seeded default definitions into %s: %s",
                    cfg_dir, ", ".join(copied))

    # Stage the TextFSM template DB (too large for the wheel). Best-effort and
    # first-run only — because the scaffold runs just once, a firewalled box
    # gets a single warning rather than a retry on every boot. Use
    # `netlapse fetch-templates` to retry later, or NETLAPSE_SKIP_TEMPLATE_DOWNLOAD
    # to opt out entirely. Resolve the same parser.db_path the app will read so
    # the download lands exactly where the parser looks for it.
    db_path = cfg_dir / "tfsm_templates.db"
    try:
        written = yaml.safe_load(config_path.read_text()) or {}
        configured = (written.get("parser") or {}).get("db_path")
        if configured:
            db_path = Path(configured).expanduser()
    except Exception:
        pass
    fetch_template_db(db_path)


def load_config() -> dict:
    """
    Load config from ~/.netlapse/config.yaml or defaults.
    Config path can be overridden with NETLAPSE_CONFIG env var.

    On first run (no config file yet) the user directory is scaffolded with an
    annotated config and the default definition catalog, so a fresh install
    starts with a working job set rather than an empty one.
    """
    config_path = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()

    if not config_path.exists():
        try:
            scaffold_user_dir(config_path)
        except Exception as e:
            # A read-only home or odd permissions shouldn't be fatal — fall
            # through to built-in defaults below.
            logger.warning("First-run scaffold failed (%s); using built-in "
                           "defaults", e)

    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        logger.info("Loaded config from %s", config_path)
        return config

    # Defaults (scaffold couldn't write, e.g. read-only home)
    return {
        "listen": {"host": "0.0.0.0", "port": 8888},
        "storage": {"backend": "file", "path": "~/.netlapse/data"},
        "dcim_db": "~/.netlapse/netlapse.db",
    }


async def start_scheduler(app: FastAPI) -> Scheduler:
    """
    Build and start the scheduler from app.state. Called at startup when the
    vault unlocks, and by the setup wizard right after it initializes the
    vault — so a first run starts collecting without a restart.
    Idempotent: returns the running scheduler if there already is one.
    """
    existing = getattr(app.state, "scheduler", None)
    if existing is not None:
        return existing

    config = app.state.config
    loop = asyncio.get_running_loop()
    scheduler_config = config.get("scheduler", {})
    api_port = config.get("listen", {}).get("port", 8888)
    credential_fallback = (
        config.get("credentials") or {}
    ).get("fallback_order") or []
    if credential_fallback:
        logger.info(
            "Credential fallback order (auth-failure only): %s",
            ", ".join(str(n) for n in credential_fallback),
        )
    # Jump-host (ProxyJump) config lives top-level, alongside `credentials` —
    # proxy routing is a global topology concern, not a per-job one.
    proxy_config = {
        "jump_hosts": config.get("jump_hosts") or {},
        "proxy_rules": config.get("proxy_rules") or [],
    }
    # Collection timeouts are a global concern (one fleet-wide default set),
    # so they are read top-level alongside jump hosts rather than nested
    # under the scheduler that consumes them.
    collection_config = config.get("collection") or {}
    logger.info(
        "Collection timeouts: connect=%ss idle=%sms inter_cmd=%ss "
        "echo_grace=%ss",
        collection_config.get("connect_timeout") or 90,
        collection_config.get("expect_prompt_timeout") or 3000,
        collection_config.get("inter_command_time", 1.0),
        collection_config.get("echo_grace") or 1.5,
    )
    scheduler = Scheduler(
        db=app.state.db,
        vault=app.state.vault,
        storage=app.state.storage,
        ws_manager=app.state.ws_manager,
        loop=loop,
        config=scheduler_config,
        parser=getattr(app.state, "parser", None),
        collection_config=collection_config,
        api_port=api_port,
        credential_fallback=credential_fallback,
        proxy_config=proxy_config,
        internal_token=getattr(app.state, "internal_token", None),
    )
    await scheduler.start()
    app.state.scheduler = scheduler
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle manager.

    Startup:  DB → storage → vault → WebSocket hub → scheduler
    Shutdown: scheduler → vault → DB
    """
    logger.info("Netlapse v%s starting", __version__)

    # A staged restore must land before anything reads config or opens a DB.
    # (main() also runs this before uvicorn; the second call is a no-op.)
    _apply_staged_restore()

    config = load_config()
    app.state.config = config
    from netlapse.backup import write_pidfile, default_config_path
    _cfg_dir = default_config_path().parent
    write_pidfile(_cfg_dir)
    # Default these so the attribute always exists. The parser is set
    # later inside the vault-unlock block; if that block is skipped
    # (vault locked) or throws, the parse-audit API must still find a
    # defined value and report the real reason rather than a hardcoded
    # "tfsm_templates.db not found".
    app.state.parser = None
    app.state.parser_error = (
        "parse engine not initialized — vault may be locked or an "
        "earlier startup step failed; check the daemon startup log"
    )

    # ── Initialize database ──────────────────────────────────────────
    db_path = Path(
        config.get("dcim_db", "~/.netlapse/netlapse.db")
    ).expanduser()
    db = NetlapseDB(db_path)
    fresh = db.init_schema()
    if fresh:
        logger.info("Created fresh database with default seed data")
    app.state.db = db

    # ── Sync definition registry (files → jobs table) ───────────────
    # Files are authoritative for capture/platform/job definitions; this
    # projects them into the jobs table. Fail-soft: missing files = inert,
    # invalid files = skip and run on the DB's last-good definitions.
    try:
        from netlapse.registry.service import run_startup_sync, resolve_definitions_dir
        from netlapse.registry.volatility import (
            set_definitions_dir, get_volatility_catalog, baseline_drift,
        )
        # Pin the volatility loader to the SAME directory the registry resolves
        # from config, so the semantic-diff defaults read volatility.yaml from
        # wherever captures.yaml lives (honors config['definitions_dir']).
        set_definitions_dir(resolve_definitions_dir(config))
        # Load it now and flag drift: the baseline heuristics apply from code
        # regardless, but a volatility.yaml that predates the shipped baseline
        # should announce itself rather than silently rely on the code floor.
        try:
            _missing = baseline_drift(get_volatility_catalog(config))
            if _missing:
                logger.warning(
                    "volatility.yaml is missing %d baseline heuristic(s) now "
                    "applied from code (%s). Filtering is still correct; update "
                    "%s/volatility.yaml to align it with the shipped baseline.",
                    len(_missing), ", ".join(_missing),
                    resolve_definitions_dir(config),
                )
        except Exception:
            pass
        run_startup_sync(db, config)
    except Exception as e:
        logger.warning("Definition registry sync failed (continuing): %s", e)

    # ── Initialize application auth (users, sessions, login gate) ────
    auth_manager = AuthManager(db, config.get("auth", {}))
    auth_manager.bootstrap_admin()
    auth_manager.store.purge_expired_sessions()
    app.state.auth = auth_manager
    # Ephemeral per-process token so the scheduler's loopback self-call to the
    # (now gated) REST API authenticates without a user session.
    import secrets as _secrets
    internal_token = _secrets.token_urlsafe(32)
    auth_manager.config["api_tokens"] = list(
        auth_manager.config.get("api_tokens") or []
    ) + [internal_token]
    app.state.internal_token = internal_token
    logger.info(
        "Auth: %s (provider=%s, %d user(s))",
        "enabled" if auth_manager.enabled else "DISABLED",
        (config.get("auth", {}) or {}).get("provider", "local"),
        auth_manager.store.count_users(),
    )

    # ── Initialize storage backend ───────────────────────────────────
    storage = create_backend(config)
    storage.initialize()
    app.state.storage = storage
    logger.info(
        "Storage backend: %s at %s",
        type(storage).__name__, storage.base_path,
    )

    # ── Initialize WebSocket hub ─────────────────────────────────────
    ws_manager = ConnectionManager()
    app.state.ws_manager = ws_manager

    # ── Enable emulation if configured ───────────────────────────────
    emu_config = config.get("emulation", {})
    if emu_config.get("enabled"):
        from netlapse.ssh.emulation import enable_emulation
        try:
            count = enable_emulation(
                lookup_path=emu_config.get("lookup_path"),
                bind_host=emu_config.get("bind_host", "127.0.0.1"),
            )
            logger.info("Emulation enabled: %d device IPs loaded", count)
        except FileNotFoundError as e:
            logger.error("Emulation enabled but lookup failed: %s", e)

    # ── Unlock vault (headless mode) ─────────────────────────────────
    # ── Initialize parse engine (independent of the vault) ───────────
    # Parsing/reparsing TextFSM output needs no secrets, so it must NOT be
    # gated behind the vault unlock. A locked vault disables *scheduled
    # collection*, not parse/reparse. (Previously this lived inside the
    # vault-unlock block, so a locked vault made the parse-audit API return
    # a misleading "tfsm_templates.db not found" 503.)
    parser = None
    app.state.parser_error = None
    try:
        from netlapse.parser import ParseEngine
        from netlapse.storage.health import (
            diagnose, checkpoint_and_clear, DEFAULT_DB_PATHS_STR,
        )
        parser_config = config.get("parser", {})
        resolved = parser_config.get("db_path") or DEFAULT_DB_PATHS_STR()
        if resolved:
            cp_err = checkpoint_and_clear(resolved)
            if cp_err:
                logger.warning("Template db checkpoint reported: %s", cp_err)
            h = diagnose(resolved)
            if not h.usable:
                app.state.parser_error = h.reason()
                raise RuntimeError(h.reason())
        parser = ParseEngine(
            db_path=parser_config.get("db_path"),
            min_score=parser_config.get("min_score", 15.0),
        )
        logger.info(
            "Parse engine ready: %d templates from %s",
            parser.template_count, parser.db_path,
        )
    except FileNotFoundError as e:
        app.state.parser_error = str(e)
        logger.info(
            "Parse engine disabled — tfsm_templates.db not found "
            "(place at ~/.netlapse/tfsm_templates.db to enable)"
        )
    except Exception as e:
        if not app.state.parser_error:
            app.state.parser_error = str(e)
        logger.warning("Parse engine init failed: %s", e)
    app.state.parser = parser

    vault = None
    scheduler = None

    try:
        from netlapse.vault.bridge import get_vault
        vault = get_vault(auto_unlock=True)
        app.state.vault = vault

        if vault.is_unlocked:
            logger.info("Vault unlocked — scheduler can start")

            # ── Start scheduler ──────────────────────────────────
            scheduler = await start_scheduler(app)
        else:
            if not vault.is_initialized:
                logger.warning(
                    "Vault not initialized — create it in the setup wizard "
                    "(/setup, or Admin → Setup) to enable collection"
                )
            else:
                logger.warning(
                    "Vault is locked — set NETLAPSE_VAULT_PASSWORD to "
                    "enable scheduled collection"
                )
            app.state.vault = vault
            app.state.scheduler = None

    except Exception as e:
        logger.warning("Vault initialization failed: %s", e)
        app.state.vault = None
        app.state.scheduler = None

    logger.info(
        "Netlapse ready — http://%s:%s",
        config.get("listen", {}).get("host", "0.0.0.0"),
        config.get("listen", {}).get("port", 8888),
    )

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    logger.info("Netlapse shutting down")

    scheduler = getattr(app.state, "scheduler", None)
    if scheduler:
        await scheduler.stop()

    vault = getattr(app.state, "vault", None)
    if vault and vault.is_unlocked:
        try:
            vault.lock()
        except Exception:
            pass

    db.close()

    from netlapse.backup import remove_pidfile
    remove_pidfile(_cfg_dir)


def _apply_staged_restore() -> None:
    """Apply a pending restore (netlapse restore / Settings → Restore)."""
    try:
        from netlapse.backup import apply_pending_restore
        res = apply_pending_restore()
    except Exception:
        logger.exception("Staged restore could not be applied; starting on current state")
        return
    if res and res.get("error"):
        logger.error("Staged restore failed: %s", res["error"])


app = FastAPI(
    title="Netlapse",
    description=(
        "Network state time-lapse. Periodic structured snapshots of "
        "network device operational state with versioned diffs. "
        "Oxidized-compatible API for drop-in replacement."
    ),
    version=__version__,
    lifespan=lifespan,
)

# Mount API routers
app.include_router(oxidized_router)
app.include_router(native_router)
app.include_router(web_router)
app.include_router(admin_router)
app.include_router(backup_router)
app.include_router(setup_router)
app.include_router(captures_router)
app.include_router(dcim_admin_router)
app.include_router(parse_audit_router)
app.include_router(changes_router)
app.include_router(topology_router)
app.include_router(auth_router)
app.include_router(users_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def _revalidate_ui(request: Request, call_next):
    """Make browsers revalidate the SPA shell and its ES modules on every
    load. Without this, heuristic caching keeps serving a stale app.js /
    views/*.js after an upgrade (modules are imported without a version).
    StaticFiles sends ETag/Last-Modified, so unchanged files cost a 304."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# Whole-UI login gate (reads app.state.auth at request time, set in lifespan).
install_auth_gate(app)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint — clients receive scheduler broadcast events."""
    # Authenticate the upgrade with the same session cookie as the UI.
    manager = getattr(websocket.app.state, "auth", None)
    if resolve_ws_user(websocket, manager) is None:
        await websocket.close(code=4401)  # 4401: app-level "unauthorized"
        return

    ws_manager = websocket.app.state.ws_manager
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; client may send pings
            data = await websocket.receive_text()
            # No echo — this is a broadcast-only channel
            # (client→server messages ignored for now)
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


def preflight(config: dict) -> None:
    """
    Interactive pre-start checks for the three states that leave Netlapse
    running-but-not-collecting, and that a first-time user hits silently:

      1. No config.yaml — running on bare built-in defaults.
      2. NETLAPSE_VAULT_PASSWORD unset — vault stays locked, scheduler can't
         collect (the API/UI still come up, which is what makes it confusing).
      3. tfsm_templates.db missing/unusable — collection runs but nothing
         parses (every capture scores MISS).

    Each problem is printed. If any are found AND we're on an interactive
    terminal, startup pauses for Enter (Ctrl-C aborts). On a non-interactive
    launch (systemd, no TTY) the warnings print and startup continues without
    blocking — an input() with no TTY would hang the daemon forever. Set
    NETLAPSE_NONINTERACTIVE=1 to skip the pause even on a TTY.
    """
    problems: list = []
    notes: list = []      # informational — never pause startup for these

    # 1. config.yaml present?
    config_path = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()
    if not config_path.exists():
        problems.append(
            f"No config.yaml at {config_path}\n"
            "      Running on built-in defaults (port 8888, file storage at "
            "~/.netlapse/data, no emulation).\n"
            "      Create one to set storage, scheduler, auth, jump hosts, etc."
        )

    # 2. vault initialized / password set?
    #    A vault that was never initialized is a fresh install: the web setup
    #    wizard creates it, so this is a pointer to /setup, not a problem that
    #    should hold startup at a prompt.
    listen = config.get("listen", {}) or {}
    host = listen.get("host", "0.0.0.0")
    host = "localhost" if host in ("0.0.0.0", "::", "") else host
    setup_url = f"http://{host}:{listen.get('port', 8888)}/setup"
    fresh = False
    try:
        from netlapse.vault.vault import CredentialVault
        fresh = not CredentialVault().is_initialized
    except Exception:
        fresh = not Path("~/.netlapse/vault.db").expanduser().exists()
    if fresh:
        notes.append(
            "First run — finish setup in the browser:\n"
            f"      {setup_url}\n"
            "      (admin account, credential vault, SSH credential, device import)"
        )
    elif not os.environ.get("NETLAPSE_VAULT_PASSWORD"):
        problems.append(
            "NETLAPSE_VAULT_PASSWORD is unset — the vault will stay LOCKED.\n"
            "      API and UI come up, but the scheduler will NOT collect.\n"
            "      export NETLAPSE_VAULT_PASSWORD=… before starting for "
            "unattended collection."
        )

    # 3. template database usable? Reuse the same resolution + verdict the
    #    parser uses, so the message is truthful (missing vs. corrupt).
    try:
        from netlapse.storage.health import diagnose, DEFAULT_DB_PATHS_STR
        parser_config = config.get("parser", {})
        resolved = parser_config.get("db_path") or DEFAULT_DB_PATHS_STR()
        if resolved:
            h = diagnose(resolved)
            if not h.usable:
                (notes if fresh else problems).append(
                    "Parser template database unusable — collection will run "
                    "but nothing parses (every capture scores MISS).\n"
                    f"      {h.reason()}\n"
                    "      Fix: python -m netlapse fetch-templates  (or copy one in), "
                    "then restart."
                )
    except Exception as e:  # never let the check itself break startup
        logger.debug("preflight template check skipped: %s", e)

    if not problems and not notes:
        return

    bar = "═" * 70
    print(f"\n{bar}")
    if problems:
        print(f"  Netlapse preflight — {len(problems)} item(s) need attention")
    else:
        print("  Netlapse preflight")
    print(bar)
    for i, p in enumerate(problems + notes, 1):
        print(f"  {i}. {p}\n")
    print(bar)

    # Notes alone (a fresh install the web wizard will finish) never pause.
    if not problems:
        print()
        return

    if os.environ.get("NETLAPSE_NONINTERACTIVE") or not sys.stdin.isatty():
        print("  Non-interactive start — continuing despite the above.")
        print(bar + "\n")
        return

    try:
        input("  Press Enter to start anyway, or Ctrl-C to abort... ")
    except (KeyboardInterrupt, EOFError):
        print("\n  Aborted.")
        raise SystemExit(1)


def main():
    """Entry point for `netlapse` CLI command."""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    _apply_staged_restore()
    config = load_config()
    preflight(config)
    listen = config.get("listen", {})

    uvicorn.run(
        "netlapse.app:app",
        host=listen.get("host", "0.0.0.0"),
        port=listen.get("port", 8888),
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()