"""
Read a capture as it stood at a point in time.

Both backends only write a new version when the raw text changes, so the
state at time T is the newest version that *started* at or before T:

  git   one commit lookup per request (``git rev-list -1 --before=T HEAD``),
        then a tree read per device. All devices share one repo, so the tree
        at that commit is the whole fleet at T.
  file  per device: the current file if its mtime <= T, otherwise the newest
        _history/{capture}.{ts}.* with ts <= T. History is capped at
        max_versions, so far enough back a device simply has no answer.

With as_of=None everything reads the working tree directly, which is the
same thing the rest of netlapse treats as "latest".

All datetimes are naive UTC, matching the storage layer.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from netlapse.storage.backend import StorageBackend

logger = logging.getLogger(__name__)

_TS_FMT = "%Y%m%dT%H%M%SZ"


def parse_as_of(value: Optional[str]) -> Optional[datetime]:
    """ISO-8601 (with or without offset/Z) or YYYYMMDDTHHMMSSZ → naive UTC."""
    if value is None:
        return None
    v = str(value).strip()
    if not v or v.lower() in ("latest", "now", "current"):
        return None
    try:
        return datetime.strptime(v, _TS_FMT)
    except ValueError:
        pass
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class CaptureReader:
    """Point-in-time reader over a storage backend. One per request."""

    def __init__(self, storage: StorageBackend, as_of: Optional[datetime] = None):
        self.storage = storage
        self.as_of = as_of
        self._kind = type(storage).__name__
        self._commit = None
        self._commit_resolved = False

    # ── public ──────────────────────────────────────────────────────

    def parsed(self, site: str, device: str, capture_type: str) -> Tuple[Optional[dict], Optional[datetime]]:
        text, when = self._read(site, device, f"{capture_type}.json")
        if text is None:
            return None, when
        try:
            env = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Corrupt JSON for %s/%s/%s", site, device, capture_type)
            return None, when
        if isinstance(env, dict) and env.get("collected_at"):
            try:
                when = parse_as_of(env["collected_at"]) or when
            except ValueError:
                pass
        return env, when

    def raw(self, site: str, device: str, capture_type: str) -> Tuple[Optional[str], Optional[datetime]]:
        return self._read(site, device, f"{capture_type}.txt")

    def has(self, site: str, device: str, capture_type: str) -> bool:
        if self.as_of is None:
            return (self._device_dir(site, device) / f"{capture_type}.txt").exists()
        text, _ = self._read(site, device, f"{capture_type}.txt")
        return text is not None

    def list_captures(self, site: str, device: str) -> list:
        """Capture types this device has at this time (raw .txt present)."""
        types = set()
        ddir = self._device_dir(site, device)
        if self.as_of is None:
            if ddir.exists():
                types = {p.stem for p in ddir.glob("*.txt")}
        elif self._kind == "GitBackend":
            commit = self._git_commit()
            if commit is not None:
                try:
                    tree = commit.tree / f"{site}/{device}"
                    types = {b.name[:-4] for b in tree.blobs if b.name.endswith(".txt")}
                except KeyError:
                    types = set()
        else:
            cands = {p.stem for p in ddir.glob("*.txt")} if ddir.exists() else set()
            hdir = ddir / "_history"
            if hdir.exists():
                for f in hdir.glob("*.txt"):
                    cands.add(f.name.split(".", 1)[0])
            types = {t for t in cands if self.has(site, device, t)}
        return sorted(types)

    @property
    def resolved_version(self) -> Optional[str]:
        """Git: the commit the as-of resolved to. None otherwise."""
        if self._kind == "GitBackend" and self.as_of is not None:
            c = self._git_commit()
            return str(c) if c is not None else None
        return None

    # ── internals ───────────────────────────────────────────────────

    def _device_dir(self, site: str, device: str) -> Path:
        return self.storage.base_path / site / device

    def _read(self, site: str, device: str, filename: str) -> Tuple[Optional[str], Optional[datetime]]:
        if self.as_of is None:
            return self._read_working(site, device, filename)
        if self._kind == "GitBackend":
            return self._read_git(site, device, filename)
        return self._read_file_history(site, device, filename)

    def _read_working(self, site, device, filename):
        p = self._device_dir(site, device) / filename
        if not p.exists():
            return None, None
        try:
            return (p.read_text(encoding="utf-8", errors="replace"),
                    datetime.utcfromtimestamp(p.stat().st_mtime))
        except OSError:
            return None, None

    def _git_commit(self):
        if self._commit_resolved:
            return self._commit
        self._commit_resolved = True
        try:
            repo = self.storage.repo
            before = self.as_of.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            self._commit = next(iter(repo.iter_commits("HEAD", before=before, max_count=1)), None)
        except Exception as e:  # empty repo, no HEAD yet
            logger.info("No git commit at or before %s: %s", self.as_of, e)
            self._commit = None
        return self._commit

    def _read_git(self, site, device, filename):
        commit = self._git_commit()
        if commit is None:
            return None, None
        rel = f"{site}/{device}/{filename}"
        try:
            blob = commit.tree / rel
        except KeyError:
            return None, None
        text = blob.data_stream.read().decode("utf-8", errors="replace")
        # The commit is the fleet state at T, not when this file last changed;
        # the parsed envelope's collected_at refines it for .json reads.
        return text, datetime.utcfromtimestamp(commit.committed_date)

    def _read_file_history(self, site, device, filename):
        stem, ext = filename.rsplit(".", 1)
        ddir = self._device_dir(site, device)
        raw_current = ddir / f"{stem}.txt"
        if raw_current.exists():
            started = datetime.utcfromtimestamp(raw_current.stat().st_mtime)
            if started <= self.as_of:
                p = ddir / filename
                if p.exists():
                    return p.read_text(encoding="utf-8", errors="replace"), started
                return None, started
        hdir = ddir / "_history"
        if not hdir.exists():
            return None, None
        best = None
        for f in hdir.iterdir():
            name = f.name
            if not name.startswith(stem + ".") or not name.endswith(".txt"):
                continue
            ts = name[len(stem) + 1:-4]
            try:
                when = datetime.strptime(ts, _TS_FMT)
            except ValueError:
                continue
            if when <= self.as_of and (best is None or when > best[0]):
                best = (when, ts)
        if best is None:
            return None, None
        p = hdir / f"{stem}.{best[1]}.{ext}"
        if not p.exists():
            return None, best[0]
        return p.read_text(encoding="utf-8", errors="replace"), best[0]
