"""
Git Storage Backend

Full-featured storage with git-backed versioning. Every collection
produces a commit containing both raw CLI text and tfsm-fire parsed
JSON. Commit trailers encode metadata for machine-queryable history.

Directory layout (git working tree):

  {base_path}/
    site1/
      border-rtr-01/
        config.txt
        config.json
        arp.txt
        arp.json
        bgp.txt
        bgp.json
    site2/
      fra-border-01/
        config.txt
        config.json
    _meta/
      devices.json
      platforms.json

Commit conventions:
  Author:   netlapse <netlapse@{hostname}>
  Message:  border-rtr-01: scheduled collection
  Trailers:
    X-Netlapse-Device: border-rtr-01
    X-Netlapse-Site: site1
    X-Netlapse-Trigger: scheduled|syslog|manual
    X-Netlapse-Types: config,arp,bgp

Structured diffs compare parsed JSON records using key fields
appropriate to each capture type (ADDRESS for ARP, NEIGHBOR for
BGP, INTERFACE for interfaces, etc.).
"""

import json
import logging
import os
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from netlapse.storage.backend import (
    StorageBackend,
    Snapshot,
    StoredVersion,
    DiffResult,
    FleetChange,
)
from netlapse.storage.diff import diff_records, is_record_capture

logger = logging.getLogger(__name__)

# The structured-diff record engine and the DIFF_KEY_FIELDS map now live in
# netlapse/storage/diff.py — a single source of truth shared by both
# backends. This backend contributes only the git-specific part: reading
# two versions of a capture's parsed JSON out of commit trees, then handing
# the record lists to diff_records().


class GitBackend(StorageBackend):
    """
    Git-backed storage with full versioning and structured diffs.

    Requires gitpython (pip install gitpython) or a git binary
    on PATH. Uses gitpython's interface for all operations.
    """

    def __init__(
        self,
        base_path: str | Path,
        author_name: str = "netlapse",
        author_email: Optional[str] = None,
    ):
        super().__init__(base_path)
        self.author_name = author_name
        self.author_email = author_email or f"netlapse@{socket.gethostname()}"
        self._repo = None

    @property
    def repo(self):
        """Lazy-load the git repo object."""
        if self._repo is None:
            try:
                from git import Repo
                self._repo = Repo(str(self.base_path))
            except Exception as e:
                raise RuntimeError(
                    f"Git repo not initialized at {self.base_path}. "
                    f"Run 'netlapse init' first. Error: {e}"
                )
        return self._repo

    def initialize(self) -> None:
        """
        Initialize the git repository.

        Creates the directory and runs git init if needed.
        Sets up .gitignore for temp files.
        """
        from git import Repo

        self.base_path.mkdir(parents=True, exist_ok=True)

        if (self.base_path / ".git").exists():
            self._repo = Repo(str(self.base_path))
            logger.info("Git backend: existing repo at %s", self.base_path)
        else:
            self._repo = Repo.init(str(self.base_path))
            logger.info("Git backend: initialized new repo at %s", self.base_path)

            # Create .gitignore
            gitignore = self.base_path / ".gitignore"
            gitignore.write_text(
                "*.tmp\n*.swp\n*.lock\n__pycache__/\n",
                encoding="utf-8",
            )
            self._repo.index.add([".gitignore"])
            self._repo.index.commit(
                "Initial commit",
                author=self._actor(),
                committer=self._actor(),
            )

    def store(self, snapshot: Snapshot, trigger: str = "scheduled") -> str:
        """
        Store a single snapshot as a git commit.

        Returns the commit SHA.
        """
        return self.store_batch([snapshot], trigger=trigger)

    def store_batch(
        self,
        snapshots: List[Snapshot],
        trigger: str = "scheduled",
    ) -> str:
        """
        Store multiple snapshots for the same device as a single commit.

        Writes all files, stages them, and commits with trailers.
        If nothing changed (git reports no diff), skips the commit.
        Returns the commit SHA (or HEAD SHA if no change).
        """
        if not snapshots:
            return str(self.repo.head.commit)

        # Validate single device
        devices = {(s.device, s.site) for s in snapshots}
        if len(devices) > 1:
            raise ValueError(
                f"store_batch requires all snapshots from the same device, "
                f"got: {devices}"
            )

        device = snapshots[0].device
        site = snapshots[0].site
        device_dir = self._device_dir(site, device)
        device_dir.mkdir(parents=True, exist_ok=True)

        # Write files
        files_to_add = []
        capture_types = []

        for snapshot in snapshots:
            capture_types.append(snapshot.capture_type)

            # Raw text
            raw_path = device_dir / self._raw_filename(snapshot.capture_type)
            raw_path.write_text(snapshot.raw_text, encoding="utf-8")
            files_to_add.append(
                str(raw_path.relative_to(self.base_path))
            )

            # Parsed JSON (if available)
            if snapshot.parsed_data is not None:
                parsed_path = device_dir / self._parsed_filename(
                    snapshot.capture_type
                )
                envelope = self._build_json_envelope(snapshot)
                parsed_path.write_text(
                    json.dumps(envelope, indent=2, default=str),
                    encoding="utf-8",
                )
                files_to_add.append(
                    str(parsed_path.relative_to(self.base_path))
                )

        # Stage files
        self.repo.index.add(files_to_add)

        # Check if anything actually changed
        if not self.repo.index.diff("HEAD") and not self.repo.untracked_files:
            # Also check staged diff for initial commits
            diff = self.repo.index.diff(self.repo.head.commit)
            if not diff:
                logger.debug(
                    "No changes for %s/%s, skipping commit", site, device
                )
                return str(self.repo.head.commit)

        # Build commit message with trailers
        types_str = ",".join(sorted(set(capture_types)))
        message = (
            f"{device}: {trigger} collection\n"
            f"\n"
            f"X-Netlapse-Device: {device}\n"
            f"X-Netlapse-Site: {site}\n"
            f"X-Netlapse-Trigger: {trigger}\n"
            f"X-Netlapse-Types: {types_str}\n"
        )

        commit = self.repo.index.commit(
            message,
            author=self._actor(),
            committer=self._actor(),
        )

        logger.info(
            "Committed %s/%s [%s] (%s) → %s",
            site, device, types_str, trigger, commit.hexsha[:8],
        )
        return commit.hexsha

    def update_parsed(self, snapshot: Snapshot) -> str:
        """
        Overwrite the parsed JSON for the current snapshot and commit it
        as a 'reparse' commit. Raw text is never rewritten or rotated.

        Re-deriving parsed data from unchanged raw is a legitimate change
        to the parsed artifact, so it gets its own commit with a reparse
        trailer — the history stays auditable and diffable. If the new
        parse is byte-identical to what's already committed, no commit is
        made and HEAD is returned.
        """
        device = snapshot.device
        site = snapshot.site
        device_dir = self._device_dir(site, device)
        device_dir.mkdir(parents=True, exist_ok=True)

        parsed_path = device_dir / self._parsed_filename(snapshot.capture_type)
        envelope = self._build_json_envelope(snapshot)
        parsed_path.write_text(
            json.dumps(envelope, indent=2, default=str),
            encoding="utf-8",
        )

        rel_path = str(parsed_path.relative_to(self.base_path))
        self.repo.index.add([rel_path])

        # Skip if the parsed artifact didn't actually change
        if not self.repo.index.diff("HEAD") and not self.repo.untracked_files:
            diff = self.repo.index.diff(self.repo.head.commit)
            if not diff:
                logger.debug(
                    "Reparse of %s/%s/%s produced no change, skipping commit",
                    site, device, snapshot.capture_type,
                )
                return str(self.repo.head.commit)

        message = (
            f"{device}: reparse {snapshot.capture_type}\n"
            f"\n"
            f"X-Netlapse-Device: {device}\n"
            f"X-Netlapse-Site: {site}\n"
            f"X-Netlapse-Trigger: reparse\n"
            f"X-Netlapse-Types: {snapshot.capture_type}\n"
            f"X-Netlapse-Template: {snapshot.template_name or '-'}\n"
            f"X-Netlapse-Score: {snapshot.template_score or 0}\n"
        )
        commit = self.repo.index.commit(
            message,
            author=self._actor(),
            committer=self._actor(),
        )
        logger.info(
            "Reparsed %s/%s/%s → %s (score=%s) %s",
            site, device, snapshot.capture_type,
            snapshot.template_name, snapshot.template_score,
            commit.hexsha[:8],
        )
        return commit.hexsha

    def fetch_latest(
        self,
        device: str,
        site: str,
        capture_type: str = "config",
    ) -> Optional[Snapshot]:
        """Read the current working tree files (HEAD)."""
        device_dir = self._device_dir(site, device)
        raw_path = device_dir / self._raw_filename(capture_type)

        if not raw_path.exists():
            return None

        raw_text = raw_path.read_text(encoding="utf-8", errors="replace")

        parsed_data = None
        parsed_path = device_dir / self._parsed_filename(capture_type)
        if parsed_path.exists():
            try:
                parsed_data = json.loads(
                    parsed_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError:
                logger.warning("Corrupt JSON at %s", parsed_path)

        # Get the commit time for this file
        collected_at = None
        try:
            rel_path = str(raw_path.relative_to(self.base_path))
            commits = list(self.repo.iter_commits(paths=rel_path, max_count=1))
            if commits:
                collected_at = datetime.utcfromtimestamp(
                    commits[0].committed_date
                )
        except Exception:
            pass

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

    def fetch_latest_raw(
        self,
        device: str,
        site: str,
        capture_type: str = "config",
    ) -> Optional[str]:
        """Fast path for Oxidized compat: read raw text from working tree."""
        raw_path = (
            self._device_dir(site, device)
            / self._raw_filename(capture_type)
        )
        if not raw_path.exists():
            return None
        return raw_path.read_text(encoding="utf-8", errors="replace")

    def search_configs(
        self,
        pattern: str,
        site: Optional[str] = None,
        capture_type: str = "config",
    ) -> List[dict]:
        """
        Regex search across all current raw text files for a capture type.

        Uses manual scan of the working tree.
        """
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            logger.warning("Invalid search pattern '%s': %s", pattern, e)
            return []

        results = []
        filename = self._raw_filename(capture_type)

        # Walk the working tree
        if site:
            site_dirs = [self.base_path / site]
        else:
            site_dirs = [
                d for d in self.base_path.iterdir()
                if d.is_dir()
                and not d.name.startswith("_")
                and not d.name.startswith(".")
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

    # ------------------------------------------------------------------
    # Versioning
    # ------------------------------------------------------------------

    def list_versions(
        self,
        device: str,
        site: str,
        limit: int = 50,
    ) -> List[StoredVersion]:
        """
        List commits that touched this device's files.

        Uses git log filtered by path prefix.
        """
        rel_prefix = f"{site}/{device}/"

        versions = []
        try:
            for commit in self.repo.iter_commits(
                paths=rel_prefix, max_count=limit
            ):
                # Parse trailers from commit message
                trigger = "scheduled"
                types = []
                for line in commit.message.splitlines():
                    if line.startswith("X-Netlapse-Trigger:"):
                        trigger = line.split(":", 1)[1].strip()
                    elif line.startswith("X-Netlapse-Types:"):
                        types = [
                            t.strip()
                            for t in line.split(":", 1)[1].split(",")
                        ]

                versions.append(StoredVersion(
                    version_id=commit.hexsha,
                    collected_at=datetime.utcfromtimestamp(
                        commit.committed_date
                    ),
                    message=commit.summary,
                    author=str(commit.author),
                    trigger=trigger,
                    capture_types=types,
                ))
        except Exception as e:
            logger.warning("Error listing versions for %s/%s: %s", site, device, e)

        return versions

    def fetch_version(
        self,
        device: str,
        site: str,
        version_id: str,
        capture_type: str = "config",
    ) -> Optional[Snapshot]:
        """
        Fetch files from a specific commit.

        Reads the blob directly from the git object store —
        does not check out or modify the working tree.
        """
        raw_rel = f"{site}/{device}/{self._raw_filename(capture_type)}"
        parsed_rel = f"{site}/{device}/{self._parsed_filename(capture_type)}"

        try:
            commit = self.repo.commit(version_id)
        except Exception:
            logger.warning("Commit not found: %s", version_id)
            return None

        # Read raw text from commit tree
        raw_text = self._read_blob(commit, raw_rel)
        if raw_text is None:
            return None

        # Read parsed JSON from commit tree
        parsed_data = None
        parsed_text = self._read_blob(commit, parsed_rel)
        if parsed_text:
            try:
                parsed_data = json.loads(parsed_text)
            except json.JSONDecodeError:
                pass

        return Snapshot(
            device=device,
            site=site,
            capture_type=capture_type,
            command=parsed_data.get("command", "") if parsed_data else "",
            raw_text=raw_text,
            parsed_data=parsed_data,
            collected_at=datetime.utcfromtimestamp(commit.committed_date),
            template_name=(
                parsed_data.get("template") if parsed_data else None
            ),
            template_score=(
                parsed_data.get("template_score") if parsed_data else None
            ),
        )

    def delete_version(
        self,
        device: str,
        site: str,
        capture_type: str,
        version_id: str,
    ) -> dict:
        """
        Not supported on the git backend: snapshots are commits, and removing
        one means rewriting history (rebase/filter-repo), which changes every
        downstream SHA and isn't safe to do from a running collector.

        To retire a bad current snapshot here, re-collect the capture (or
        commit a revert to the prior content) so the drift is corrected
        forward rather than by rewriting the past.
        """
        raise NotImplementedError(
            "git backend versions are immutable commits — deleting one would "
            "rewrite history. Re-collect the capture to correct it forward."
        )

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
        Compute structured diff between two versions.

        If versions not specified, uses the two most recent commits
        that touched this device's files.

        Returns both:
          - raw_diff: git-style text diff (for Oxidized compat)
          - added/removed/changed: structured record diff (for native API)
        """
        rel_prefix = f"{site}/{device}/"

        # Resolve commits
        if from_version and to_version:
            try:
                from_commit = self.repo.commit(from_version)
                to_commit = self.repo.commit(to_version)
            except Exception as e:
                logger.warning("Could not resolve commits: %s", e)
                return None
        else:
            # Find the two most recent commits for this device
            commits = list(
                self.repo.iter_commits(paths=rel_prefix, max_count=2)
            )
            if len(commits) < 2:
                logger.debug("Need at least 2 commits to diff %s/%s", site, device)
                return None
            to_commit = commits[0]    # newer
            from_commit = commits[1]  # older

        raw_rel = f"{site}/{device}/{self._raw_filename(capture_type)}"
        parsed_rel = f"{site}/{device}/{self._parsed_filename(capture_type)}"

        # Structured diff first — it decides whether the text artifacts are
        # needed at all. Previously this ran unconditionally while the file
        # backend gated it, so the two backends could return different answers
        # for the same capture; both now route on is_record_capture().
        added, removed, changed, schema, key_fields, ignored = self._structured_diff(
            from_commit, to_commit, parsed_rel, capture_type, ignore_fields
        )

        # Raw text diff (Oxidized compat) + line-aligned config-aware diff.
        # Skipped when the caller only needs the record verdict and the record
        # diff produced one — see file_backend.diff() for the rationale.
        if include_text or not schema:
            raw_diff = self._git_text_diff(from_commit, to_commit, raw_rel)
            aligned = self._build_aligned(
                self._read_blob(from_commit, raw_rel),
                self._read_blob(to_commit, raw_rel),
                mask_common=mask_common,
                capture_type=capture_type,
            )
        else:
            raw_diff = ""
            aligned = None

        return DiffResult(
            device=device,
            capture_type=capture_type,
            from_version=from_commit.hexsha,
            to_version=to_commit.hexsha,
            from_time=datetime.utcfromtimestamp(
                from_commit.committed_date
            ).isoformat() + "Z",
            to_time=datetime.utcfromtimestamp(
                to_commit.committed_date
            ).isoformat() + "Z",
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

    # Record + field separators for the custom git-log format. Chosen from
    # the control range so they can't collide with paths, triggers, or SHAs.
    _FLEET_REC = "\x1e"
    _FLEET_FS = "\x1f"

    def fleet_changes(
        self,
        since: str,
        until: Optional[str] = None,
        capture_type: Optional[str] = None,
        site: Optional[str] = None,
    ) -> List[FleetChange]:
        """
        Fleet-wide change feed via a single `git log --name-status`.

        One git invocation walks every commit in the window newest-first and
        emits, per changed raw capture file, a FleetChange. No blobs are read
        and no diffs are computed here — magnitude/detail live behind the
        per-device diff, which each row links to via version_id/prev_version_id.

        State is keyed on the raw .txt blob. Reparse commits rewrite only the
        .json artifact and so are invisible to this feed by construction — a
        template edit can never masquerade as device drift. (Parse-artifact
        churn is Parse Audit's concern, not the change feed's.) The previous
        version of each capture is resolved in the same pass when it falls
        inside the window, giving a free "from" side for recent drill-ins.
        """
        REC, FS = self._FLEET_REC, self._FLEET_FS
        fmt = (
            f"{REC}%H{FS}%ct{FS}"
            f"%(trailers:key=X-Netlapse-Trigger,valueonly)"
        )
        args = [
            "--no-renames",         # keep status to single-letter A/M/D
            "--name-status",
            f"--pretty=tformat:{fmt}",
            f"--since={since}",
        ]
        if until:
            args.append(f"--until={until}")

        try:
            out = self.repo.git.log(*args)
        except Exception as e:  # empty repo / bad revision range
            logger.warning("fleet_changes git log failed: %s", e)
            return []

        rows: List[FleetChange] = []
        # (site, device, capture) -> the most recent row still awaiting its
        # "from" version. When we meet the same capture again (older commit),
        # that older SHA is the previous version for the newer row.
        pending_prev: Dict[tuple, FleetChange] = {}

        for block in out.split(REC):
            block = block.strip("\n")
            if not block:
                continue
            header, *file_lines = block.split("\n")
            try:
                sha, epoch, trigger = header.split(FS)
            except ValueError:
                continue
            trigger = (trigger or "scheduled").strip()
            at = datetime.utcfromtimestamp(int(epoch)).isoformat() + "Z"

            for fl in file_lines:
                fl = fl.strip()
                if not fl:
                    continue
                status, _, path = fl.partition("\t")
                status = status[:1]  # A | M | D
                parts = path.split("/")
                if len(parts) != 3:
                    continue
                psite, device, fname = parts
                if "." not in fname:
                    continue
                cap, ext = fname.rsplit(".", 1)
                if ext != "txt":          # state is raw text; .json is derived
                    continue
                if site and psite != site:
                    continue
                if capture_type and cap != capture_type:
                    continue

                kind = {"A": "new", "M": "drift", "D": "gone"}.get(status)
                if kind is None:
                    continue

                key = (psite, device, cap)
                # This (older) commit is the previous version for the last
                # row we recorded on the same capture.
                waiting = pending_prev.get(key)
                if waiting is not None:
                    waiting.prev_version_id = sha

                row = FleetChange(
                    device=device,
                    site=psite,
                    capture_type=cap,
                    kind=kind,
                    trigger=trigger,
                    at=at,
                    version_id=sha,
                )
                rows.append(row)
                pending_prev[key] = row

        return rows

    def _actor(self):
        """Build a git Actor for commits."""
        from git import Actor
        return Actor(self.author_name, self.author_email)

    def _read_blob(self, commit, rel_path: str) -> Optional[str]:
        """Read a file from a specific commit's tree."""
        try:
            blob = commit.tree / rel_path
            return blob.data_stream.read().decode("utf-8", errors="replace")
        except (KeyError, TypeError):
            return None

    def _git_text_diff(self, from_commit, to_commit, rel_path: str) -> str:
        """
        Get git-style unified diff between two commits for a file.
        Used by the Oxidized compat version/view endpoint.
        """
        try:
            diffs = from_commit.diff(
                to_commit, paths=[rel_path], create_patch=True
            )
            for d in diffs:
                if d.diff:
                    return d.diff.decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("Error computing text diff: %s", e)
        return ""

    def _structured_diff(
        self,
        from_commit,
        to_commit,
        parsed_rel: str,
        capture_type: str,
        ignore_fields: Optional[list] = None,
    ) -> tuple:
        """
        Compare parsed JSON records between two commits.

        Reads the two parsed blobs, then defers to the shared helper for the
        record comparison, schema, key resolution, and field exclusion.

        Returns (added, removed, changed, schema, key_fields, ignored).
        """
        from_text = self._read_blob(from_commit, parsed_rel)
        to_text = self._read_blob(to_commit, parsed_rel)

        if not from_text or not to_text:
            return [], [], [], [], [], (ignore_fields or [])

        try:
            from_data = json.loads(from_text)
            to_data = json.loads(to_text)
        except json.JSONDecodeError:
            return [], [], [], [], [], (ignore_fields or [])

        from_records = from_data.get("records", [])
        to_records = to_data.get("records", [])

        # Documents (configs) are served by the line-aligned diff, not the
        # record engine — see is_record_capture(). The file backend applies
        # the same test, so both backends answer identically.
        if not is_record_capture(capture_type, from_records, to_records):
            return [], [], [], [], [], (ignore_fields or [])

        # Shared, backend-agnostic record comparison + field exclusion
        # (storage/backend.py → storage/diff.py, defaults from the volatility
        # registry). One source of truth for DIFF_KEY_FIELDS and the matching
        # logic across both backends.
        return self._structured_with_schema(
            capture_type, from_records, to_records, ignore_fields
        )