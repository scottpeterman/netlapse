"""Netlapse application authentication: users, sessions, and the login gate."""

from netlapse.auth.manager import AuthManager, AuthError
from netlapse.auth.web import (
    router as auth_router,
    users_router,
    install_auth_gate,
    resolve_ws_user,
    require_user,
    require_admin,
    COOKIE_NAME,
)

__all__ = [
    "AuthManager",
    "AuthError",
    "auth_router",
    "users_router",
    "install_auth_gate",
    "resolve_ws_user",
    "require_user",
    "require_admin",
    "COOKIE_NAME",
]