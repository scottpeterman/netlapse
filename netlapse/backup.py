"""
Netlapse backup / restore — snapshot and restore the deployed state.

What "deployed state" is
------------------------
Everything that makes one install behave the way it does, resolved from the
live config.yaml exactly the way the daemon resolves it:

  config.yaml                    $NETLAPSE_CONFIG or ~/.netlapse/config.yaml
  definitions                    captures.yaml, jobs.yaml, roles.yaml,
                                 volatility.yaml in the definitions dir
  netlapse.db                    config `dcim_db` — devices, jobs, history,
                                 users and sessions
  vault.db                       ~/.netlapse/vault.db — credentials, still
                                 encrypted under the vault master password
  tfsm_templates.db              config `parser.db_path` or
                                 ~/.netlapse/tfsm_templates.db (Template Lab
                                 edits live here)
  data/  (optional)              config `storage.path` — collected snapshots

Data scope: "none" (default) leaves snapshots out; "current" adds the current
snapshot files but not _history/ (file backend only); "all" adds everything,
including history or the git repository.

Archive layout (tar.gz):
    manifest.json          first member, so listing/inspecting is cheap
    state/config.yaml
    state/definitions/*.yaml
    state/netlapse.db      consistent online copy via the SQLite backup API
    state/vault.db         (single file — no -wal/-shm)
    state/tfsm_templates.db
    data/...               relative to storage.path

Restore is two-phase because the SQLite files cannot be swapped under a
running daemon (and in a container the CLI cannot stop the daemon):

  stage   validate the archive (member paths, sha256 of every state file,
          SQLite integrity), extract it into <config dir>/.restore-staging,
          and write <config dir>/.restore-pending.json.
  apply   at the next daemon start (or immediately from the CLI when no
          daemon is running): take a state-only "pre-restore" backup of what
          is there now, park the current files, move the staged ones into
          place, and roll the parked files back if any step fails.

A restore reproduces the archived state exactly: definition files that exist
now but were not in the archive are removed, and when the archive carries
data the current data directory is replaced (it is kept beside the original
as <data>.pre-restore-<timestamp> — delete it once you are satisfied).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import yaml

from netlapse import __version__

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
DATA_SCOPES = ("none", "current", "all")
DEFINITION_NAMES = ("captures.yaml", "jobs.yaml", "roles.yaml", "volatility.yaml")
ARCHIVE_RE = re.compile(r"^[A-Za-z0-9._-]+\.tar\.gz$")

PENDING_MARKER = ".restore-pending.json"
FAILED_MARKER = ".restore-failed.json"
LAST_RESTORE = ".restore-last.json"
STAGING_DIR = ".restore-staging"
PIDFILE = "netlapse.pid"

Progress = Optional[Callable[[str], None]]


class BackupError(Exception):
    """A backup or restore could not be completed; message is operator-facing."""


# ═══════════════════════════════════════════════════════════════════════════
# Path resolution
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class StatePaths:
    config_path: Path
    definitions_dir: Path
    dcim_db: Path
    vault_db: Path
    templates_db: Path
    data_dir: Path
    backups_dir: Path
    storage_backend: str

    @property
    def config_dir(self) -> Path:
        return self.config_path.parent

    def as_dict(self) -> Dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}


def default_config_path() -> Path:
    return Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def resolve_state_paths(
    config_path: Optional[Path] = None,
    config: Optional[dict] = None,
) -> StatePaths:
    """Resolve every state location the way the daemon does.

    `config` overrides reading config.yaml — used at apply time to place files
    where the *restored* config says they live.
    """
    cfg_path = Path(config_path).expanduser() if config_path else default_config_path()
    cfg = config if config is not None else _load_yaml(cfg_path)

    # Same rule as registry.service.resolve_definitions_dir: explicit
    # definitions_dir, else the directory config.yaml lives in.
    defs_dir = (
        Path(cfg["definitions_dir"]).expanduser()
        if cfg.get("definitions_dir") else cfg_path.parent
    )

    parser_cfg = cfg.get("parser") or {}
    storage_cfg = cfg.get("storage") or {}
    backup_cfg = cfg.get("backup") or {}

    return StatePaths(
        config_path=cfg_path,
        definitions_dir=defs_dir,
        dcim_db=Path(cfg.get("dcim_db") or "~/.netlapse/netlapse.db").expanduser(),
        # CredentialVault has no config override — always ~/.netlapse/vault.db.
        vault_db=Path.home() / ".netlapse" / "vault.db",
        templates_db=Path(
            parser_cfg.get("db_path") or "~/.netlapse/tfsm_templates.db"
        ).expanduser(),
        data_dir=Path(storage_cfg.get("path") or "~/.netlapse/data").expanduser(),
        backups_dir=Path(
            backup_cfg.get("dir") or (cfg_path.parent / "backups")
        ).expanduser(),
        storage_backend=str(storage_cfg.get("backend") or "file"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Daemon liveness (pidfile)
# ═══════════════════════════════════════════════════════════════════════════

def write_pidfile(config_dir: Path) -> None:
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / PIDFILE).write_text(str(os.getpid()), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write pidfile: %s", e)


def remove_pidfile(config_dir: Path) -> None:
    p = config_dir / PIDFILE
    try:
        if p.exists() and p.read_text().strip() == str(os.getpid()):
            p.unlink()
    except OSError:
        pass


def _port_open(listen: Optional[dict]) -> bool:
    """True if something accepts TCP on the configured listen address."""
    listen = listen or {}
    host = str(listen.get("host") or "127.0.0.1")
    if host in ("0.0.0.0", "::", "[::]", ""):
        host = "127.0.0.1"
    try:
        port = int(listen.get("port") or 8888)
    except (TypeError, ValueError):
        port = 8888
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def daemon_running(config_dir: Path, listen: Optional[dict] = None) -> bool:
    """
    True if a daemon appears to be running: the pidfile names a live process,
    OR something answers on the configured listen port. The port probe
    covers a daemon started before pidfiles existed (or with an unwritable
    config dir). Errs toward True — a false positive only means a restore is
    staged instead of applied immediately.
    """
    p = config_dir / PIDFILE
    try:
        pid = int(p.read_text().strip())
        if pid == os.getpid():
            return True
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        pass        # no pidfile, unreadable, or process gone
    return _port_open(listen)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite DB (WAL included) into one file."""
    s = sqlite3.connect(str(src), timeout=30)
    try:
        d = sqlite3.connect(str(dst))
        try:
            s.backup(d)
            d.execute("PRAGMA journal_mode=DELETE")
            d.commit()
        finally:
            d.close()
    finally:
        s.close()


def _sqlite_check(path: Path, quick: bool = False) -> Optional[str]:
    """None if the DB is sound, else the problem."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            pragma = "quick_check" if quick else "integrity_check"
            rows = conn.execute(f"PRAGMA {pragma}").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return str(e)
    result = ", ".join(str(r[0]) for r in rows[:5])
    return None if result == "ok" else result


def _iter_data_files(data_dir: Path, scope: str, exclude: List[Path]) -> Iterator[Path]:
    excl = {p.resolve() for p in exclude if p.exists()}
    for root, dirs, files in os.walk(data_dir):
        rootp = Path(root)
        dirs[:] = sorted(
            d for d in dirs
            if (rootp / d).resolve() not in excl
            and not (scope == "current" and d == "_history")
            and not (rootp / d).is_symlink()
        )
        for f in sorted(files):
            p = rootp / f
            if f.endswith(".partial") or p.is_symlink():
                continue
            yield p


def _reset_owner(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ═══════════════════════════════════════════════════════════════════════════
# Create
# ═══════════════════════════════════════════════════════════════════════════

def create_backup(
    paths: StatePaths,
    data_scope: str = "none",
    label: Optional[str] = None,
    dest_dir: Optional[Path] = None,
    progress: Progress = None,
) -> Dict[str, Any]:
    """
    Write a backup archive. Safe while the daemon is running: SQLite files are
    copied with the online backup API, everything else is read-only.

    Returns {"path", "name", "size", "manifest"}.
    """
    if data_scope not in DATA_SCOPES:
        raise BackupError(f"data scope must be one of {', '.join(DATA_SCOPES)}")
    if data_scope == "current" and paths.storage_backend == "git":
        raise BackupError(
            "the git storage backend keeps history inside .git — "
            "use data scope 'all' or 'none'"
        )
    if not paths.config_path.exists():
        raise BackupError(f"no config.yaml at {paths.config_path}")

    say = progress or (lambda _m: None)
    dest = Path(dest_dir).expanduser() if dest_dir else paths.backups_dir
    dest.mkdir(parents=True, exist_ok=True)

    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") if label else ""
    name = f"netlapse-{_utc_stamp()}{('-' + safe_label) if safe_label else ''}-{data_scope}.tar.gz"
    final = dest / name
    partial = dest / (name + ".partial")

    with tempfile.TemporaryDirectory(prefix="netlapse-backup-") as tmp:
        tmpd = Path(tmp)
        (tmpd / "definitions").mkdir()
        members: List[tuple] = []          # (source path, arcname)
        components: Dict[str, Any] = {}

        # ── state ────────────────────────────────────────────────────
        say("copying config")
        shutil.copy2(paths.config_path, tmpd / "config.yaml")
        members.append((tmpd / "config.yaml", "state/config.yaml"))

        defs = []
        for n in DEFINITION_NAMES:
            src = paths.definitions_dir / n
            if src.is_file() and src.resolve() != paths.config_path.resolve():
                shutil.copy2(src, tmpd / "definitions" / n)
                members.append((tmpd / "definitions" / n, f"state/definitions/{n}"))
                defs.append(n)
        components["definitions"] = defs

        for key, src in (
            ("netlapse.db", paths.dcim_db),
            ("vault.db", paths.vault_db),
            ("tfsm_templates.db", paths.templates_db),
        ):
            if src.is_file():
                say(f"snapshotting {key}")
                _sqlite_snapshot(src, tmpd / key)
                members.append((tmpd / key, f"state/{key}"))
                components[key] = True
            else:
                components[key] = False
        if not components["netlapse.db"]:
            raise BackupError(f"no netlapse.db at {paths.dcim_db}")

        hashes = {arc: _sha256(src) for src, arc in members}

        # ── data inventory (walk once; counts go into the manifest) ──
        data_files: List[Path] = []
        data_bytes = 0
        if data_scope != "none":
            if not paths.data_dir.is_dir():
                raise BackupError(f"data directory {paths.data_dir} does not exist")
            say("scanning data directory")
            for p in _iter_data_files(
                paths.data_dir, data_scope, [paths.backups_dir, dest]
            ):
                data_files.append(p)
                data_bytes += p.stat().st_size

        manifest = {
            "format": FORMAT_VERSION,
            "netlapse_version": __version__,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "host": socket.gethostname(),
            "label": label or None,
            "data_scope": data_scope,
            "storage_backend": paths.storage_backend,
            "components": components,
            "sha256": hashes,
            "data": {"files": len(data_files), "bytes": data_bytes},
            "source_paths": paths.as_dict(),
        }
        (tmpd / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # ── write archive (manifest first) ───────────────────────────
        try:
            with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
                tar.add(tmpd / "manifest.json", arcname="manifest.json",
                        filter=_reset_owner)
                for src, arc in members:
                    tar.add(src, arcname=arc, filter=_reset_owner)
                total = len(data_files)
                for i, p in enumerate(data_files, 1):
                    rel = p.relative_to(paths.data_dir).as_posix()
                    tar.add(p, arcname=f"data/{rel}", recursive=False,
                            filter=_reset_owner)
                    if i % 2000 == 0 or i == total:
                        say(f"archiving data {i}/{total} files")
            os.replace(partial, final)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    size = final.stat().st_size
    logger.info(
        "Backup written: %s (%s, data=%s, %d data files)",
        final, _human(size), data_scope, len(data_files),
    )
    return {"path": str(final), "name": name, "size": size, "manifest": manifest}


# ═══════════════════════════════════════════════════════════════════════════
# Inspect / list
# ═══════════════════════════════════════════════════════════════════════════

def read_manifest(archive: Path) -> Dict[str, Any]:
    """Read manifest.json (the first member) without scanning the archive."""
    try:
        with tarfile.open(archive, "r|gz") as tar:
            first = next(iter(tar), None)
            if first is None or first.name != "manifest.json":
                raise BackupError("not a netlapse backup (no leading manifest.json)")
            f = tar.extractfile(first)
            manifest = json.loads(f.read().decode("utf-8"))
    except (tarfile.TarError, OSError, EOFError, json.JSONDecodeError) as e:
        raise BackupError(f"unreadable archive: {e}") from None
    if manifest.get("format") != FORMAT_VERSION:
        raise BackupError(
            f"unsupported backup format {manifest.get('format')!r} "
            f"(this netlapse reads format {FORMAT_VERSION})"
        )
    return manifest


def list_backups(backups_dir: Path) -> List[Dict[str, Any]]:
    out = []
    if not backups_dir.is_dir():
        return out
    for p in sorted(backups_dir.glob("*.tar.gz"), reverse=True):
        entry = {"name": p.name, "size": p.stat().st_size,
                 "modified": datetime.fromtimestamp(
                     p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
        try:
            m = read_manifest(p)
            entry.update({
                "created_at": m.get("created_at"),
                "netlapse_version": m.get("netlapse_version"),
                "host": m.get("host"),
                "label": m.get("label"),
                "data_scope": m.get("data_scope"),
                "data_files": (m.get("data") or {}).get("files"),
            })
        except BackupError as e:
            entry["error"] = str(e)
        out.append(entry)
    return out


def resolve_archive(backups_dir: Path, name_or_path: str) -> Path:
    """Accept a bare archive name (looked up in backups_dir) or a path."""
    p = Path(name_or_path).expanduser()
    if p.is_file():
        return p
    if ARCHIVE_RE.match(name_or_path) and (backups_dir / name_or_path).is_file():
        return backups_dir / name_or_path
    raise BackupError(f"backup not found: {name_or_path}")


# ═══════════════════════════════════════════════════════════════════════════
# Stage
# ═══════════════════════════════════════════════════════════════════════════

def _safe_members(tar: tarfile.TarFile) -> Iterator[tarfile.TarInfo]:
    for m in tar:
        name = m.name
        parts = Path(name).parts
        if (name.startswith("/") or ".." in parts or not parts
                or parts[0] not in ("manifest.json", "state", "data")):
            raise BackupError(f"unsafe or unexpected archive member: {name!r}")
        if not (m.isfile() or m.isdir()):
            raise BackupError(f"archive member is not a regular file: {name!r}")
        m.uid, m.gid = os.getuid(), os.getgid()
        m.uname = m.gname = ""
        m.mode = (m.mode or 0o644) & 0o755 if m.isdir() else (m.mode or 0o644) & 0o644
        yield m


def pending_restore(config_dir: Path) -> Optional[Dict[str, Any]]:
    p = config_dir / PENDING_MARKER
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"error": "pending marker unreadable"}


def last_restore(config_dir: Path) -> Optional[Dict[str, Any]]:
    for name in (FAILED_MARKER, LAST_RESTORE):
        p = config_dir / name
        if p.exists():
            try:
                d = json.loads(p.read_text())
                d["failed"] = name == FAILED_MARKER
                return d
            except (OSError, json.JSONDecodeError):
                pass
    return None


def cancel_restore(config_dir: Path) -> bool:
    had = (config_dir / PENDING_MARKER).exists()
    (config_dir / PENDING_MARKER).unlink(missing_ok=True)
    shutil.rmtree(config_dir / STAGING_DIR, ignore_errors=True)
    return had


def stage_restore(
    archive: Path,
    paths: StatePaths,
    progress: Progress = None,
) -> Dict[str, Any]:
    """Validate and extract `archive` into the staging area; mark it pending."""
    say = progress or (lambda _m: None)
    archive = Path(archive)
    manifest = read_manifest(archive)
    cfg_dir = paths.config_dir
    staging = cfg_dir / STAGING_DIR
    cancel_restore(cfg_dir)                      # one pending restore at a time
    staging.mkdir(parents=True)

    try:
        say("extracting archive")
        with tarfile.open(archive, "r:gz") as tar:
            members = list(_safe_members(tar))
            try:
                tar.extractall(staging, members=members, filter="fully_trusted")
            except TypeError:                     # Python without extraction filters
                tar.extractall(staging, members=members)

        say("verifying checksums")
        for arc, want in (manifest.get("sha256") or {}).items():
            f = staging / arc
            if not f.is_file():
                raise BackupError(f"archive is missing {arc}")
            if _sha256(f) != want:
                raise BackupError(f"checksum mismatch for {arc} — archive is damaged")
        if not (staging / "state" / "config.yaml").is_file():
            raise BackupError("archive has no state/config.yaml")
        if not (staging / "state" / "netlapse.db").is_file():
            raise BackupError("archive has no state/netlapse.db")

        say("checking databases")
        for db in ("netlapse.db", "vault.db", "tfsm_templates.db"):
            f = staging / "state" / db
            if f.is_file():
                problem = _sqlite_check(f, quick=(db == "tfsm_templates.db"))
                if problem:
                    raise BackupError(f"{db} in archive failed integrity check: {problem}")

        data_expected = (manifest.get("data") or {}).get("files") or 0
        if manifest.get("data_scope") != "none":
            got = sum(1 for _ in (staging / "data").rglob("*") if _.is_file()) \
                if (staging / "data").is_dir() else 0
            if got != data_expected:
                raise BackupError(
                    f"archive data is incomplete: {got} of {data_expected} files"
                )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    vault_check = _check_vault_password(staging / "state" / "vault.db")
    pending = {
        "archive": str(archive),
        "archive_name": archive.name,
        "staged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "staging": str(staging),
        "manifest": manifest,
        "vault_password": vault_check,
    }
    (cfg_dir / PENDING_MARKER).write_text(json.dumps(pending, indent=2))
    (cfg_dir / FAILED_MARKER).unlink(missing_ok=True)
    logger.info("Restore staged from %s (vault password: %s)", archive.name, vault_check)
    return pending


def _check_vault_password(vault_file: Path) -> str:
    """'ok' / 'mismatch' against $NETLAPSE_VAULT_PASSWORD, or 'unchecked'."""
    password = os.environ.get("NETLAPSE_VAULT_PASSWORD")
    if not password or not vault_file.is_file():
        return "unchecked"
    try:
        from netlapse.vault.vault import CredentialVault
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "vault.db"
            shutil.copy2(vault_file, probe)
            v = CredentialVault(probe)
            try:
                return "ok" if v.unlock(password) else "mismatch"
            except Exception:
                return "mismatch"
    except Exception as e:
        logger.debug("vault probe failed: %s", e)
        return "unchecked"


# ═══════════════════════════════════════════════════════════════════════════
# Apply
# ═══════════════════════════════════════════════════════════════════════════

def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))       # crosses filesystems


def apply_pending_restore(config_path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    Apply a staged restore. Call before anything opens the databases (daemon
    startup does this first). No-op when nothing is pending.

    Returns the result record, or None if there was nothing to apply.
    """
    cfg_path = Path(config_path).expanduser() if config_path else default_config_path()
    cfg_dir = cfg_path.parent
    pending = pending_restore(cfg_dir)
    if not pending:
        return None

    stamp = _utc_stamp()
    staging = cfg_dir / STAGING_DIR
    manifest = pending.get("manifest") or {}
    result: Dict[str, Any] = {
        "archive": pending.get("archive_name"),
        "applied_at": None,
        "data_scope": manifest.get("data_scope"),
    }

    def fail(msg: str) -> Dict[str, Any]:
        result["error"] = msg
        result["failed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (cfg_dir / FAILED_MARKER).write_text(json.dumps(result, indent=2))
        (cfg_dir / PENDING_MARKER).unlink(missing_ok=True)
        logger.error("Restore from %s FAILED: %s", result["archive"], msg)
        return result

    if not (staging / "state" / "config.yaml").is_file():
        return fail(f"staging area {staging} is missing or incomplete; re-stage the restore")

    logger.warning("Applying staged restore from %s", result["archive"])
    current = resolve_state_paths(cfg_path)

    # 1. Durable undo: state-only backup of what is here now.
    try:
        if current.config_path.exists() and current.dcim_db.exists():
            pre = create_backup(current, "none", label="pre-restore")
            result["pre_restore_backup"] = pre["name"]
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        return fail(f"could not write the pre-restore safety backup ({e}); nothing was changed")

    # 2. Where the restored config says things live.
    new_cfg = _load_yaml(staging / "state" / "config.yaml")
    target = resolve_state_paths(cfg_path, new_cfg)

    plan: List[tuple] = [(staging / "state" / "config.yaml", cfg_path)]
    restored_defs = set()
    for n in DEFINITION_NAMES:
        src = staging / "state" / "definitions" / n
        if src.is_file():
            plan.append((src, target.definitions_dir / n))
            restored_defs.add(n)
    db_targets = {
        "netlapse.db": target.dcim_db,
        "vault.db": target.vault_db,
        "tfsm_templates.db": target.templates_db,
    }
    for name, dst in db_targets.items():
        src = staging / "state" / name
        if src.is_file():
            plan.append((src, dst))
    # Definition files present now but absent from the archive are removed.
    removals = [
        target.definitions_dir / n for n in DEFINITION_NAMES
        if n not in restored_defs and (target.definitions_dir / n).is_file()
        and (target.definitions_dir / n).resolve() != cfg_path.resolve()
    ]

    park_dir = cfg_dir / f".restore-rollback-{stamp}"
    parked: List[tuple] = []        # (parked path, original path)
    placed: List[Path] = []
    data_parked: Optional[Path] = None

    def park(p: Path) -> None:
        if p.exists():
            dst = park_dir / f"{len(parked):03d}-{p.name}"
            _move(p, dst)
            parked.append((dst, p))

    try:
        for _src, dst in plan:
            park(dst)
            if dst.suffix == ".db":
                # A stale -wal/-shm from the old DB would be replayed into the
                # restored one on open — park them with it.
                for side in (Path(str(dst) + "-wal"), Path(str(dst) + "-shm")):
                    park(side)
        for p in removals:
            park(p)
        for src, dst in plan:
            _move(src, dst)
            placed.append(dst)

        if manifest.get("data_scope") not in (None, "none"):
            staged_data = staging / "data"
            if target.data_dir.exists():
                data_parked = target.data_dir.with_name(
                    f"{target.data_dir.name}.pre-restore-{stamp}")
                _move(target.data_dir, data_parked)
            if staged_data.is_dir():
                _move(staged_data, target.data_dir)
            else:
                target.data_dir.mkdir(parents=True, exist_ok=True)
            placed.append(target.data_dir)
    except Exception as e:
        logger.exception("Restore apply failed — rolling back")
        for p in placed:
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        if data_parked and data_parked.exists():
            _move(data_parked, target.data_dir)
        for parked_path, original in reversed(parked):
            try:
                _move(parked_path, original)
            except OSError:
                logger.error("Rollback could not return %s to %s", parked_path, original)
        shutil.rmtree(park_dir, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        return fail(f"{e} — previous state was put back")

    shutil.rmtree(park_dir, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    (cfg_dir / PENDING_MARKER).unlink(missing_ok=True)
    (cfg_dir / FAILED_MARKER).unlink(missing_ok=True)

    result["applied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result["vault_password"] = pending.get("vault_password")
    if data_parked:
        result["previous_data_dir"] = str(data_parked)
    (cfg_dir / LAST_RESTORE).write_text(json.dumps(result, indent=2))
    try:
        target.backups_dir.mkdir(parents=True, exist_ok=True)
        with open(target.backups_dir / "restore-history.jsonl", "a") as f:
            f.write(json.dumps(result) + "\n")
    except OSError:
        pass
    logger.warning(
        "Restore applied from %s (pre-restore backup: %s%s)",
        result["archive"], result.get("pre_restore_backup", "none"),
        f"; previous data kept at {data_parked}" if data_parked else "",
    )
    return result
