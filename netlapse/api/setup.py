"""
First-run setup — the /setup page and the wizard's API.

Setup mode
----------
A fresh install with no user accounts (and no NETLAPSE_ADMIN_PASSWORD) is in
setup mode: the auth gate lets through only /setup, GET /api/v1/setup/status
and POST /api/v1/setup/admin. The FIRST visitor creates the admin account and
is signed in; from then on every /api/v1/setup endpoint is admin-only and the
rest of the wizard runs inside the app at #/setup.

Wizard steps (each reported live by /status, so the wizard can be reopened
and shows what is already done):

  admin        an account exists
  vault        initialized and unlocked. The wizard initializes it and
               unlocks it for this run, then starts the scheduler in place.
               Restarts need NETLAPSE_VAULT_PASSWORD — there is no web unlock.
  credential   at least one SSH credential, one of them the default
  devices      devices exist (SC2 map.json import with preview, or manual)
  templates    tfsm_templates.db loaded — placing it stays manual
  complete     operator finished the wizard (<config dir>/.setup-complete)
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["setup"])

COMPLETE_MARKER = ".setup-complete"
MAX_MAP_BYTES = 50 * 1024 * 1024


def _cfg_dir() -> Path:
    from netlapse.backup import default_config_path
    return default_config_path().parent


def _vault(request: Request):
    """The app's vault instance, created (locked) if startup didn't get one."""
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        from netlapse.vault.bridge import get_vault
        vault = get_vault(auto_unlock=False)
        request.app.state.vault = vault
    return vault


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": msg})


# ═══════════════════════════════════════════════════════════════════════════
# Status
# ═══════════════════════════════════════════════════════════════════════════

def _status(request: Request) -> dict:
    state = request.app.state
    manager = getattr(state, "auth", None)
    auth_enabled = bool(manager and manager.enabled)
    needs_admin = bool(auth_enabled and manager.needs_setup)

    out = {
        "needs_admin": needs_admin,
        "auth_enabled": auth_enabled,
        "local_auth": bool(manager and manager.mode_info().get("local_enabled")),
    }
    if needs_admin:
        # Before an admin exists, reveal nothing beyond "set up the admin".
        return out

    db = state.db
    vault = _vault(request)
    initialized = bool(vault.is_initialized)
    unlocked = bool(vault.is_unlocked)
    creds, default_cred = [], None
    if unlocked:
        try:
            for c in vault.list_credentials():
                if c.credential_type.value == "ssh":
                    creds.append({"id": c.id, "name": c.name,
                                  "username": c.display_username,
                                  "is_default": c.is_default})
                    if c.is_default:
                        default_cred = c.name
        except Exception as e:                      # noqa: BLE001
            logger.warning("setup status: listing credentials failed: %s", e)

    devices = db.conn.execute("SELECT COUNT(*) FROM dcim_device").fetchone()[0]
    sites = db.conn.execute("SELECT COUNT(*) FROM dcim_site").fetchone()[0]
    jobs = db.conn.execute("SELECT COUNT(*) FROM jobs WHERE enabled = 1").fetchone()[0]

    parser = getattr(state, "parser", None)
    config = getattr(state, "config", {}) or {}
    tpl_path = (config.get("parser") or {}).get("db_path") or "~/.netlapse/tfsm_templates.db"
    marker = _cfg_dir() / COMPLETE_MARKER
    completed = None
    if marker.exists():
        try:
            completed = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            completed = {"completed_at": None}

    sched = getattr(state, "scheduler", None)
    out.update({
        "vault": {
            "initialized": initialized,
            "unlocked": unlocked,
            "env_password_set": bool(os.environ.get("NETLAPSE_VAULT_PASSWORD")),
        },
        "credentials": creds,
        "default_credential": default_cred,
        "devices": devices,
        "sites": sites,
        "enabled_jobs": jobs,
        "templates": {
            "loaded": parser is not None,
            "count": getattr(parser, "template_count", 0) if parser else 0,
            "path": str(Path(tpl_path).expanduser()),
            "error": getattr(state, "parser_error", None),
        },
        "scheduler_running": sched is not None,
        "config_dir": str(_cfg_dir()),
        "complete": completed,
    })
    out["steps"] = {
        "admin": True,
        "vault": initialized and unlocked,
        "credential": bool(default_cred),
        "devices": devices > 0,
        "templates": parser is not None,
    }
    # Auto-open the wizard only for installs that are genuinely not set up —
    # an existing deployment upgraded to this version has a vault and devices.
    out["should_open"] = completed is None and (not initialized or devices == 0)
    return out


@router.get("/api/v1/setup/status")
def setup_status(request: Request):
    return _status(request)


# ═══════════════════════════════════════════════════════════════════════════
# Step: first admin (setup mode only)
# ═══════════════════════════════════════════════════════════════════════════

class AdminBody(BaseModel):
    username: str
    password: str
    confirm: str


@router.post("/api/v1/setup/admin")
async def claim_admin(request: Request, body: AdminBody):
    from netlapse.auth.manager import AuthError
    from netlapse.auth.web import _public_user, _set_session_cookie

    manager = getattr(request.app.state, "auth", None)
    if manager is None or not manager.enabled:
        return _err(400, "authentication is disabled in config — no admin account is needed")
    if body.password != body.confirm:
        return _err(400, "passwords do not match")
    try:
        user = manager.claim_first_admin(body.username, body.password)
    except AuthError as e:
        return _err(409 if "already" in str(e) else 400, str(e))

    token = manager.store.create_session(
        user["id"], manager.session_ttl, request.headers.get("user-agent"))
    resp = JSONResponse(content={"ok": True, "user": _public_user(user),
                                 "next": "/#/setup"})
    _set_session_cookie(resp, token, manager)
    return resp


# ═══════════════════════════════════════════════════════════════════════════
# Step: vault
# ═══════════════════════════════════════════════════════════════════════════

class VaultBody(BaseModel):
    password: str
    confirm: str


@router.post("/api/v1/setup/vault")
async def init_vault(request: Request, body: VaultBody):
    """Initialize the vault, unlock it for this run, start the scheduler."""
    vault = _vault(request)
    if vault.is_initialized:
        if vault.is_unlocked:
            return _err(409, "the vault is already initialized and unlocked")
        return _err(409, "the vault is already initialized but locked — set "
                         "NETLAPSE_VAULT_PASSWORD to its master password and restart")
    if body.password != body.confirm:
        return _err(400, "passwords do not match")
    if len(body.password or "") < 8:
        return _err(400, "master password must be at least 8 characters")
    env_pw = os.environ.get("NETLAPSE_VAULT_PASSWORD")
    if env_pw and env_pw != body.password:
        # Initializing with a different password would work now and then
        # leave the vault locked (collection stopped) after the next restart.
        return _err(400, "NETLAPSE_VAULT_PASSWORD is set in this process's "
                         "environment and doesn't match — use that password, "
                         "or unset the variable and restart first")

    def _init():
        vault.initialize(body.password)
        if not vault.is_unlocked:
            vault.unlock(body.password)

    try:
        await run_in_threadpool(_init)
    except Exception as e:                          # noqa: BLE001
        logger.exception("Setup: vault initialization failed")
        return _err(400, f"vault initialization failed: {e}")
    logger.warning("Setup: vault initialized and unlocked for this run")

    scheduler_error = None
    try:
        from netlapse.app import start_scheduler
        await start_scheduler(request.app)
        logger.info("Setup: scheduler started after vault initialization")
    except Exception as e:                          # noqa: BLE001
        logger.exception("Setup: scheduler start failed")
        scheduler_error = str(e)

    st = _status(request)
    st["scheduler_error"] = scheduler_error
    return st


# ═══════════════════════════════════════════════════════════════════════════
# Step: devices — SC2 map.json preview / import
# ═══════════════════════════════════════════════════════════════════════════

def _role_rules(db):
    from netlapse.dcim.map_importer import ROLE_RULES_FILE, load_role_rules
    from netlapse.registry.service import resolve_definitions_dir
    roles_path = resolve_definitions_dir() / ROLE_RULES_FILE
    known = [r[0] for r in db.conn.execute("SELECT slug FROM dcim_device_role").fetchall()]
    return load_role_rules(roles_path, known_roles=known)


def _site_slug(raw: Optional[str]) -> Optional[str]:
    """Normalize a forced site to slug form (lowercase, [a-z0-9_-])."""
    import re
    s = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-")
    return s or None


async def _map_sync(request: Request, dry_run: bool, site: Optional[str],
                    exclude: Optional[str], collection: bool,
                    skip_unknown: bool = False):
    from netlapse.dcim.map_importer import clear_caches, parse_platform, sync_from_map

    raw = await request.body()
    if not raw:
        return _err(400, "upload a map.json file")
    if len(raw) > MAX_MAP_BYTES:
        return _err(413, "map file is larger than 50 MB")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return _err(400, f"not valid JSON: {e}")
    if not isinstance(data, dict) or not data:
        return _err(400, "expected a Secure Cartography map.json (an object of hostname → node)")

    db = request.app.state.db
    excludes = [p.strip() for p in (exclude or "").split(",") if p.strip()] or None
    site = _site_slug(site)
    known_platforms = {r[0] for r in db.conn.execute("SELECT slug FROM dcim_platform")}

    # Optionally drop nodes whose platform string maps to no known platform
    # (SC2 nodes discovered by IP with no platform, unsupported vendors).
    # They would import but never collect until a platform is set by hand.
    dropped = []
    if skip_unknown:
        kept = {}
        for key, node in data.items():
            pstr = ((node or {}).get("node_details") or {}).get("platform", "")
            if parse_platform(pstr)[1] in known_platforms:
                kept[key] = node
            else:
                dropped.append(key)
        data = kept
        if not data:
            return _err(400, "no devices with a recognized platform in this map")

    def work():
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = f.name
        try:
            clear_caches()
            return sync_from_map(
                db=db, map_path=tmp, role_rules=_role_rules(db),
                exclude_prefixes=excludes, site_override=site,
                collection_enabled=collection, dry_run=dry_run,
            )
        finally:
            os.unlink(tmp)

    try:
        result = await run_in_threadpool(work)
    except ValueError as e:                         # roles.yaml / map shape problems
        return _err(400, str(e))
    except Exception as e:                          # noqa: BLE001
        logger.exception("Setup: map %s failed", "preview" if dry_run else "import")
        return _err(400, f"map could not be processed: {e}")

    out = {
        "skipped_unknown_platform": len(dropped),
        "site_override": site,
        "created": result.created, "updated": result.updated,
        "skipped": result.skipped, "errors": result.errors[:50],
        "sites_created": result.sites_created, "roles_created": result.roles_created,
    }
    if dry_run:
        devs = []
        for d in result.devices:
            exists = db.get_device_by_name_and_site(d["name"], d["site"]) \
                if hasattr(db, "get_device_by_name_and_site") else None
            devs.append({**d, "action": "update" if exists else "create",
                         "platform_known": d.get("platform") in known_platforms})
        by = lambda k: {v: sum(1 for x in devs if x.get(k) == v)
                        for v in sorted({str(x.get(k)) for x in devs})}
        out.update({
            "devices": devs,
            "total": len(devs),
            "by_site": by("site"),
            "by_platform": by("platform"),
            "by_role": by("role"),
            "unknown_platforms": sorted({d["platform"] for d in devs if not d["platform_known"]}),
            "would_create": sum(1 for d in devs if d["action"] == "create"),
            "would_update": sum(1 for d in devs if d["action"] == "update"),
        })
    else:
        logger.warning("Setup: map import — %d created, %d updated, %d skipped",
                       result.created, result.updated, result.skipped)
    return out


@router.post("/api/v1/setup/map/preview")
async def map_preview(request: Request, site: Optional[str] = None,
                      exclude: Optional[str] = None, collection: bool = True,
                      skip_unknown: bool = False):
    return await _map_sync(request, True, site, exclude, collection, skip_unknown)


@router.post("/api/v1/setup/map/import")
async def map_import(request: Request, site: Optional[str] = None,
                     exclude: Optional[str] = None, collection: bool = True,
                     skip_unknown: bool = False):
    return await _map_sync(request, False, site, exclude, collection, skip_unknown)


# ═══════════════════════════════════════════════════════════════════════════
# Finish
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/api/v1/setup/complete")
def setup_complete(request: Request):
    user = getattr(request.state, "user", {}) or {}
    marker = _cfg_dir() / COMPLETE_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "by": user.get("username"),
    }))
    return _status(request)


# ═══════════════════════════════════════════════════════════════════════════
# /setup — first-run page (create the admin)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/setup", include_in_schema=False)
async def setup_page(request: Request):
    manager = getattr(request.app.state, "auth", None)
    if manager is None or not manager.enabled or not manager.needs_setup:
        return RedirectResponse(url="/#/setup", status_code=302)
    return HTMLResponse(_SETUP_HTML)


_SETUP_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Netlapse · Setup</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--line:#334155;--fg:#e2e8f0;--mut:#94a3b8;
        --accent:#3b82f6;--err:#f87171;--warn:#fbbf24}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:var(--bg);color:var(--fg);padding:16px;
       font-family:'IBM Plex Sans',system-ui,-apple-system,sans-serif}
  .card{width:380px;max-width:100%;background:var(--card);border:1px solid var(--line);
        border-radius:12px;padding:30px 28px;box-shadow:0 12px 40px rgba(0,0,0,.45)}
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:18px}
  .brand .ico{width:34px;height:34px;border-radius:8px;background:var(--accent);
        display:flex;align-items:center;justify-content:center}
  .brand h1{font-size:18px;margin:0;font-weight:600}
  .brand p{margin:0;font-size:12px;color:var(--mut)}
  .note{font-size:12.5px;line-height:1.5;color:var(--mut);margin:0 0 6px}
  .note b{color:var(--warn);font-weight:600}
  label{display:block;font-size:12px;color:var(--mut);margin:14px 0 6px}
  input{width:100%;padding:10px 12px;background:#0f172a;border:1px solid var(--line);
        border-radius:8px;color:var(--fg);font-size:14px;outline:none}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:20px;padding:11px;border:0;border-radius:8px;
        background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .err{margin-top:14px;font-size:13px;color:var(--err);min-height:16px}
  .steps{font-size:12px;color:var(--mut);margin:14px 0 0;padding-left:18px;line-height:1.7}
</style></head><body>
  <form class="card" id="f">
    <div class="brand">
      <div class="ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"
        stroke="#fff" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/>
        <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg></div>
      <div><h1>Welcome to Netlapse</h1><p>First-run setup · step 1 of 6</p></div>
    </div>
    <p class="note">Create the administrator account. <b>This page is open to anyone who can
      reach this port until an account exists</b> — the first visitor becomes admin.</p>
    <label for="u">Admin username</label>
    <input id="u" value="admin" autocomplete="username">
    <label for="p">Password <span style="opacity:.7">(min 8 characters)</span></label>
    <input id="p" type="password" autocomplete="new-password" autofocus>
    <label for="c">Confirm password</label>
    <input id="c" type="password" autocomplete="new-password">
    <button id="b" type="submit">Create admin &amp; continue</button>
    <div class="err" id="e"></div>
    <ol class="steps">
      <li>Admin account</li><li>Credential vault</li><li>SSH credential</li>
      <li>Devices (map import)</li><li>Template database</li><li>Finish</li>
    </ol>
  </form>
<script>
  const f=document.getElementById('f'),b=document.getElementById('b'),e=document.getElementById('e');
  f.addEventListener('submit',async(ev)=>{
    ev.preventDefault(); e.textContent='';
    const u=document.getElementById('u').value.trim(),
          p=document.getElementById('p').value, c=document.getElementById('c').value;
    if(p.length<8){e.textContent='Password must be at least 8 characters';return;}
    if(p!==c){e.textContent='Passwords do not match';return;}
    b.disabled=true; b.textContent='Creating…';
    try{
      const r=await fetch('/api/v1/setup/admin',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:u,password:p,confirm:c})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok){
        if(r.status===409){location.href='/login';return;}
        throw new Error(d.error||'Setup failed');
      }
      location.href=d.next||'/#/setup';
    }catch(err){e.textContent=err.message; b.disabled=false; b.textContent='Create admin & continue';}
  });
</script></body></html>"""
