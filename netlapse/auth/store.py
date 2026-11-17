"""
Auth persistence — ``app_users`` and ``app_sessions``.

These tables live in the same ``netlapse.db`` as everything else and are
reached through the existing ``NetlapseDB`` thread-safe connection proxy, so
there is no second SQLite handle on the file and no WAL/lock contention. The
schema is created idempotently at startup via ``ensure_schema`` — independent
of the big DCIM migration chain, so it can't perturb it.

Design choices worth noting:
  * App-login passwords are one-way *hashed* here (scrypt). They are NOT in the
    vault — the vault is for *retrievable* device secrets, a deliberately
    different security property.
  * Session tokens are never stored in the clear: the cookie holds the raw
    token, the DB holds its SHA-256. A DB read alone can't mint a live session.
  * ``role`` exists from day one (defaulting to 'admin') so viewer/operator can
    be added later without a migration.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from netlapse.auth.hashing import hash_password

logger = logging.getLogger(__name__)

VALID_ROLES = ("admin", "viewer")  # admin: full control; viewer: read-only

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash        TEXT    NOT NULL,
    role                 TEXT    NOT NULL DEFAULT 'admin',
    is_active            INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL,
    last_login_at        TEXT,
    theme                TEXT    NOT NULL DEFAULT 'light',
    auth_source          TEXT    NOT NULL DEFAULT 'local'
);

CREATE TABLE IF NOT EXISTS app_sessions (
    token_hash   TEXT    PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    last_seen_at TEXT    NOT NULL,
    user_agent   TEXT
);

CREATE INDEX IF NOT EXISTS idx_app_sessions_user ON app_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_app_sessions_exp  ON app_sessions(expires_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    """Thin data-access layer over the shared NetlapseDB connection."""

    def __init__(self, db):
        self.db = db

    def ensure_schema(self) -> None:
        self.db.conn.executescript(_SCHEMA)
        # Migration: add theme to app_users tables created before theming.
        cols = {r["name"] for r in
                self.db.conn.execute("PRAGMA table_info(app_users)")}
        if "theme" not in cols:
            self.db.conn.execute(
                "ALTER TABLE app_users "
                "ADD COLUMN theme TEXT NOT NULL DEFAULT 'light'"
            )
        if "auth_source" not in cols:
            self.db.conn.execute(
                "ALTER TABLE app_users "
                "ADD COLUMN auth_source TEXT NOT NULL DEFAULT 'local'"
            )
        self.db.conn.commit()

    # ── Users ────────────────────────────────────────────────────────────
    def count_users(self, active_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM app_users"
        if active_only:
            sql += " WHERE is_active = 1"
        return int(self.db.conn.execute(sql).fetchone()["n"])

    def count_active_admins(self) -> int:
        row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM app_users "
            "WHERE is_active = 1 AND role = 'admin'"
        ).fetchone()
        return int(row["n"])

    def get_by_username(self, username: str) -> Optional[dict]:
        row = self.db.conn.execute(
            "SELECT * FROM app_users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        return dict(row) if row else None

    def get_by_id(self, user_id: int) -> Optional[dict]:
        row = self.db.conn.execute(
            "SELECT * FROM app_users WHERE id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[dict]:
        rows = self.db.conn.execute(
            "SELECT id, username, role, is_active, must_change_password, "
            "created_at, updated_at, last_login_at, auth_source "
            "FROM app_users ORDER BY username COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "admin",
        must_change_password: bool = False,
        auth_source: str = "local",
    ) -> dict:
        username = username.strip()
        if not username:
            raise ValueError("username is required")
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")
        now = _iso(_now())
        try:
            cur = self.db.conn.execute(
                "INSERT INTO app_users "
                "(username, password_hash, role, is_active, "
                " must_change_password, created_at, updated_at, auth_source) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (username, hash_password(password), role,
                 1 if must_change_password else 0, now, now, auth_source),
            )
            self.db.conn.commit()
        except Exception as e:
            # UNIQUE violation surfaces here
            if "UNIQUE" in str(e).upper():
                raise ValueError(f"username already exists: {username}")
            raise
        return self.get_by_id(cur.lastrowid)

    def set_password(
        self, user_id: int, password: str, must_change: bool = False
    ) -> None:
        self.db.conn.execute(
            "UPDATE app_users SET password_hash = ?, must_change_password = ?, "
            "updated_at = ? WHERE id = ?",
            (hash_password(password), 1 if must_change else 0,
             _iso(_now()), user_id),
        )
        self.db.conn.commit()

    def update_password_hash(self, user_id: int, password_hash: str) -> None:
        """Used for transparent rehash-on-login; takes a prepared hash."""
        self.db.conn.execute(
            "UPDATE app_users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, _iso(_now()), user_id),
        )
        self.db.conn.commit()

    def set_active(self, user_id: int, active: bool) -> None:
        self.db.conn.execute(
            "UPDATE app_users SET is_active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, _iso(_now()), user_id),
        )
        self.db.conn.commit()
        if not active:
            self.revoke_user_sessions(user_id)

    def set_role(self, user_id: int, role: str) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")
        self.db.conn.execute(
            "UPDATE app_users SET role = ?, updated_at = ? WHERE id = ?",
            (role, _iso(_now()), user_id),
        )
        self.db.conn.commit()

    def set_auth_source(self, user_id: int, source: str) -> None:
        self.db.conn.execute(
            "UPDATE app_users SET auth_source = ?, updated_at = ? WHERE id = ?",
            (source, _iso(_now()), user_id),
        )
        self.db.conn.commit()

    def set_theme(self, user_id: int, theme: str) -> None:
        if theme not in ("light", "dark"):
            raise ValueError(f"invalid theme: {theme}")
        self.db.conn.execute(
            "UPDATE app_users SET theme = ?, updated_at = ? WHERE id = ?",
            (theme, _iso(_now()), user_id),
        )
        self.db.conn.commit()

    def touch_login(self, user_id: int) -> None:
        self.db.conn.execute(
            "UPDATE app_users SET last_login_at = ? WHERE id = ?",
            (_iso(_now()), user_id),
        )
        self.db.conn.commit()

    def delete_user(self, user_id: int) -> None:
        # ON DELETE CASCADE clears sessions
        self.db.conn.execute("DELETE FROM app_users WHERE id = ?", (user_id,))
        self.db.conn.commit()

    # ── Sessions ─────────────────────────────────────────────────────────
    def create_session(
        self, user_id: int, ttl_seconds: int, user_agent: Optional[str] = None
    ) -> str:
        """Mint a session, store its hash, return the raw token for the cookie."""
        token = secrets.token_urlsafe(32)
        now = _now()
        self.db.conn.execute(
            "INSERT INTO app_sessions "
            "(token_hash, user_id, created_at, expires_at, last_seen_at, user_agent) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_token_hash(token), user_id, _iso(now),
             _iso(now + timedelta(seconds=ttl_seconds)), _iso(now),
             (user_agent or "")[:256]),
        )
        self.db.conn.commit()
        return token

    def resolve_session(self, token: str) -> Optional[dict]:
        """Return the active, non-expired user for a raw token, or None."""
        if not token:
            return None
        row = self.db.conn.execute(
            "SELECT s.token_hash, s.expires_at, u.* "
            "FROM app_sessions s JOIN app_users u ON u.id = s.user_id "
            "WHERE s.token_hash = ?",
            (_token_hash(token),),
        ).fetchone()
        if row is None:
            return None
        row = dict(row)
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            self.revoke_session(token)
            return None
        if expires <= _now():
            self.revoke_session(token)
            return None
        if not row.get("is_active"):
            return None
        # sliding last_seen (cheap, keeps an idle-tracking signal)
        self.db.conn.execute(
            "UPDATE app_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (_iso(_now()), row["token_hash"]),
        )
        self.db.conn.commit()
        return row

    def revoke_session(self, token: str) -> None:
        self.db.conn.execute(
            "DELETE FROM app_sessions WHERE token_hash = ?",
            (_token_hash(token),),
        )
        self.db.conn.commit()

    def revoke_user_sessions(self, user_id: int) -> None:
        self.db.conn.execute(
            "DELETE FROM app_sessions WHERE user_id = ?", (user_id,)
        )
        self.db.conn.commit()

    def purge_expired_sessions(self) -> int:
        cur = self.db.conn.execute(
            "DELETE FROM app_sessions WHERE expires_at <= ?", (_iso(_now()),)
        )
        self.db.conn.commit()
        return cur.rowcount or 0