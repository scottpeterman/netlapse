"""
Storage Backend Interface

All storage backends implement this interface. The collection engine
and API layer never touch the filesystem directly — they go through
the backend, which handles layout, versioning, and retrieval.

Two backends ship with Netlapse:
  - FileBackend:  simple directory tree, last-N rotation, no diffs
  - GitBackend:   git-versioned with commit trailers, full diff support

The backend is selected in config.yaml:
  storage:
    backend: file    # or 'git'
    path: ~/.netlapse/data
"""

import abc
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes shared by all backends
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """A single captured state for one device + capture type."""
    device: str
    site: str
    capture_type: str
    command: str
    raw_text: str
    parsed_data: Optional[dict] = None
    collected_at: Optional[datetime] = None
    template_name: Optional[str] = None
    template_score: Optional[float] = None


@dataclass
class StoredVersion:
    """Metadata about a stored version (returned by list_versions)."""
    version_id: str            # git SHA or timestamp string
    collected_at: datetime
    message: str = ""
    author: str = "netlapse"
    trigger: str = "scheduled"  # scheduled | syslog | manual
    capture_types: List[str] = field(default_factory=list)


@dataclass
class DiffResult:
    """Structured diff between two parsed snapshots."""
    device: str
    capture_type: str
    from_version: str
    to_version: str
    from_time: Optional[str] = None
    to_time: Optional[str] = None
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    raw_diff: Optional[str] = None   # unified text diff for Oxidized compat
    aligned: Optional[dict] = None   # line-aligned config-aware diff: {rows, summary}
    schema: Optional[list] = None      # all record column names (for field-exclusion UI)
    key_fields: Optional[list] = None  # identity field(s) — never excludable
    ignored: Optional[list] = None     # fields actually excluded from this comparison


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

@dataclass
class FleetChange:
    """
    One change event in the fleet-wide change feed.

    A change event is a single capture's raw text changing in one stored
    version. It is deliberately lightweight — this is the aggregate "what
    moved" index, not a diff. Magnitude and line-level detail come from
    drilling into the per-device diff (version_id + prev_version_id link
    straight to it).

    kind:
      new    — capture appeared for the first time in the window (git add)
      drift  — capture's raw text was modified (git modify) — the real signal
      gone   — capture disappeared (git delete): device stopped returning it

    version_id is the version this change landed in (the "to" side).
    prev_version_id is the previous version that touched the same capture,
    resolved for free in the same walk when it falls inside the window; None
    when it doesn't, in which case the diff viewer falls back to latest-two.
    """
    device: str
    site: str
    capture_type: str
    kind: str                       # new | drift | gone
    trigger: str                    # scheduled | syslog | manual
    at: str                         # ISO-8601 UTC
    version_id: str                 # "to" version (git SHA)
    prev_version_id: Optional[str] = None  # "from" version, if in-window


class StorageBackend(abc.ABC):
    """
    Abstract storage backend.

    Subclasses must implement the core methods. Methods that require
    versioning (list_versions, fetch_version, diff) may raise
    NotImplementedError on backends that don't support them.
    """

    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path).expanduser().resolve()

    # --- Core (all backends) ---

    @abc.abstractmethod
    def initialize(self) -> None:
        """
        Set up the storage directory structure.
        Called once on first run or when backend is switched.
        """
        ...

    @abc.abstractmethod
    def store(
        self,
        snapshot: Snapshot,
        trigger: str = "scheduled",
    ) -> str:
        """
        Store a snapshot (raw + parsed) for a device.

        Returns a version identifier (git SHA for git backend,
        timestamp string for file backend).
        """
        ...

    @abc.abstractmethod
    def store_batch(
        self,
        snapshots: List[Snapshot],
        trigger: str = "scheduled",
    ) -> str:
        """
        Store multiple snapshots for the same device in one operation.

        A collection cycle typically produces several capture types
        (config, arp, bgp, etc.) for one device. This batches them
        into a single commit (git) or a single timestamp group (file).

        All snapshots must be for the same device.
        Returns a version identifier.
        """
        ...

    @abc.abstractmethod
    def fetch_latest(
        self,
        device: str,
        site: str,
        capture_type: str = "config",
    ) -> Optional[Snapshot]:
        """
        Fetch the most recent snapshot for a device + capture type.
        Returns None if no snapshot exists.
        """
        ...

    @abc.abstractmethod
    def fetch_latest_raw(
        self,
        device: str,
        site: str,
        capture_type: str = "config",
    ) -> Optional[str]:
        """
        Fetch just the raw text of the most recent snapshot.
        Optimized path for Oxidized compat API (GET /node/fetch).
        """
        ...

    @abc.abstractmethod
    def search_configs(
        self,
        pattern: str,
        site: Optional[str] = None,
        capture_type: str = "config",
    ) -> List[dict]:
        """
        Search across all stored raw text files for a regex pattern.

        Args:
            pattern: regex pattern to search
            site: optional site slug filter
            capture_type: which capture type to search (default: config)

        Returns list of {"device": ..., "site": ..., "matches": [...]},
        where each match has {"line_number": int, "text": str}.
        """
        ...

    @abc.abstractmethod
    def list_capture_types(self) -> List[str]:
        """Return all distinct capture types with stored data."""
        ...

    # --- Cross-capture "one token" search (concrete, all backends) ---

    def search_all(
        self,
        regex: "re.Pattern",
        site: Optional[str] = None,
        max_per_file: int = 50,
    ) -> List[dict]:
        """
        Find a precompiled regex across EVERY capture type in a single walk.

        Where search_configs() scans one capture type for a user regex, this
        powers One Search: it visits each device directory once and greps
        every {capture_type}.txt under it, tagging each hit with the capture
        type it came from so the caller can group results by meaning.

        Both shipping backends keep the same working-tree layout
        ({base}/{site}/{device}/{capture_type}.txt), so this lives here and
        is inherited unchanged — there is nothing git- or file-specific about
        reading the current working tree.

        Args:
            regex:        compiled pattern (build it with token_match, which
                          handles IP/MAC/ASN boundary and format normalization)
            site:         optional site slug filter
            max_per_file: cap matched lines per capture file (context, not dump)

        Returns a list of:
            {"device", "site", "capture_type", "matches": [{line_number, text}]}
        one entry per (device, capture_type) that has at least one hit.
        """
        results: List[dict] = []

        if site:
            site_dirs = [self.base_path / site]
        else:
            site_dirs = [
                d for d in self.base_path.iterdir()
                if d.is_dir() and not d.name.startswith(("_", "."))
            ]

        for site_dir in site_dirs:
            if not site_dir.exists():
                continue
            for device_dir in sorted(site_dir.iterdir()):
                if not device_dir.is_dir() or device_dir.name.startswith("_"):
                    continue

                for txt_path in sorted(device_dir.glob("*.txt")):
                    try:
                        text = txt_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        continue

                    match_lines = []
                    for i, line in enumerate(text.splitlines(), start=1):
                        if regex.search(line):
                            match_lines.append(
                                {"line_number": i, "text": line}
                            )
                            if len(match_lines) >= max_per_file:
                                break

                    if match_lines:
                        results.append({
                            "device": device_dir.name,
                            "site": site_dir.name,
                            "capture_type": txt_path.stem,
                            "matches": match_lines,
                        })

        return results

    def update_parsed(self, snapshot: Snapshot) -> str:
        """
        Re-derive and persist ONLY the parsed JSON artifact for the
        current (latest) snapshot, leaving raw text untouched.

        This is the storage primitive behind the reparse workflow: a
        template was added/edited/removed and the operator wants stored
        parsed data re-generated from already-collected raw output. It is
        deliberately distinct from store():

          - store() rotates/versions raw and skips writing parsed JSON when
            raw is unchanged and a JSON already exists. Correct for
            collection, wrong for reparse (it can't overwrite a bad parse).
          - update_parsed() overwrites the parsed JSON in place, never
            rotates raw, and never churns raw history. On the git backend it
            lands as a 'reparse' commit so the change stays auditable.

        Implementations write {site}/{device}/{capture_type}.json from
        snapshot.parsed_data / template_name / template_score and return a
        version identifier (timestamp for file, commit SHA for git).
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support reparse."
        )

    # --- Versioning (git backend, raises on file backend) ---

    def list_versions(
        self,
        device: str,
        site: str,
        limit: int = 50,
    ) -> List[StoredVersion]:
        """List stored versions for a device, newest first."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support version history. "
            "Switch to the git backend for versioning."
        )

    def fetch_version(
        self,
        device: str,
        site: str,
        version_id: str,
        capture_type: str = "config",
    ) -> Optional[Snapshot]:
        """Fetch a specific version of a snapshot."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support version retrieval."
        )

    def delete_version(
        self,
        device: str,
        site: str,
        capture_type: str,
        version_id: str,
    ) -> dict:
        """
        Delete one capture's snapshot (raw + parsed) for a device.

        Scoped to a single capture type on purpose: a timestamp can hold
        several captures, and a bad parse or bad collection is almost always
        one of them — deleting the whole timestamp would take good captures
        with it.

        version_id is a history timestamp or the literal "current". Backends
        that can cleanly remove a version return a summary dict; those that
        can't (immutable history) raise NotImplementedError.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support deleting a version."
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

        If from_version/to_version not specified, diffs the two
        most recent versions.

        The git backend computes both raw text diff (for Oxidized compat)
        and structured JSON diff (for the native API). Both backends also
        attach a line-aligned, config-aware diff (DiffResult.aligned) built
        from the two raw texts via storage/config_diff.py.

        mask_common: when True, volatile lines (config-change timestamps,
        "Last configuration change", etc.) are masked from the comparison
        key so they don't register as drift. Display is always the real line.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support diffs."
        )

    def fleet_changes(
        self,
        since: str,
        until: Optional[str] = None,
        capture_type: Optional[str] = None,
        site: Optional[str] = None,
    ) -> List["FleetChange"]:
        """
        Return every capture-level change across the whole fleet in a window,
        newest first. This is the aggregate change feed: "which devices and
        which capture types moved," answered without diffing each device.

        Args:
            since:   window start — git-compatible (ISO-8601 or "24 hours ago").
            until:   window end (default: now).
            capture_type: restrict to one capture (e.g. "config", "interfaces").
            site:    restrict to one site slug.

        Only the raw text of a capture is treated as state; parsed-JSON churn
        (e.g. a reparse after a template edit) is not a fleet change and never
        appears here. Requires a versioned backend.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support the change feed. "
            "Switch to the git backend for fleet-wide change history."
        )

    def classify_change(
        self,
        device: str,
        site: str,
        capture_type: str,
        to_version: str,
        from_version: Optional[str] = None,
        mask_common: bool = True,
    ) -> dict:
        """
        Semantic verdict for one candidate change: real drift, or just noise?

        A capture's raw text changing is NOT the same as it drifting. Age/
        uptime timers and counters tick every poll; config files carry a
        commit-timestamp line. This decides realness the SAME way the UI's
        Semantic Diff does, so the change feed and the drill-in never
        disagree:

          - parseable capture (records present) → structured diff with the
            volatility catalog applied (ignore_fields=None). Real iff any
            added/removed/changed record survives dropping volatile columns.
            The text diff is ignored for these — an ARP table where only AGE
            moved is all-red in text but empty here, which is the point.
          - config / raw-only (no records) → masked line diff. Real iff the
            text differs once common volatile lines (commit timestamps, etc.)
            are masked.

        A first appearance (no from_version) is always real ('new') — there's
        nothing to compare. Returns {real, mode, added, removed, changed},
        where the counts are the change magnitude for display.
        """
        if not from_version:
            return {"real": True, "mode": "new",
                    "added": 0, "removed": 0, "changed": 0}

        try:
            r = self.diff(
                device, site, capture_type,
                from_version=from_version, to_version=to_version,
                mask_common=mask_common, ignore_fields=None,
                include_text=False,
            )
        except NotImplementedError:
            # Backend can't diff — don't silently drop the event; surface it
            # as unverified rather than assert a verdict we can't compute.
            return {"real": True, "mode": "unverified",
                    "added": 0, "removed": 0, "changed": 0}

        if r is None:
            return {"real": False, "mode": "none",
                    "added": 0, "removed": 0, "changed": 0}

        # schema is populated only when the capture parses into records; its
        # presence is the parseable-vs-raw discriminator.
        if r.schema:
            a = len(r.added or [])
            rm = len(r.removed or [])
            c = len(r.changed or [])
            return {"real": bool(a or rm or c), "mode": "structured",
                    "added": a, "removed": rm, "changed": c}

        aligned = r.aligned or {}
        summ = aligned.get("summary") or {}
        real = bool(aligned) and not summ.get("identical", True)
        return {"real": real, "mode": "text",
                "added": summ.get("added", 0),
                "removed": summ.get("removed", 0),
                "changed": summ.get("changed", 0)}

    # --- Utility ---

    def _device_dir(self, site: str, device: str) -> Path:
        """Canonical path: {base}/{site_slug}/{device_name}/"""
        return self.base_path / site / device

    def _raw_filename(self, capture_type: str) -> str:
        return f"{capture_type}.txt"

    def _parsed_filename(self, capture_type: str) -> str:
        return f"{capture_type}.json"

    def _structured_with_schema(
        self,
        capture_type: str,
        from_records: list,
        to_records: list,
        ignore_fields: Optional[list] = None,
    ) -> tuple:
        """
        Run the record diff and resolve the field-exclusion context in one
        place for both backends.

        ignore_fields semantics:
          - None  → apply the defaults resolved from definitions/volatility.yaml
                    (heuristics + per-capture rules), so e.g. ARP age noise is
                    excluded out of the box.
          - list  → use exactly these (an explicit choice from the UI); the
                    empty list means "compare every field".
        Key fields are always kept regardless.

        Returns (added, removed, changed, schema, key_fields, ignored).
        """
        from netlapse.storage.diff import (
            diff_records,
            resolve_key_fields,
            record_field_names,
        )

        schema = record_field_names(from_records, to_records)
        key_fields = resolve_key_fields(capture_type, from_records, to_records)

        if ignore_fields is not None:
            effective = [f for f in ignore_fields if f not in key_fields]
        else:
            # Defaults come from the volatility registry (YAML-backed policy),
            # matched against the columns actually present. Lazy import keeps
            # the storage layer free of an import-time dependency on registry.
            from netlapse.registry.volatility import get_volatility_catalog
            effective = get_volatility_catalog().resolve(
                capture_type, schema, key_fields
            )

        added, removed, changed = diff_records(
            capture_type, from_records, to_records, ignore_fields=effective
        )
        return added, removed, changed, schema, key_fields, effective

    def _build_aligned(
        self,
        from_text: Optional[str],
        to_text: Optional[str],
        mask_common: bool = False,
        capture_type: str = "config",
    ) -> Optional[dict]:
        """
        Build the line-aligned, config-aware diff payload from two raw texts.

        Returns {"rows": [...], "summary": {...}} (see storage/config_diff.py
        for the row contract) or None if both texts are empty. This is the
        same engine for both backends — text diff has nothing git- or
        file-specific about it, so it lives on the base class.

        When mask_common is set, volatile config lines are masked using the
        per-capture regex set resolved from the volatility policy layer
        (BASELINE_LINE_MASKS ∪ volatility.yaml `line_masks`) — the line-level
        analogue of the field volatility the record diff applies.
        """
        if not from_text and not to_text:
            return None

        # Imported lazily so the storage layer has no import-time dependency
        # on config_diff for backends/paths that never diff.
        from netlapse.storage.config_diff import (
            diff_configs,
            NormalizeOptions,
            PREAMBLE_DROPS,
            SESSION_HEAD_DROPS,
        )

        # Volatile-line masking is a user choice (the "mask volatile lines"
        # toggle) AND now data-driven: the patterns come from the volatility
        # catalog, so an operator can add a volatile config line in
        # volatility.yaml without a code change. The code baseline still applies
        # even with no YAML.
        mask_patterns: list = []
        if mask_common:
            from netlapse.registry.volatility import get_volatility_catalog
            mask_patterns = get_volatility_catalog().resolve_line_masks(capture_type)

        opts = NormalizeOptions(
            mask_patterns=mask_patterns,
            # CLI session framing (prompt/command echo, RE context, blank edges)
            # is never state, so it's always scrubbed — the text diff gets the
            # same treatment the config engine already applies, which keeps
            # parseable captures (ospf, etc.) from showing prompt lines as drift.
            drop_patterns=list(PREAMBLE_DROPS),
            # The pagination-disable shotgun and its acknowledgements, matched
            # only in the leading frame — see SESSION_HEAD_DROPS. Without this a
            # snapshot taken before the shotgun was in place shows its whole
            # session header as deleted lines, which reads as real drift and
            # inflates the change counters.
            head_drop_patterns=list(SESSION_HEAD_DROPS),
        )
        return diff_configs(from_text or "", to_text or "", opts)

    def _build_json_envelope(self, snapshot: Snapshot) -> dict:
        """
        Build the JSON envelope for parsed data.

        Standard format:
        {
          "device": "border-rtr-01",
          "site": "site1",
          "capture_type": "arp",
          "command": "show ip arp",
          "template": "cisco_ios_show_ip_arp",
          "template_score": 87.5,
          "collected_at": "2026-05-24T14:90:00Z",
          "record_count": 47,
          "records": [...]
        }
        """
        collected = snapshot.collected_at or datetime.utcnow()
        envelope = {
            "device": snapshot.device,
            "site": snapshot.site,
            "capture_type": snapshot.capture_type,
            "command": snapshot.command,
            "template": snapshot.template_name,
            "template_score": snapshot.template_score,
            "collected_at": collected.isoformat() + "Z",
        }

        if snapshot.parsed_data and "records" in snapshot.parsed_data:
            envelope["record_count"] = len(snapshot.parsed_data["records"])
            envelope["records"] = snapshot.parsed_data["records"]
        elif snapshot.parsed_data:
            envelope["record_count"] = 0
            envelope["records"] = []
            envelope["raw_parsed"] = snapshot.parsed_data
        else:
            envelope["record_count"] = 0
            envelope["records"] = []

        return envelope