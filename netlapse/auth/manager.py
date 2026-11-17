"""
AuthManager — orchestration over store + provider(s).

Owns: first-run admin bootstrap, login (authenticate → mint session), logout,
session resolution for the request gate, and the guarded user-CRUD operations
the admin UI calls. Lockout guards (can't remove the last admin, can't disable
yourself) live here so every caller — API, future CLI — gets them for free.
"""

import logging
import os
import secrets
from typing import List, Optional

from netlapse.auth.providers import (
    AuthResult, LocalAuthProvider, LdapAuthProvider,
)
from netlapse.auth.store import AuthStore, VALID_ROLES

logger = logging.getLogger(__name__)

DEFAULT_SESSION_TTL = 7 * 24 * 3600  # 7 days
MIN_PASSWORD_LEN = 8
ENV_ADMIN_PASSWORD = "NETLAPSE_ADMIN_PASSWORD"


class AuthError(Exception):
    """Raised for guard violations (last admin, self-disable, weak password)."""


class AuthManager:
    def __init__(self, db, config: Optional[dict] = None):
        config = config or {}
        self.config = config
        self.enabled = bool(config.get("enabled", True))
        self.session_ttl = int(config.get("session_ttl", DEFAULT_SESSION_TTL))
        self.cookie_secure = bool(config.get("cookie_secure", False))

        self.store = AuthStore(db)
        self.store.ensure_schema()

        provider_name = (config.get("provider") or "local").lower()
        self.local = LocalAuthProvider(self.store)
        # Provider chain — local first; LDAP appended when implemented.
        self.providers = [self.local]
        if provider_name == "ldap":
            self.providers = [LdapAuthProvider(config.get("ldap", {}), self.store)]
        elif provider_name == "ldap+local":
            self.providers = [
                LdapAuthProvider(config.get("ldap", {}), self.store),
                self.local,
            ]

    # ── Bootstrap ────────────────────────────────────────────────────────
    def bootstrap_admin(self) -> None:
        """
        Ensure an 'admin' account exists and honor NETLAPSE_ADMIN_PASSWORD.

        Behavior:
          * Fresh install (no users): create 'admin' from the env var if set,
            otherwise create nothing — the instance stays in setup mode and
            the first visitor to /setup creates the admin.
          * 'admin' already exists + env var set: reset its password to the env
            value and re-enable it (break-glass). Set the env var to recover a
            locked-out lab; UNSET it after first login to stop resets on every
            restart.
        Idempotent; runs at startup.
        """
        env_pw = os.environ.get(ENV_ADMIN_PASSWORD)
        existing = self.store.get_by_username("admin")

        # Break-glass requires a local provider in the chain. In pure 'ldap'
        # mode the reset below still happens, but there is NO login path that
        # validates a local password — so warn loudly instead of locking the
        # operator out silently. Recovery: provider local/ldap+local + restart.
        if env_pw and not any(
            getattr(p, "name", "") == "local" for p in self.providers
        ):
            logger.warning(
                "\n%s\n  %s is set, but auth.provider=%r has no local fallback.\n"
                "  The break-glass admin reset CANNOT be used to log in while\n"
                "  LDAP-only. To break glass: set auth.provider to 'local' (or\n"
                "  'ldap+local') and restart, then log in as admin.\n%s",
                "!" * 64, ENV_ADMIN_PASSWORD,
                (self.config.get("provider") or "local"), "!" * 64,
            )

        if existing is None:
            if env_pw:
                self.store.create_user("admin", env_pw, role="admin")
                logger.info("Created 'admin' account from %s", ENV_ADMIN_PASSWORD)
            elif self.store.count_users() == 0:
                # Setup mode: no account is created here. The first visitor
                # to /setup creates the admin (see api/setup.py).
                logger.warning(
                    "\n%s\n  No user accounts yet — Netlapse is in SETUP MODE.\n"
                    "  Open /setup in a browser: the FIRST VISITOR creates the\n"
                    "  admin account. (Set %s to create 'admin' headlessly.)\n%s",
                    "═" * 64, ENV_ADMIN_PASSWORD, "═" * 64,
                )
            # else: other users exist but no 'admin' and no env var — leave as-is
            return

        # 'admin' exists — env var resets it (break-glass).
        if env_pw:
            self.store.set_password(existing["id"], env_pw)
            if not existing.get("is_active"):
                self.store.set_active(existing["id"], True)
            logger.warning(
                "'admin' password reset from %s (break-glass). "
                "Unset the env var after login to stop resets on restart.",
                ENV_ADMIN_PASSWORD,
            )

    # ── Setup mode ───────────────────────────────────────────────────────
    @property
    def needs_setup(self) -> bool:
        """True while no user account exists (fresh install, no env admin).
        Cached once False — the last-admin guard keeps it from returning."""
        if getattr(self, "_has_users", False):
            return False
        self._has_users = self.store.count_users() > 0
        return not self._has_users

    def claim_first_admin(self, username: str, password: str) -> dict:
        """Create the first admin. Only valid while no users exist; the check
        and insert are serialized so two racing visitors can't both win."""
        import threading
        lock = getattr(AuthManager, "_claim_lock", None)
        if lock is None:
            lock = AuthManager._claim_lock = threading.Lock()
        username = (username or "").strip()
        if not username:
            raise AuthError("username is required")
        if len(password or "") < MIN_PASSWORD_LEN:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LEN} characters")
        if not any(getattr(p, "name", "") == "local" for p in self.providers):
            raise AuthError(
                "auth.provider has no local provider, so a local admin could not "
                "log in — set provider to 'local' or 'ldap+local' first"
            )
        with lock:
            if self.store.count_users() > 0:
                raise AuthError("setup is already complete — sign in instead")
            self.store.create_user(username, password, role="admin")
            self._has_users = True
        user = self.store.get_by_username(username)
        logger.warning("Setup: first admin account %r created", username)
        return user

    # ── Login / logout / session ─────────────────────────────────────────
    def login(
        self, username: str, password: str, user_agent: Optional[str] = None
    ) -> Optional[str]:
        """Authenticate and return a session token, or None on failure."""
        result = AuthResult.failure("no provider")
        for provider in self.providers:
            try:
                result = provider.authenticate(username or "", password or "")
            except NotImplementedError:
                continue
            if result.ok:
                break
        if not result.ok or not result.user:
            logger.info("login failed for %r: %s", username, result.reason)
            return None
        user = result.user
        self.store.touch_login(user["id"])
        token = self.store.create_session(
            user["id"], self.session_ttl, user_agent
        )
        logger.info("login ok: %s", user["username"])
        return token

    def logout(self, token: str) -> None:
        if token:
            self.store.revoke_session(token)

    def resolve(self, token: str) -> Optional[dict]:
        """Return the user dict for a valid session token, else None."""
        return self.store.resolve_session(token)

    def mode_info(self) -> dict:
        """
        Describe the active auth mode for the UI. Lets the Users page reflect
        reality instead of offering controls that the auth layer ignores.
        """
        provider = (self.config.get("provider") or "local").lower()
        names = [getattr(p, "name", "") for p in self.providers]
        ldap_enabled = "ldap" in names
        local_enabled = "local" in names
        ldap_mock = bool(
            (self.config.get("ldap") or {}).get("mock", {}).get("enabled")
        )
        return {
            "provider": provider,            # local | ldap | ldap+local
            "ldap_enabled": ldap_enabled,
            "local_enabled": local_enabled,
            # In LDAP-only mode local accounts have no login path; role is
            # directory-authoritative for everyone who can log in.
            "ldap_only": ldap_enabled and not local_enabled,
            "directory_authoritative": ldap_enabled,
            "ldap_mock": ldap_mock,
        }

    # ── User CRUD (admin) ────────────────────────────────────────────────
    def list_users(self) -> List[dict]:
        return self.store.list_users()

    def create_user(
        self, username: str, password: str, role: str = "admin"
    ) -> dict:
        self._check_password(password)
        if role not in VALID_ROLES:
            raise AuthError(f"invalid role: {role}")
        return self.store.create_user(username, password, role=role)

    def set_password(self, user_id: int, password: str) -> None:
        self._check_password(password)
        if not self.store.get_by_id(user_id):
            raise AuthError("user not found")
        self.store.set_password(user_id, password)
        # Force re-login everywhere after a password change.
        self.store.revoke_user_sessions(user_id)

    def set_active(self, user_id: int, active: bool, acting_user_id: int) -> None:
        user = self.store.get_by_id(user_id)
        if not user:
            raise AuthError("user not found")
        if not active:
            if user_id == acting_user_id:
                raise AuthError("you cannot disable your own account")
            if user["role"] == "admin" and self.store.count_active_admins() <= 1:
                raise AuthError("cannot disable the last active admin")
        self.store.set_active(user_id, active)

    def set_role(self, user_id: int, role: str) -> None:
        user = self.store.get_by_id(user_id)
        if not user:
            raise AuthError("user not found")
        if (user["role"] == "admin" and role != "admin"
                and self.store.count_active_admins() <= 1):
            raise AuthError("cannot demote the last active admin")
        self.store.set_role(user_id, role)

    def set_theme(self, user_id: int, theme: str) -> None:
        """Self-service UI preference; no role guard."""
        self.store.set_theme(user_id, theme)

    def delete_user(self, user_id: int, acting_user_id: int) -> None:
        user = self.store.get_by_id(user_id)
        if not user:
            raise AuthError("user not found")
        if user_id == acting_user_id:
            raise AuthError("you cannot delete your own account")
        if user["role"] == "admin" and self.store.count_active_admins() <= 1:
            raise AuthError("cannot delete the last active admin")
        self.store.delete_user(user_id)

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _check_password(password: str) -> None:
        if not password or len(password) < MIN_PASSWORD_LEN:
            raise AuthError(
                f"password must be at least {MIN_PASSWORD_LEN} characters"
            )