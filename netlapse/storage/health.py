"""
Template database health checks.

The parse engine fails to initialize for several distinct reasons, but the
API historically reported all of them as "tfsm_templates.db not found". This
module produces a *truthful* structured verdict so callers can tell the
operator what is actually wrong — most importantly, the difference between a
genuinely-missing file and a present-but-unopenable one (stale WAL/journal
sidecars left by an interrupted write, a malformed image, or a permission
problem).

Nothing here is destructive. diagnose() is read-only. The only state-changing
helper, checkpoint_and_clear(), opens the db cleanly (which makes SQLite
auto-recover a hot journal) and truncates the WAL — the same recovery
dbrepair.py performs — and is opt-in.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass
class TemplateDbHealth:
    """Structured verdict for a template database path."""
    path: str
    exists: bool
    integrity: Optional[str] = None          # "ok", a sqlite verdict, or None
    error: Optional[str] = None              # exception text if open failed
    sidecars: List[str] = field(default_factory=list)  # stale -wal/-shm/-journal

    @property
    def usable(self) -> bool:
        return self.exists and self.integrity == "ok" and self.error is None

    def reason(self) -> str:
        """One-line, operator-facing explanation of why the db is unusable."""
        if not self.exists:
            return (f"{Path(self.path).name} not found — place a template "
                    f"database at {self.path} to enable parsing")
        if self.sidecars and (self.error or self.integrity not in (None, "ok")):
            names = ", ".join(self.sidecars)
            return (f"{Path(self.path).name} is present but unopenable. "
                    f"Stale SQLite sidecars are likely being replayed onto it: "
                    f"{names}. Stop the daemon, delete those sidecar files "
                    f"(NOT the .db), verify with "
                    f"'sqlite3 {self.path} \"PRAGMA integrity_check;\"', "
                    f"then reload.")
        if self.error:
            return (f"{Path(self.path).name} is present but could not be "
                    f"opened: {self.error}")
        if self.integrity and self.integrity != "ok":
            return (f"{Path(self.path).name} is present but failed "
                    f"integrity_check: {self.integrity}")
        return f"{Path(self.path).name} is healthy"


def diagnose(db_path: str) -> TemplateDbHealth:
    """Read-only health verdict for a template db path. Never raises."""
    p = Path(db_path).expanduser()
    h = TemplateDbHealth(path=str(p), exists=p.exists())

    for suffix in SIDECAR_SUFFIXES:
        side = p.with_name(p.name + suffix)
        if side.exists():
            h.sidecars.append(side.name)

    if not h.exists:
        return h

    try:
        # A read-only connection is enough for quick_check and avoids taking
        # a write lock; opening at all is what would replay a hot journal,
        # so we deliberately use immutable=1 to inspect without recovering.
        uri = f"file:{p}?immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            h.integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        h.error = str(e)
    except Exception as e:  # pragma: no cover - defensive
        h.error = str(e)

    return h


def checkpoint_and_clear(db_path: str) -> Optional[str]:
    """
    Open the db cleanly and TRUNCATE the WAL. Opening auto-recovers a hot
    rollback journal; the checkpoint flushes/clears a WAL. Returns None on
    success or an error string. Safe to call at startup before constructing
    the ParseEngine.
    """
    p = Path(db_path).expanduser()
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(str(p), timeout=10)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.commit()
        finally:
            conn.close()
        return None
    except sqlite3.DatabaseError as e:
        return str(e)


def DEFAULT_DB_PATHS_STR() -> Optional[str]:
    """
    Resolve the same default search order ParseEngine uses, returning the
    first path that exists, or the first default (for a truthful 'not found'
    message) if none exist. Kept here so app.py can recover/diagnose the db
    by name before constructing the engine.
    """
    from netlapse.parser.engine import DEFAULT_DB_PATHS
    resolved = [Path(p).expanduser() for p in DEFAULT_DB_PATHS]
    for r in resolved:
        if r.exists():
            return str(r)
    return str(resolved[0]) if resolved else None