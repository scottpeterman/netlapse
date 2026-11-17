"""
File Storage Backend

Simple directory-based storage with no external dependencies.
Writes raw text and parsed JSON to a site/device/ tree.
Keeps the last N versions by timestamping previous captures.

Directory layout (identical to git backend's working tree):

  {base_path}/
    site1/
      border-rtr-01/
        config.txt          ← current (latest)
        config.json
        ospf.txt
        ospf.json
        _history/
          config.20260524T149000Z.txt   ← previous versions
          config.20260524T149000Z.json
          ospf.20260524T149000Z.txt
          ospf.20260524T149000Z.json

The "current" files are always at the top level for fast reads.
The _history/ dir holds rotated copies with timestamps embedded.
When max_versions is reached, oldest are deleted.

This backend supports:
  - store / store_batch
  - fetch_latest / fetch_latest_raw
  - fetch_version (by timestamp ID, or "current")
  - search_configs (scans current .txt files)
  - list_versions (device-level, across all capture types)
  - diff (STRUCTURED): current vs newest _history snapshot for a capture
    type, using the shared backend-agnostic engine in storage/diff.py

The structured diff is NOT a git feature. It is record comparison over the
parsed JSON this backend already versions in _history/. The only thing the
git backend adds on top is real commit history (blame, arbitrary-pair
diffs, trailers); for "what changed since last collection" the file backend
is now fully capable with zero dependencies.
"""

import difflib
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple

from netlapse.storage.diff import DIFF_KEY_FIELDS, is_record_capture
from netlapse.storage.backend import (
    StorageBackend,
    Snapshot,
    StoredVersion,
    DiffResult,
    FleetChange,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_VERSIONS = 50


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """
    Crash-safe replacement for Path.write_text().

    Path.write_text opens with mode 'w', which truncates the target up
    front: a process killed mid-write (e.g. an interrupted reparse) leaves
    an empty or half-written file. This writes to a temp file in the same
    directory, fsyncs it, then os.replace()s it into place — an atomic
    rename on POSIX and Windows — so a reader/restart sees either the old
    complete file or the new complete file, never a torn one.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# Matches a rotated history file: {capture_type}.{timestamp}.{ext}
# capture_type is non-greedy so the rigid timestamp anchors the split;
# this correctly handles multi-word types (e.g. lldp_neighbors) that the
# previous [a-z]+ pattern silently dropped.
_HISTORY_RE = re.compile(
    r"^(?P<type>.+?)\.(?P<ts>\d{8}T\d{6}Z)\.(?P<ext>txt|json)$"
)


class FileBackend(StorageBackend):
    """
    File-based storage backend.

    Zero dependencies beyond the standard library.
    Good for evaluation, small deployments, or environments
    where git isn't available.
    """

    def __init__(self, base_path: str | Path, max_versions: int = DEFAULT_MAX_VERSIONS):
        super().__init__(base_path)
        self.max_versions = max_versions

    def initialize(self) -> None:
        """Create the base directory if it doesn't exist."""
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info("File backend initialized at %s", self.base_path)

    def store(self, snapshot: Snapshot, trigger: str = "scheduled") -> str:
        """
        Store a single snapshot.

        1. Rotate current files to _history/ (if they exist and changed)
        2. Write new current files
        3. Prune _history/ if over max_versions
        """
        device_dir = self._device_dir(snapshot.site, snapshot.device)
        device_dir.mkdir(parents=True, exist_ok=True)

        raw_path = device_dir / self._raw_filename(snapshot.capture_type)
        parsed_path = device_dir / self._parsed_filename(snapshot.capture_type)

        # Check if content actually changed
        if raw_path.exists():
            existing = raw_path.read_text(encoding="utf-8", errors="replace")

            # A large single-cycle shrink is far more often a truncated read
            # (the prompt matched early and the capture ended mid-stream) than
            # a real change of that size. Both lengths are already in hand, so
            # flag it either way rather than storing it silently.
            if existing and len(snapshot.raw_text) < len(existing) * 0.75:
                logger.warning(
                    "%s/%s/%s shrank %d -> %d bytes; possible truncated read",
                    snapshot.site, snapshot.device, snapshot.capture_type,
                    len(existing), len(snapshot.raw_text),
                )

            if existing == snapshot.raw_text:
                # Raw text unchanged, but the PARSE may still have improved —
                # a better-scoring template, a new template in the DB, or a
                # first successful parse. Comparing content rather than mere
                # existence keeps that improvement from being discarded: the
                # old check wrote only when config.json was absent, so a
                # low-quality first parse became permanent.
                if snapshot.parsed_data is not None:
                    envelope = self._build_json_envelope(snapshot)
                    new_json = json.dumps(envelope, indent=2, default=str)
                    current_json = None
                    if parsed_path.exists():
                        current_json = parsed_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    if current_json != new_json:
                        _atomic_write_text(parsed_path, new_json)
                        logger.info(
                            "Updated parsed data for unchanged raw: %s/%s/%s"
                            " (template=%s score=%s)",
                            snapshot.site, snapshot.device,
                            snapshot.capture_type,
                            snapshot.template_name, snapshot.template_score,
                        )

                logger.info(
                    "No change for %s/%s/%s, skipping",
                    snapshot.site, snapshot.device, snapshot.capture_type,
                )
                return self._timestamp_str()

        # Rotate existing to history
        self._rotate_to_history(device_dir, snapshot.capture_type)

        # Write new current
        _atomic_write_text(raw_path, snapshot.raw_text)
        logger.debug("Wrote %s (%d bytes)", raw_path, len(snapshot.raw_text))

        if snapshot.parsed_data is not None:
            envelope = self._build_json_envelope(snapshot)
            _atomic_write_text(
                parsed_path,
                json.dumps(envelope, indent=2, default=str),
            )
            logger.debug("Wrote %s", parsed_path)

        # Prune old history
        self._prune_history(device_dir, snapshot.capture_type)

        ts = self._timestamp_str(snapshot.collected_at)
        logger.info(
            "Stored %s/%s/%s (%s)",
            snapshot.site, snapshot.device, snapshot.capture_type, trigger,
        )
        return ts

    def store_batch(
        self,
        snapshots: List[Snapshot],
        trigger: str = "scheduled",
    ) -> str:
        """Store multiple snapshots for the same device."""
        if not snapshots:
            return self._timestamp_str()

        # Validate all snapshots are for the same device
        devices = {(s.device, s.site) for s in snapshots}
        if len(devices) > 1:
            raise ValueError(
                f"store_batch requires all snapshots from the same device, "
                f"got: {devices}"
            )

        ts = ""
        for snapshot in snapshots:
            ts = self.store(snapshot, trigger=trigger)

        return ts

    def update_parsed(self, snapshot: Snapshot) -> str:
        """
        Overwrite the parsed JSON for the current snapshot in place.

        Raw text is never read, rotated, or touched. No _history/ churn.
        Used by the reparse workflow to regenerate parsed data from
        already-collected raw output after a template change.
        """
        device_dir = self._device_dir(snapshot.site, snapshot.device)
        device_dir.mkdir(parents=True, exist_ok=True)

        parsed_path = device_dir / self._parsed_filename(snapshot.capture_type)
        envelope = self._build_json_envelope(snapshot)
        _atomic_write_text(
            parsed_path,
            json.dumps(envelope, indent=2, default=str),
        )
        logger.info(
            "Reparsed %s/%s/%s → %s (score=%s)",
            snapshot.site, snapshot.device, snapshot.capture_type,
            snapshot.template_name, snapshot.template_score,
        )
        return self._timestamp_str()

    def fetch_latest(
        self,
        device: str,
        site: str,
        capture_type: str = "config",
    ) -> Optional[Snapshot]:
        """Read the current (top-level) files for a device."""
        device_dir = self._device_dir(site, device)
        raw_path = device_dir / self._raw_filename(capture_type)

        if not raw_path.exists():
            return None

        raw_text = raw_path.read_text(encoding="utf-8", errors="replace")

        # Try to load parsed data
        parsed_data = None
        parsed_path = device_dir / self._parsed_filename(capture_type)
        if parsed_path.exists():
            try:
                parsed_data = json.loads(
                    parsed_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError:
                logger.warning("Corrupt JSON at %s", parsed_path)

        return Snapshot(
            device=device,
            site=site,
            capture_type=capture_type,
            command=parsed_data.get("command", "") if parsed_data else "",
            raw_text=raw_text,
            parsed_data=parsed_data,
            collected_at=self._file_mtime(raw_path),
            template_name=(
                parsed_data.get("template") if parsed_data else None
            ),
            template_score=(
                parsed_data.get("template_score") if parsed_data else None
            ),
        )

    def fetch_latest_raw(
        self,
        device: str,
        site: str,
        capture_type: str = "config",
    ) -> Optional[str]:
        """Fast path: read just the raw text file."""
        raw_path = (
            self._device_dir(site, device)
            / self._raw_filename(capture_type)
        )
        if not raw_path.exists():
            return None
        return raw_path.read_text(encoding="utf-8", errors="replace")

    def fetch_version(
        self,
        device: str,
        site: str,
        version_id: str,
        capture_type: str = "config",
    ) -> Optional[Snapshot]:
        """
        Fetch a specific version of a snapshot.

        version_id is either the literal "current" (top-level files) or a
        history timestamp (e.g. "20260524T149000Z"), which reads the
        matching {capture_type}.{ts}.{txt,json} from _history/.

        Returns None if that version of this capture type doesn't exist.
        """
        if version_id == "current":
            return self.fetch_latest(device, site, capture_type)

        history_dir = self._device_dir(site, device) / "_history"
        raw_path = history_dir / f"{capture_type}.{version_id}.txt"
        if not raw_path.exists():
            return None

        raw_text = raw_path.read_text(encoding="utf-8", errors="replace")

        parsed_data = None
        parsed_path = history_dir / f"{capture_type}.{version_id}.json"
        if parsed_path.exists():
            try:
                parsed_data = json.loads(
                    parsed_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError:
                logger.warning("Corrupt JSON at %s", parsed_path)

        try:
            collected_at = datetime.strptime(version_id, "%Y%m%dT%H%M%SZ")
        except ValueError:
            collected_at = self._file_mtime(raw_path)

        return Snapshot(
            device=device,
            site=site,
            capture_type=capture_type,
            command=parsed_data.get("command", "") if parsed_data else "",
            raw_text=raw_text,
            parsed_data=parsed_data,
            collected_at=collected_at,
            template_name=(
                parsed_data.get("template") if parsed_data else None
            ),
            template_score=(
                parsed_data.get("template_score") if parsed_data else None
            ),
        )

    def search_configs(
        self,
        pattern: str,
        site: Optional[str] = None,
        capture_type: str = "config",
    ) -> List[dict]:
        """
        Regex search across all current raw text files for a capture type.

        Scans {base}/{site}/{device}/{capture_type}.txt for each device.
        Returns list of dicts with device, site, and matching lines with
        line numbers and surrounding context.
        """
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            logger.warning("Invalid search pattern '%s': %s", pattern, e)
            return []

        results = []
        filename = self._raw_filename(capture_type)

        # Walk site directories
        if site:
            site_dirs = [self.base_path / site]
        else:
            site_dirs = [
                d for d in self.base_path.iterdir()
                if d.is_dir() and not d.name.startswith("_")
            ]

        for site_dir in site_dirs:
            if not site_dir.exists():
                continue
            for device_dir in sorted(site_dir.iterdir()):
                if not device_dir.is_dir() or device_dir.name.startswith("_"):
                    continue

                target_path = device_dir / filename
                if not target_path.exists():
                    continue

                text = target_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                lines = text.splitlines()
                match_lines = []
                for i, line in enumerate(lines, start=1):
                    if regex.search(line):
                        match_lines.append({
                            "line_number": i,
                            "text": line,
                        })
                        if len(match_lines) >= 50:
                            break

                if match_lines:
                    results.append({
                        "device": device_dir.name,
                        "site": site_dir.name,
                        "matches": match_lines,
                    })

        return results

    def list_capture_types(self) -> List[str]:
        """
        Scan the storage tree and return all distinct capture types
        that have at least one .txt file stored.
        """
        types = set()
        for site_dir in self.base_path.iterdir():
            if not site_dir.is_dir() or site_dir.name.startswith(("_", ".")):
                continue
            for device_dir in site_dir.iterdir():
                if not device_dir.is_dir() or device_dir.name.startswith("_"):
                    continue
                for txt_file in device_dir.glob("*.txt"):
                    types.add(txt_file.stem)
        return sorted(types)

    def list_versions(
        self,
        device: str,
        site: str,
        limit: int = 50,
    ) -> List[StoredVersion]:
        """
        List available versions for a device, newest first.

        Device-level and capture-type-agnostic, matching the git backend's
        semantics: a "version" is a point in time, and each version reports
        which capture types it contains. The current top-level files are
        version_id "current"; rotated snapshots are timestamp IDs.

        Limited compared to git: no commit messages, no author, no trigger
        (those need the git backend). The previous implementation keyed on
        config.txt and therefore showed nothing for devices that only have
        non-config captures (ospf/lldp/interfaces) — that's fixed here.
        """
        device_dir = self._device_dir(site, device)
        versions: List[StoredVersion] = []

        # ── Current state — any top-level capture type present ───────
        if device_dir.exists():
            current_types = sorted(
                p.stem for p in device_dir.glob("*.txt")
            )
            if current_types:
                newest = max(
                    self._file_mtime(device_dir / f"{t}.txt")
                    for t in current_types
                )
                versions.append(StoredVersion(
                    version_id="current",
                    collected_at=newest,
                    message="current version",
                    capture_types=current_types,
                ))

        # ── Historical snapshots grouped by timestamp ────────────────
        history_dir = device_dir / "_history"
        if history_dir.exists():
            groups: dict = {}   # timestamp → set(capture_type)
            for f in history_dir.iterdir():
                m = _HISTORY_RE.match(f.name)
                if m and m.group("ext") == "txt":
                    groups.setdefault(m.group("ts"), set()).add(m.group("type"))

            for ts in sorted(groups.keys(), reverse=True):
                try:
                    dt = datetime.strptime(ts, "%Y%m%dT%H%M%SZ")
                except ValueError:
                    continue
                versions.append(StoredVersion(
                    version_id=ts,
                    collected_at=dt,
                    message=f"historical snapshot {ts}",
                    capture_types=sorted(groups[ts]),
                ))

        return versions[:limit]

    def delete_version(
        self,
        device: str,
        site: str,
        capture_type: str,
        version_id: str,
    ) -> dict:
        """
        Delete one capture's snapshot (raw + parsed) for a device.

        - A history timestamp: the matching _history/{capture}.{ts}.{txt,json}
          files are removed. Nothing else moves.
        - "current": the newest history snapshot is PROMOTED back to the
          top-level current files (so the device keeps a valid current view
          and the bad collection is undone), and that history entry is then
          consumed. With no history to fall back on, current is removed and
          the capture stays empty until the next collection.

        Only touches this backend's tree — DCIM rows and job history are left
        alone, matching delete_device()'s "storage is purged separately" model.

        Returns {device, site, capture_type, deleted, promoted, remaining}.
        Raises FileNotFoundError if the target version doesn't exist.
        """
        device_dir = self._device_dir(site, device)
        history_dir = device_dir / "_history"
        raw_name = self._raw_filename(capture_type)
        parsed_name = self._parsed_filename(capture_type)
        deleted: List[str] = []

        def _unlink(p: Path):
            if p.exists():
                p.unlink()
                deleted.append(p.name)

        if version_id == "current":
            cur_raw = device_dir / raw_name
            cur_parsed = device_dir / parsed_name
            if not cur_raw.exists() and not cur_parsed.exists():
                raise FileNotFoundError(
                    f"no current {capture_type} snapshot for {device}@{site}"
                )

            newest = self._newest_history_ts(history_dir, capture_type)
            if newest:
                # Promote the previous snapshot back into place, then consume
                # its history files (it has become current).
                h_raw = history_dir / f"{capture_type}.{newest}.txt"
                h_parsed = history_dir / f"{capture_type}.{newest}.json"
                _atomic_write_text(cur_raw, h_raw.read_text(encoding="utf-8"))
                if h_parsed.exists():
                    _atomic_write_text(cur_parsed, h_parsed.read_text(encoding="utf-8"))
                elif cur_parsed.exists():
                    cur_parsed.unlink()   # promoted snapshot had no parse
                h_raw.unlink()
                if h_parsed.exists():
                    h_parsed.unlink()
                promoted = newest
                deleted.append(f"current {capture_type} (replaced by {newest})")
            else:
                _unlink(cur_raw)
                _unlink(cur_parsed)
                promoted = None
        else:
            h_raw = history_dir / f"{capture_type}.{version_id}.txt"
            h_parsed = history_dir / f"{capture_type}.{version_id}.json"
            if not h_raw.exists() and not h_parsed.exists():
                raise FileNotFoundError(
                    f"no {capture_type} snapshot {version_id} for {device}@{site}"
                )
            _unlink(h_raw)
            _unlink(h_parsed)
            promoted = None

        remaining = len(self._history_timestamps(history_dir, capture_type))
        if (device_dir / raw_name).exists():
            remaining += 1   # count the current snapshot too

        logger.info(
            "Deleted %s snapshot %s for %s@%s%s",
            capture_type, version_id, device, site,
            f" (promoted {promoted})" if promoted else "",
        )
        return {
            "device": device,
            "site": site,
            "capture_type": capture_type,
            "deleted": deleted,
            "promoted": promoted,
            "remaining": remaining,
        }

    def _history_timestamps(self, history_dir: Path, capture_type: str) -> List[str]:
        """All history timestamps present for one capture, newest first."""
        if not history_dir.exists():
            return []
        out = set()
        for f in history_dir.iterdir():
            m = _HISTORY_RE.match(f.name)
            if m and m.group("type") == capture_type and m.group("ext") == "txt":
                out.add(m.group("ts"))
        return sorted(out, reverse=True)

    def _newest_history_ts(self, history_dir: Path, capture_type: str) -> Optional[str]:
        ts = self._history_timestamps(history_dir, capture_type)
        return ts[0] if ts else None

    def diff(
        self,
        device: str,
        site: str,
        capture_type: str = "config",
        from_version: Optional[str] = None,
        to_version: Optional[str] = None,
        mask_common: bool = False,
        ignore_fields: Optional[list] = None,
        include_text: bool = True,
    ) -> Optional[DiffResult]:
        """
        Structured diff between two versions of one capture type.

        With no versions specified, diffs the newest historical snapshot
        against the current state — i.e. "what changed in the last
        collection." With both specified, diffs those two (each is either
        "current" or a history timestamp).

        Returns both a structured record diff (added/removed/changed via the
        shared engine) and a unified raw-text diff for the Oxidized-compat
        consumers and the raw diff pane. Returns None when there aren't two
        versions of this capture type to compare.
        """
        device_dir = self._device_dir(site, device)

        # ── Resolve the pair to compare ──────────────────────────────
        if from_version and to_version:
            from_id, to_id = from_version, to_version
        else:
            history = self._history_versions_for_type(device_dir, capture_type)
            if not history:
                # Only the current state (or nothing) exists for this type.
                logger.debug(
                    "Need at least 2 %s snapshots to diff %s/%s",
                    capture_type, site, device,
                )
                return None
            from_id = history[0]   # newest history timestamp (older state)
            to_id = "current"      # latest collection (newer state)

        from_snap = self.fetch_version(device, site, from_id, capture_type)
        to_snap = self.fetch_version(device, site, to_id, capture_type)
        if from_snap is None or to_snap is None:
            return None

        # ── Structured record diff (shared, backend-agnostic) ────────
        # Routed on is_record_capture(), not on membership in a hardcoded
        # slug list: a capture type defined in captures.yaml must get the
        # same semantic diff as a built-in one. Documents (configs) fall
        # through to the line-aligned diff below, which is what they want.
        from_records = (
            (from_snap.parsed_data or {}).get("records", [])
            if from_snap.parsed_data else []
        )
        to_records = (
            (to_snap.parsed_data or {}).get("records", [])
            if to_snap.parsed_data else []
        )

        if is_record_capture(capture_type, from_records, to_records):
            added, removed, changed, schema, key_fields, ignored = (
                self._structured_with_schema(
                    capture_type, from_records, to_records, ignore_fields
                )
            )
        else:
            added, removed, changed = [], [], []
            schema, key_fields, ignored = [], [], []

        # ── Raw text diff + line-aligned diff ────────────────────────
        # Both are O(lines²)-ish on large captures, and a caller that only
        # needs the record verdict (the change feed's batched verify) never
        # reads them. Skip the work unless it was asked for, or unless the
        # record diff came back with no schema — in which case the text diff
        # IS the answer and has to be computed.
        if include_text or not schema:
            raw_diff = "\n".join(difflib.unified_diff(
                (from_snap.raw_text or "").splitlines(),
                (to_snap.raw_text or "").splitlines(),
                fromfile=f"{capture_type}@{from_id}",
                tofile=f"{capture_type}@{to_id}",
                lineterm="",
            ))
            aligned = self._build_aligned(
                from_snap.raw_text, to_snap.raw_text,
                mask_common=mask_common, capture_type=capture_type,
            )
        else:
            raw_diff = ""
            aligned = None

        return DiffResult(
            device=device,
            capture_type=capture_type,
            from_version=from_id,
            to_version=to_id,
            from_time=(
                from_snap.collected_at.isoformat() + "Z"
                if from_snap.collected_at else None
            ),
            to_time=(
                to_snap.collected_at.isoformat() + "Z"
                if to_snap.collected_at else None
            ),
            added=added,
            removed=removed,
            changed=changed,
            raw_diff=raw_diff,
            aligned=aligned,
            schema=schema,
            key_fields=key_fields,
            ignored=ignored,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _history_versions_for_type(
        self, device_dir: Path, capture_type: str
    ) -> List[str]:
        """
        Return history timestamp IDs for ONE capture type, newest first.

        Used by diff() to find the prior state to compare against the
        current files. Only timestamps that have a .txt are returned
        (a .json without a .txt can't anchor a diff).
        """
        history_dir = device_dir / "_history"
        if not history_dir.exists():
            return []

        txt_re = re.compile(
            rf"^{re.escape(capture_type)}\.(?P<ts>\d{{8}}T\d{{6}}Z)\.txt$"
        )
        timestamps = []
        for f in history_dir.iterdir():
            m = txt_re.match(f.name)
            if m:
                timestamps.append(m.group("ts"))

        return sorted(timestamps, reverse=True)

    def fleet_changes(
        self,
        since: str,
        until: Optional[str] = None,
        capture_type: Optional[str] = None,
        site: Optional[str] = None,
    ) -> List[FleetChange]:
        """
        Fleet-wide change feed reconstructed from the _history/ timeline.

        This backend rotates a capture to _history/ only when its raw text
        actually changed (store() short-circuits on an identical body), so
        every rotated file marks a real change — no de-duplication needed.
        The change feed rebuilds each capture's version timeline from
        filenames alone (no content reads): the embedded timestamp on each
        _history entry is that version's collection time, and the current
        file's mtime is the latest one.

        For each capture, versions are ordered oldest→newest; the earliest
        surviving version is reported as 'new' and every later one as
        'drift' (the moment a new body replaced the previous). version_id /
        prev_version_id use this backend's own IDs ("current" or a history
        timestamp), so a row links straight into diff().

        Limits vs the git backend: no per-version trigger (this backend
        doesn't record it) — trigger is left blank; and 'gone' is not
        detectable (there's no deletion event in a rotate-on-change tree).
        If history has been pruned to a single surviving version, the oldest
        one may read as 'new' rather than 'drift'; recent windows are
        unaffected since those events sit well after the first collection.
        """
        since_dt = self._parse_window(since)
        until_dt = self._parse_window(until) if until else None

        if site:
            site_dirs = [self.base_path / site]
        else:
            site_dirs = [
                d for d in self.base_path.iterdir()
                if d.is_dir() and not d.name.startswith(("_", "."))
            ]

        rows: List[FleetChange] = []

        for site_dir in site_dirs:
            if not site_dir.exists():
                continue
            psite = site_dir.name
            for dev_dir in sorted(site_dir.iterdir()):
                if not dev_dir.is_dir() or dev_dir.name.startswith("_"):
                    continue
                device = dev_dir.name

                # Capture types currently present, plus any seen only in
                # history (defensive — current should always exist).
                caps = {p.stem for p in dev_dir.glob("*.txt")}
                hist: dict = {}   # capture -> [(dt, ts_id), ...]
                hist_dir = dev_dir / "_history"
                if hist_dir.exists():
                    for f in hist_dir.iterdir():
                        m = _HISTORY_RE.match(f.name)
                        if not m or m.group("ext") != "txt":
                            continue
                        cap = m.group("type")
                        try:
                            dt = datetime.strptime(m.group("ts"), "%Y%m%dT%H%M%SZ")
                        except ValueError:
                            continue
                        hist.setdefault(cap, []).append((dt, m.group("ts")))
                        caps.add(cap)

                for cap in caps:
                    if capture_type and cap != capture_type:
                        continue

                    entries = list(hist.get(cap, []))            # (dt, ts_id)
                    cur = dev_dir / f"{cap}.txt"
                    if cur.exists():
                        entries.append((self._file_mtime(cur), "current"))
                    if not entries:
                        continue

                    entries.sort(key=lambda e: e[0])             # oldest first

                    for i, (dt, vid) in enumerate(entries):
                        # Classify from the FULL timeline, then window-filter,
                        # so a windowed drift is never mislabeled 'new'.
                        if since_dt and dt < since_dt:
                            continue
                        if until_dt and dt > until_dt:
                            continue
                        rows.append(FleetChange(
                            device=device,
                            site=psite,
                            capture_type=cap,
                            kind="new" if i == 0 else "drift",
                            trigger="",                          # not recorded here
                            at=dt.isoformat() + "Z",
                            version_id=vid,
                            prev_version_id=entries[i - 1][1] if i > 0 else None,
                        ))

        rows.sort(key=lambda r: r.at, reverse=True)
        return rows

    @staticmethod
    def _parse_window(value: Optional[str]) -> Optional[datetime]:
        """Parse an absolute ISO-8601 window bound (naive UTC) from the API."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.strip().rstrip("Z"))
        except ValueError:
            logger.warning("fleet_changes: unparseable window bound %r", value)
            return None

    def _rotate_to_history(self, device_dir: Path, capture_type: str) -> None:
        """Move current files to _history/ with timestamp suffix."""
        history_dir = device_dir / "_history"

        raw_path = device_dir / self._raw_filename(capture_type)
        parsed_path = device_dir / self._parsed_filename(capture_type)

        if not raw_path.exists():
            return

        history_dir.mkdir(exist_ok=True)

        # Use the file's mtime as the timestamp
        ts = self._timestamp_str(self._file_mtime(raw_path))

        # Move raw
        history_raw = history_dir / f"{capture_type}.{ts}.txt"
        shutil.copy2(str(raw_path), str(history_raw))

        # Move parsed (if exists)
        if parsed_path.exists():
            history_parsed = history_dir / f"{capture_type}.{ts}.json"
            shutil.copy2(str(parsed_path), str(history_parsed))

    def _prune_history(self, device_dir: Path, capture_type: str) -> None:
        """Remove oldest history files when over max_versions."""
        history_dir = device_dir / "_history"
        if not history_dir.exists():
            return

        # Find all history files for this capture type
        prefix = f"{capture_type}."
        history_files = sorted(
            [f for f in history_dir.iterdir() if f.name.startswith(prefix)],
            key=lambda f: f.name,
            reverse=True,
        )

        # Group by timestamp (txt + json = 2 files per version)
        # Keep max_versions * 2 files
        max_files = self.max_versions * 2
        if len(history_files) > max_files:
            for old_file in history_files[max_files:]:
                old_file.unlink()
                logger.debug("Pruned old history: %s", old_file)

    @staticmethod
    def _timestamp_str(dt: Optional[datetime] = None) -> str:
        """Generate a timestamp string for version IDs."""
        dt = dt or datetime.utcnow()
        return dt.strftime("%Y%m%dT%H%M%SZ")

    @staticmethod
    def _file_mtime(path: Path) -> datetime:
        """Get a file's modification time as datetime."""
        return datetime.utcfromtimestamp(path.stat().st_mtime)