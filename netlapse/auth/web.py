"""
Auth web layer — the HTTP surface.

Contains:
  * ``install_auth_gate(app)`` — one middleware that puts the *entire* UI and
    API behind a valid session, except the login page and the login endpoint.
    Browser navigations get a 302 to /login; API calls get a 401. Mutating
    requests get an Origin-based CSRF check.
  * ``resolve_ws_user`` — same cookie check for the WebSocket upgrade.
  * ``router`` — /login page, /api/v1/auth/* (login, logout, me, password),
    and /api/v1/admin/users/* CRUD.
  * ``require_user`` / ``require_admin`` dependencies for route handlers.

Machine clients (e.g. Oxidized consumers hitting /nodes) can bypass the cookie
gate with a bearer/X-API-Key token listed under ``auth.api_tokens`` in config,
or — when ``auth.basic_auth`` is true — with HTTP Basic credentials matching a
local user account (LibreNMS' ``oxidized.url = http://user:pass@host:port``).
Basic-authed requests are subject to the same role checks as a cookie session.
"""

import logging
from urllib.parse import urlparse

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

COOKIE_NAME = "netlapse_session"

# Paths reachable without a session.
_PUBLIC_PATHS = {"/login", "/api/v1/auth/login"}

# While no user account exists (setup mode) these are the ONLY reachable
# paths: the first-run page, its status read, and the first-admin claim.
_SETUP_OPEN_PATHS = {"/setup", "/api/v1/setup/status", "/api/v1/setup/admin"}

# Prefixes that should get a JSON 401 (not an HTML redirect) when unauthorized.
_API_PREFIXES = ("/api/", "/nodes", "/node/", "/reload")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Non-admin (viewer) users are read-only. These POSTs are reads or self-service
# and stay allowed for them; every other mutating request is blocked at the gate.
_VIEWER_POST_ALLOW = {
    "/api/v1/auth/logout",
    "/api/v1/auth/password",
    "/api/v1/auth/theme",
    "/api/v1/search",
    "/api/v1/search/global",      # One Search (read)
    "/api/v1/changes/verify",     # Changes feed verdicts (read)
    "/nodes/conf_search",
}

# ── Read-only API tokens (auth.readonly_api_tokens) ─────────────────────────
# For programmatic consumers such as the MCP server. Unlike auth.api_tokens
# (admin-equivalent), these resolve to the viewer role AND are confined to an
# explicit endpoint allowlist: inventory, capture metadata, filtered parsed
# records, gated raw lines, diffs (compact), search, changes, job status.
# Whole-capture endpoints (snapshots/latest, Oxidized fetch/version views)
# are deliberately absent. Collect Now is allowed only for a token whose entry
# sets allow_collect: true.
import hmac
import re as _re

_RO_GET = [_re.compile(p) for p in (
    r"^/api/v1/health$",
    r"^/api/v1/status/scheduler$",
    r"^/api/v1/(sites|platforms|roles)$",
    r"^/api/v1/devices$",
    r"^/api/v1/devices/\d+$",
    r"^/api/v1/devices/\d+/captures$",
    r"^/api/v1/devices/\d+/captures/[A-Za-z0-9_.-]+/(records|lines)$",
    r"^/api/v1/devices/\d+/snapshots$",
    r"^/api/v1/devices/\d+/diff$",
    r"^/api/v1/devices/\d+/collect/(plan|latest)$",
    r"^/api/v1/collect/requests/[A-Za-z0-9]+$",
    r"^/api/v1/jobs$",
    r"^/api/v1/jobs/[A-Za-z0-9_.-]+$",
    r"^/api/v1/jobs/[A-Za-z0-9_.-]+/history$",
    r"^/api/v1/history/\d+$",
    r"^/api/v1/search/capture_types$",
    r"^/api/v1/changes/candidates$",
    r"^/api/v1/parse-audit$",
)]
_RO_POST = {"/api/v1/search", "/api/v1/search/global", "/api/v1/changes/verify"}
_RO_COLLECT = _re.compile(r"^/api/v1/collect/\d+$")


def _presented_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.headers.get("x-api-key") or None


def _readonly_token_entry(request: Request, manager) -> Optional[dict]:
    """Match the presented token against auth.readonly_api_tokens. Entries are
    either a bare token string or {name, token, allow_collect}."""
    entries = manager.config.get("readonly_api_tokens") or []
    if not entries:
        return None
    presented = _presented_token(request)
    if not presented:
        return None
    for i, e in enumerate(entries):
        if isinstance(e, str):
            e = {"token": e, "name": f"readonly-{i}"}
        tok = str((e or {}).get("token") or "")
        if tok and hmac.compare_digest(tok.encode(), presented.encode()):
            return {"name": str(e.get("name") or f"readonly-{i}"),
                    "allow_collect": bool(e.get("allow_collect"))}
    return None


def _readonly_allowed(method: str, path: str, entry: dict) -> bool:
    if method in ("GET", "HEAD"):
        return any(rx.match(path) for rx in _RO_GET)
    if method == "POST":
        if path in _RO_POST:
            return True
        return bool(entry.get("allow_collect")) and bool(_RO_COLLECT.match(path))
    return False

# Admin-only surfaces — viewers are denied entirely (reads included), not just
# blocked from writes. Settings, the credential vault, and user management.
_ADMIN_ONLY_PREFIXES = (
    "/api/v1/admin/system",
    "/api/v1/admin/config",
    "/api/v1/admin/vault",
    "/api/v1/admin/users",
    "/api/v1/admin/backup",
    "/api/v1/setup",
)


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith(_API_PREFIXES):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _safe_next(raw: str) -> str:
    """Only allow same-site relative redirects (block open-redirects)."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _origin_ok(request: Request) -> bool:
    """CSRF guard: if an Origin/Referer is present it must be same-host."""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if origin:
        return urlparse(origin).netloc == host
    referer = request.headers.get("referer")
    if referer:
        return urlparse(referer).netloc == host
    # No browser-set Origin/Referer (e.g. curl) — not a CSRF vector.
    return True


def _api_token_ok(request: Request, manager) -> bool:
    tokens = set(manager.config.get("api_tokens") or [])
    if not tokens:
        return False
    presented = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        presented = auth[7:].strip()
    presented = presented or request.headers.get("x-api-key")
    return bool(presented) and presented in tokens


def _basic_auth_user(request: Request, manager):
    """Validate an HTTP Basic 'Authorization' header against the local user
    store. Returns the user dict on success, else None.

    This is what lets Oxidized consumers that embed credentials in the URL
    (e.g. LibreNMS' oxidized.url = http://user:pass@host:port) authenticate.
    The same accounts and roles as cookie login apply — a viewer account
    presented here stays read-only. Off unless auth.basic_auth is true.

    Note: Basic credentials are base64-encoded, not encrypted. Only enable
    over a trusted segment or behind TLS.
    """
    if not manager.config.get("basic_auth"):
        return None
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return None
    import base64
    import binascii
    try:
        raw = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    username, sep, password = raw.partition(":")
    if not sep:
        return None
    result = manager.local.authenticate(username, password)
    return result.user if result.ok else None


# Read-only Oxidized endpoints that may be exposed without a session when
# auth.oxidized_public is true. This is the real-Oxidized trust model: the
# upstream Oxidized REST API has no auth, so consumers like LibreNMS that
# cannot send credentials (their oxidized config stores only a bare URL) reach
# these over a trusted segment. Strictly reads — collection triggers
# (/node/next), search POSTs, and everything else stay behind the gate.
_OXIDIZED_PUBLIC_EXACT = {"/nodes", "/reload", "/node/version",
                          "/node/version/view", "/node/version/diffs"}
_OXIDIZED_PUBLIC_PREFIXES = ("/node/fetch/", "/node/show/")

# Second tier — the collection trigger (LibreNMS "Refresh" button). This is a
# mutating, side-effect route (/node/next/<node>): it enqueues a real SSH
# collection against a production device, so it is NOT part of the read set and
# is never covered by the read grant. It is a separate, opt-in, IP-scoped
# capability gated by auth.oxidized_public_trigger_ips (absent/empty = no one).
# A trigger IP must also satisfy the read fence (oxidized_public_allow_ips) when
# one is set, so trigger access is always a strict subset of read access.
# Methods: the handler implements GET and PUT; LibreNMS sends PUT.
_OXIDIZED_TRIGGER_PREFIX = "/node/next/"
_OXIDIZED_TRIGGER_METHODS = {"GET", "PUT"}


def _oxidized_public_ok(request: Request, manager) -> bool:
    """True when this request targets a read-only Oxidized route and public
    Oxidized access is enabled (optionally restricted by source IP)."""
    if not manager.config.get("oxidized_public"):
        # Feature switched off is the normal case, and this is the first of
        # four auth tiers — every request from a cookie-authenticated browser
        # passes through here on its way to the session check. Logging it was
        # emitting a WARNING per request, per static asset.
        return False
    # The collection trigger is owned by the second-tier gate; cede it silently
    # here so we don't emit a misleading method/read-set reject for a route the
    # trigger gate may legitimately allow.
    if request.url.path.startswith(_OXIDIZED_TRIGGER_PREFIX):
        return False
    if request.method not in _SAFE_METHODS:
        logger.warning("OXIPUB path=%s REJECT: method %s", request.url.path, request.method)
        return False
    path = request.url.path
    is_oxidized_read = (
        path in _OXIDIZED_PUBLIC_EXACT
        or path.startswith(_OXIDIZED_PUBLIC_PREFIXES)
    )
    if not is_oxidized_read:
        logger.warning("OXIPUB path=%s REJECT: not in read set %s / prefixes %s",
                       path, sorted(_OXIDIZED_PUBLIC_EXACT), _OXIDIZED_PUBLIC_PREFIXES)
        return False
    # Optional source-IP allowlist. When set, only these client IPs get the
    # unauthenticated pass; everyone else falls through to normal auth.
    allow = manager.config.get("oxidized_public_allow_ips")
    if allow:
        client = request.client.host if request.client else None
        if client not in set(allow):
            logger.warning("OXIPUB path=%s REJECT: client %r not in allow %s",
                           path, client, allow)
            return False
    logger.debug("OXIPUB path=%s ALLOW", path)
    return True


def _oxidized_trigger_ok(request: Request, manager) -> bool:
    """True when this request targets the collection-trigger route and the
    source IP has been explicitly granted the opt-in trigger capability.

    This is a second, narrower tier than the read grant. It is deliberately
    separate from _oxidized_public_ok: reading configs and poking production
    devices are different grants, and an operator opts into the trigger by
    listing source IPs in auth.oxidized_public_trigger_ips. Absent/empty means
    no IP may trigger collection over the public surface, which is the safe
    default for a side-effect route.
    """
    # Only meaningful when the public Oxidized surface is enabled at all.
    if not manager.config.get("oxidized_public"):
        return False
    path = request.url.path
    if not path.startswith(_OXIDIZED_TRIGGER_PREFIX):
        return False  # not the trigger route — not ours to judge

    # Opt-in capability. Distinct config key from the read allowlist.
    trigger_ips = manager.config.get("oxidized_public_trigger_ips")
    if not trigger_ips:
        logger.warning("OXIPUB path=%s REJECT: trigger capability not configured "
                       "(set auth.oxidized_public_trigger_ips to enable)", path)
        return False
    if request.method not in _OXIDIZED_TRIGGER_METHODS:
        logger.warning("OXIPUB path=%s REJECT: method %s not allowed for trigger",
                       path, request.method)
        return False
    client = request.client.host if request.client else None
    if client not in set(trigger_ips):
        logger.warning("OXIPUB path=%s REJECT: client %r not granted trigger capability %s",
                       path, client, trigger_ips)
        return False
    # Strict subset: a trigger IP must also clear the read fence when one is set.
    allow = manager.config.get("oxidized_public_allow_ips")
    if allow and client not in set(allow):
        logger.warning("OXIPUB path=%s REJECT: trigger client %r not in read allowlist %s",
                       path, client, allow)
        return False
    logger.debug("OXIPUB path=%s ALLOW (trigger)", path)
    return True


def install_auth_gate(app) -> None:
    """Register the global HTTP auth middleware on the FastAPI app."""

    @app.middleware("http")
    async def _gate(request: Request, call_next):
        manager = getattr(request.app.state, "auth", None)

        # Auth disabled or not yet ready → pass through (config opt-out).
        if manager is None or not manager.enabled:
            return await call_next(request)

        path = request.url.path
        if path in _PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        # Public read-only Oxidized access (config opt-in). For consumers that
        # cannot present credentials (e.g. LibreNMS stores only a bare URL).
        # Reads only, optionally source-IP restricted — see _oxidized_public_ok.
        if _oxidized_public_ok(request, manager):
            request.state.user = {"username": "(oxidized-public)", "role": "viewer",
                                  "id": None, "is_machine": True}
            return await call_next(request)

        # Second tier (config opt-in, IP-scoped): the collection trigger that
        # backs the LibreNMS Refresh button. Side-effect route, so it is gated
        # separately from reads — see _oxidized_trigger_ok.
        if _oxidized_trigger_ok(request, manager):
            request.state.user = {"username": "(oxidized-trigger)", "role": "viewer",
                                  "id": None, "is_machine": True}
            return await call_next(request)

        # Machine token bypass (Oxidized-style consumers).
        if _api_token_ok(request, manager):
            request.state.user = {"username": "(api-token)", "role": "admin",
                                  "id": None, "is_machine": True}
            return await call_next(request)

        # Read-only token (e.g. the MCP server): viewer role, allowlisted
        # endpoints only, Collect Now only with allow_collect.
        ro = _readonly_token_entry(request, manager)
        if ro is not None:
            if not _readonly_allowed(request.method, path, ro):
                logger.info("read-only token %r denied %s %s", ro["name"], request.method, path)
                return JSONResponse(
                    status_code=403,
                    content={"error": "not permitted for a read-only API token"},
                )
            request.state.user = {
                "username": f"(token:{ro['name']})", "role": "viewer", "id": None,
                "is_machine": True, "token_scope": "readonly",
                "allow_collect": ro["allow_collect"],
            }
            return await call_next(request)

        # Setup mode (no accounts yet): only the first-run page and its two
        # endpoints are reachable; everything else points at /setup.
        if manager.needs_setup:
            if path in _SETUP_OPEN_PATHS:
                if request.method not in _SAFE_METHODS and not _origin_ok(request):
                    return JSONResponse(
                        status_code=403,
                        content={"error": "cross-origin request blocked"},
                    )
                return await call_next(request)
            if _wants_json(request):
                return JSONResponse(
                    status_code=503,
                    content={"error": "setup required", "setup_url": "/setup"},
                )
            return RedirectResponse(url="/setup", status_code=302)

        token = request.cookies.get(COOKIE_NAME)
        user = manager.resolve(token) if token else None

        # HTTP Basic fallback (Oxidized consumers with URL-embedded creds).
        # Resolves to a real local user; role checks below still apply.
        if user is None:
            user = _basic_auth_user(request, manager)

        if user is None:
            if _wants_json(request):
                headers = ({"WWW-Authenticate": "Basic realm=\"Netlapse\""}
                           if manager.config.get("basic_auth") else None)
                return JSONResponse(
                    status_code=401, content={"error": "authentication required"},
                    headers=headers,
                )
            nxt = _safe_next(path + (("?" + request.url.query)
                                     if request.url.query else ""))
            return RedirectResponse(url=f"/login?next={nxt}", status_code=302)

        # Authenticated. CSRF check on state-changing requests.
        if request.method not in _SAFE_METHODS and not _origin_ok(request):
            return JSONResponse(
                status_code=403, content={"error": "cross-origin request blocked"}
            )

        is_admin = user.get("is_machine") or user.get("role") == "admin"

        # Admin-only surfaces: deny non-admins entirely (GET included).
        if not is_admin and path.startswith(_ADMIN_ONLY_PREFIXES):
            return JSONResponse(
                status_code=403,
                content={"error": "admin role required"},
            )

        # Role enforcement: viewers are read-only everywhere else.
        if (request.method not in _SAFE_METHODS
                and not is_admin
                and path not in _VIEWER_POST_ALLOW):
            return JSONResponse(
                status_code=403,
                content={"error": "read-only role: this action requires admin"},
            )

        request.state.user = user
        return await call_next(request)


def resolve_ws_user(websocket, manager):
    """Cookie auth for the WebSocket upgrade. Returns user dict or None."""
    if manager is None or not manager.enabled:
        return {"username": "(anon)", "role": "admin", "id": None}
    token = websocket.cookies.get(COOKIE_NAME)
    return manager.resolve(token) if token else None


# ═══════════════════════════════════════════════════════════════════════════
# Dependencies
# ═══════════════════════════════════════════════════════════════════════════

def require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        # Should be unreachable behind the gate, but be explicit.
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user.get("role") != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="admin role required")
    return user


# ═══════════════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════════════

router = APIRouter(tags=["auth"])


def _public_user(u: dict) -> dict:
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "role": u.get("role"),
        "must_change_password": bool(u.get("must_change_password")),
        "is_active": bool(u.get("is_active", 1)),
        "last_login_at": u.get("last_login_at"),
        "created_at": u.get("created_at"),
        "theme": u.get("theme", "light"),
        "auth_source": u.get("auth_source", "local"),
    }


def _set_session_cookie(response, token: str, manager) -> None:
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, samesite="lax",
        secure=manager.cookie_secure, max_age=manager.session_ttl, path="/",
    )


@router.get("/login", include_in_schema=False)
async def login_page(request: Request):
    manager = getattr(request.app.state, "auth", None)
    if manager and manager.enabled:
        if manager.needs_setup:
            return RedirectResponse(url="/setup", status_code=302)
        token = request.cookies.get(COOKIE_NAME)
        if token and manager.resolve(token):
            return RedirectResponse(url="/", status_code=302)
    return HTMLResponse(_LOGIN_HTML)


class LoginBody(BaseModel):
    username: str
    password: str
    next: str | None = None


@router.post("/api/v1/auth/login")
async def login(request: Request, body: LoginBody):
    manager = request.app.state.auth
    token = manager.login(
        body.username, body.password,
        user_agent=request.headers.get("user-agent"),
    )
    if not token:
        return JSONResponse(status_code=401,
                            content={"error": "invalid username or password"})
    user = manager.resolve(token)
    resp = JSONResponse(content={
        "ok": True, "user": _public_user(user),
        "next": _safe_next(body.next or "/"),
    })
    _set_session_cookie(resp, token, manager)
    return resp


@router.post("/api/v1/auth/logout")
async def logout(request: Request):
    manager = request.app.state.auth
    token = request.cookies.get(COOKIE_NAME)
    manager.logout(token)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/api/v1/auth/me")
async def me(request: Request):
    user = require_user(request)
    return {"user": _public_user(user), "auth": request.app.state.auth.mode_info()}


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


@router.post("/api/v1/auth/password")
async def change_own_password(request: Request, body: PasswordBody):
    manager = request.app.state.auth
    user = require_user(request)
    # Re-verify current password before allowing change.
    if not manager.login(user["username"], body.current_password):
        return JSONResponse(status_code=403,
                            content={"error": "current password is incorrect"})
    try:
        manager.set_password(user["id"], body.new_password)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    # set_password revokes sessions — issue a fresh one so the user stays in.
    token = manager.store.create_session(user["id"], manager.session_ttl,
                                          request.headers.get("user-agent"))
    resp = JSONResponse(content={"ok": True})
    _set_session_cookie(resp, token, manager)
    return resp


class ThemeBody(BaseModel):
    theme: str


@router.post("/api/v1/auth/theme")
async def set_own_theme(request: Request, body: ThemeBody):
    """Self-service UI theme preference. Allowed for every signed-in user
    (including read-only viewers). Takes effect on the next page load."""
    manager = request.app.state.auth
    user = require_user(request)
    theme = (body.theme or "").strip().lower()
    if theme not in ("light", "dark"):
        return JSONResponse(status_code=400,
                            content={"error": "theme must be 'light' or 'dark'"})
    if user.get("id") is not None:
        manager.set_theme(user["id"], theme)
    return {"theme": theme}


# ── User management (admin) ────────────────────────────────────────────────
users_router = APIRouter(prefix="/api/v1/admin/users", tags=["admin-users"])


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "admin"


class UpdateUserBody(BaseModel):
    role: str | None = None
    is_active: bool | None = None


class SetPasswordBody(BaseModel):
    password: str


@users_router.get("")
async def list_users(request: Request):
    require_admin(request)
    return {"users": [_public_user(u) for u in request.app.state.auth.list_users()]}


@users_router.post("")
async def create_user(request: Request, body: CreateUserBody):
    require_admin(request)
    try:
        u = request.app.state.auth.create_user(body.username, body.password,
                                                role=body.role)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"user": _public_user(u)}


@users_router.patch("/{user_id}")
async def update_user(request: Request, user_id: int, body: UpdateUserBody):
    actor = require_admin(request)
    mgr = request.app.state.auth
    try:
        if body.role is not None:
            mgr.set_role(user_id, body.role)
        if body.is_active is not None:
            mgr.set_active(user_id, body.is_active, acting_user_id=actor["id"])
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"user": _public_user(mgr.store.get_by_id(user_id))}


@users_router.post("/{user_id}/password")
async def reset_password(request: Request, user_id: int, body: SetPasswordBody):
    require_admin(request)
    try:
        request.app.state.auth.set_password(user_id, body.password)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True}


@users_router.delete("/{user_id}")
async def delete_user(request: Request, user_id: int):
    actor = require_admin(request)
    try:
        request.app.state.auth.delete_user(user_id, acting_user_id=actor["id"])
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# Login page (self-contained — /static is behind the gate, so no external deps)
# ═══════════════════════════════════════════════════════════════════════════

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Netlapse · Sign in</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--line:#334155;--fg:#e2e8f0;--mut:#94a3b8;
        --accent:#3b82f6;--err:#f87171}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:var(--bg);color:var(--fg);
       font-family:'IBM Plex Sans',system-ui,-apple-system,sans-serif}
  .card{width:340px;background:var(--card);border:1px solid var(--line);
        border-radius:12px;padding:32px 28px;box-shadow:0 12px 40px rgba(0,0,0,.45)}
  .brand{display:flex;align-items:center;gap:10px;margin-bottom:22px}
  .brand .ico{width:34px;height:34px;border-radius:8px;background:var(--accent);
        display:flex;align-items:center;justify-content:center}
  .brand h1{font-size:18px;margin:0;font-weight:600}
  .brand p{margin:0;font-size:12px;color:var(--mut)}
  label{display:block;font-size:12px;color:var(--mut);margin:14px 0 6px}
  input{width:100%;padding:10px 12px;background:#0f172a;border:1px solid var(--line);
        border-radius:8px;color:var(--fg);font-size:14px;outline:none}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:20px;padding:11px;border:0;border-radius:8px;
        background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .err{margin-top:14px;font-size:13px;color:var(--err);min-height:16px}
</style></head><body>
  <form class="card" id="f">
    <div class="brand">
      <div class="ico"><svg width="18" height="18" viewBox="0 0 24 24" fill="none"
        stroke="#fff" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/>
        <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg></div>
      <div><h1>Netlapse</h1><p>Sign in to continue</p></div>
    </div>
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password">
    <button id="b" type="submit">Sign in</button>
    <div class="err" id="e"></div>
  </form>
<script>
  const q=new URLSearchParams(location.search), nxt=q.get('next')||'/';
  const f=document.getElementById('f'),b=document.getElementById('b'),e=document.getElementById('e');
  f.addEventListener('submit',async(ev)=>{
    ev.preventDefault(); e.textContent=''; b.disabled=true; b.textContent='Signing in…';
    try{
      const r=await fetch('/api/v1/auth/login',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:document.getElementById('u').value,
          password:document.getElementById('p').value, next:nxt})});
      const d=await r.json().catch(()=>({}));
      if(!r.ok){throw new Error(d.error||'Sign in failed');}
      location.href=d.next||'/';
    }catch(err){e.textContent=err.message; b.disabled=false; b.textContent='Sign in';}
  });
</script></body></html>"""