"""
Storage Cleanup — Reporter (dry-run analysis only)

This module *describes* what a cleanup would do. It never deletes, moves,
or rewrites anything. It walks the FileBackend working tree, reconciles it
against the DCIM device inventory, and produces a structured report:

  - Orphan classification — on-disk (site, device) directories that no
    longer correspond to a live DCIM device. Source of truth is DCIM, which
    is correct: delete_device() deliberately leaves storage on disk ("Purge
    those separately if you want them gone"), so nothing reconciles the tree.

  - Per-capture rollup — across the live fleet, how each capture type spends
    disk: total bytes, version counts, median snapshot size, and an estimated
    collection cadence. This is what tells you that a large + frequent capture
    (e.g. mtu) dominates the tree while the uniform max_versions cap gives
    every capture the same *count* but wildly different *time depth*.

  - Cruft — empty _history/ and device dirs, and stray .tmp files left by an
    interrupted atomic write.

Nothing here mutates state. The dataclasses are deliberately the shape an
--apply path and a REST route would consume later, so the scan is written
once and reused.

    python -m netlapse cleanup            # full text report
    python -m netlapse cleanup --json     # machine-readable
    python -m netlapse cleanup --top 30   # show 30 largest devices
    python -m netlapse cleanup --site site1

The tree layout is the one both shipping backends share:

    {base}/{site}/{device}/{capture}.txt          (current)
    {base}/{site}/{device}/{capture}.json
    {base}/{site}/{device}/_history/{capture}.{ts}.txt
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Same shape the backends write: {capture}.{YYYYMMDDThhmmssZ}.{txt|json}
# capture is non-greedy so the rigid timestamp anchors the split — this
# correctly handles multi-word types (lldp_neighbors, bgp_summary).
_HISTORY_RE = re.compile(
    r"^(?P<type>.+?)\.(?P<ts>\d{8}T\d{6}Z)\.(?P<ext>txt|json)$"
)
_TS_FMT = "%Y%m%dT%H%M%SZ"

# Atomic-write temp files: ".{name}.{rand}.tmp" (see file_backend._atomic_write_text)
_TMP_RE = re.compile(r"^\..*\.tmp$")


# ---------------------------------------------------------------------------
# Classification of an on-disk device directory against DCIM
# ---------------------------------------------------------------------------

# active     — (site, device) is a collection-enabled DCIM device. Keep.
# disabled   — matches a DCIM device, but collection_enabled = 0. Operator
#              call: kept history for a paused device, or a decommission in
#              progress. Reported, never auto-reclaimable.
# wrong_site — the device *name* exists in DCIM, but under a different site.
#              Almost always a re-site/rename migration leftover. Reported
#              for review; may hold history the correctly-sited tree lacks.
# orphan     — no DCIM device by that name anywhere. Bare-IP stubs and
#              superseded names (foo -> foo-new). Clearly reclaimable.
# no_dcim    — DCIM was empty/unavailable, so no verdict could be reached.
CLASS_ACTIVE = "active"
CLASS_DISABLED = "disabled"
CLASS_WRONG_SITE = "wrong_site"
CLASS_ORPHAN = "orphan"
CLASS_NO_DCIM = "no_dcim"

# Which classes count toward "you could reclaim this now without a policy call"
RECLAIMABLE_CLASSES = {CLASS_ORPHAN}


@dataclass
class CaptureStat:
    """Per-capture-type footprint inside one device directory."""
    capture_type: str
    current_bytes: int = 0           # current .txt + .json at device root
    has_current: bool = False
    history_versions: int = 0        # distinct timestamps in _history (by .txt)
    history_bytes: int = 0           # all _history files for this type
    oldest: Optional[str] = None     # ISO; earliest history timestamp
    newest: Optional[str] = None     # ISO; latest history timestamp
    median_snapshot_bytes: int = 0   # median raw .txt size across versions
    cadence_seconds: Optional[float] = None  # median delta between versions

    @property
    def total_bytes(self) -> int:
        return self.current_bytes + self.history_bytes


@dataclass
class DeviceReport:
    site: str
    device: str
    path: str
    classification: str = CLASS_NO_DCIM
    reason: str = ""
    dcim_status: Optional[str] = None     # DCIM device status if matched
    elsewhere_sites: List[str] = field(default_factory=list)  # for wrong_site
    total_bytes: int = 0
    captures: Dict[str, CaptureStat] = field(default_factory=dict)
    stray_tmp: List[str] = field(default_factory=list)
    empty_history: bool = False
    idle_days: Optional[float] = None     # days since newest artifact mtime

    @property
    def reclaimable(self) -> bool:
        return self.classification in RECLAIMABLE_CLASSES


@dataclass
class CaptureRollup:
    """Fleet-wide footprint of one capture type across active devices."""
    capture_type: str
    devices: int = 0
    total_bytes: int = 0
    total_versions: int = 0
    avg_versions: float = 0.0
    median_snapshot_bytes: int = 0
    cadence_seconds: Optional[float] = None
    span_days: Optional[float] = None


@dataclass
class CleanupReport:
    base_path: str
    generated_at: str
    dcim_devices: int
    total_bytes: int = 0
    device_count: int = 0
    class_counts: Dict[str, int] = field(default_factory=dict)
    class_bytes: Dict[str, int] = field(default_factory=dict)
    reclaimable_bytes: int = 0
    devices: List[DeviceReport] = field(default_factory=list)
    capture_rollup: List[CaptureRollup] = field(default_factory=list)
    stray_tmp_total: int = 0
    empty_history_dirs: int = 0
    empty_device_dirs: List[str] = field(default_factory=list)
    empty_site_dirs: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts, _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _median_int(values: List[int]) -> int:
    return int(statistics.median(values)) if values else 0


def _scan_device(site: str, device: str, device_dir: Path) -> DeviceReport:
    """Footprint one device directory. No DCIM verdict yet."""
    rpt = DeviceReport(site=site, device=device, path=str(device_dir))

    # Current capture files at the device root (each .txt is a capture type)
    capture_files: Dict[str, CaptureStat] = {}
    newest_mtime = 0.0

    for entry in device_dir.iterdir():
        name = entry.name
        if entry.is_dir():
            continue
        if _TMP_RE.match(name):
            rpt.stray_tmp.append(name)
            continue
        if name.endswith(".txt") or name.endswith(".json"):
            ctype = name.rsplit(".", 1)[0]
            cs = capture_files.setdefault(ctype, CaptureStat(capture_type=ctype))
            try:
                st = entry.stat()
            except OSError:
                continue
            cs.current_bytes += st.st_size
            cs.has_current = True
            newest_mtime = max(newest_mtime, st.st_mtime)

    # History
    history_dir = device_dir / "_history"
    if history_dir.is_dir():
        # group history files by capture type
        per_type_ts: Dict[str, Dict[str, int]] = {}     # type -> {ts: txt_bytes}
        per_type_bytes: Dict[str, int] = {}
        history_empty = True
        for h in history_dir.iterdir():
            if h.is_dir():
                continue
            history_empty = False
            if _TMP_RE.match(h.name):
                rpt.stray_tmp.append(f"_history/{h.name}")
                continue
            m = _HISTORY_RE.match(h.name)
            if not m:
                continue
            ctype = m.group("type")
            ts = m.group("ts")
            ext = m.group("ext")
            try:
                size = h.stat().st_size
            except OSError:
                continue
            per_type_bytes[ctype] = per_type_bytes.get(ctype, 0) + size
            if ext == "txt":
                per_type_ts.setdefault(ctype, {})[ts] = size

        rpt.empty_history = history_empty

        for ctype, ts_map in per_type_ts.items():
            cs = capture_files.setdefault(ctype, CaptureStat(capture_type=ctype))
            cs.history_bytes = per_type_bytes.get(ctype, 0)
            cs.history_versions = len(ts_map)
            sizes = list(ts_map.values())
            cs.median_snapshot_bytes = _median_int(sizes)
            stamps = sorted(_parse_ts(t) for t in ts_map if _parse_ts(t))
            if stamps:
                cs.oldest = stamps[0].isoformat()
                cs.newest = stamps[-1].isoformat()
                if len(stamps) >= 2:
                    deltas = [
                        (stamps[i + 1] - stamps[i]).total_seconds()
                        for i in range(len(stamps) - 1)
                    ]
                    cs.cadence_seconds = statistics.median(deltas)

        # capture types that exist only in history (no current) still counted above
        for ctype in per_type_bytes:
            if ctype not in per_type_ts:
                cs = capture_files.setdefault(ctype, CaptureStat(capture_type=ctype))
                cs.history_bytes = per_type_bytes[ctype]

    rpt.captures = capture_files
    rpt.total_bytes = sum(cs.total_bytes for cs in capture_files.values())

    if newest_mtime > 0:
        rpt.idle_days = round(
            (datetime.now(timezone.utc).timestamp() - newest_mtime) / 86400, 1
        )

    return rpt


def scan_tree(base_path: Path, site_filter: Optional[str] = None) -> Tuple[List[DeviceReport], List[str], List[str]]:
    """
    Walk {base}/{site}/{device}/ and footprint every device directory.

    Returns (device_reports, empty_device_dirs, empty_site_dirs).
    No DCIM reconciliation here — that's a separate, pure step.
    """
    reports: List[DeviceReport] = []
    empty_devices: List[str] = []
    empty_sites: List[str] = []

    if not base_path.exists():
        return reports, empty_devices, empty_sites

    for site_dir in sorted(base_path.iterdir()):
        if not site_dir.is_dir() or site_dir.name.startswith((".", "_")):
            continue
        if site_filter and site_dir.name != site_filter:
            continue

        site_has_device = False
        for device_dir in sorted(site_dir.iterdir()):
            if not device_dir.is_dir() or device_dir.name.startswith("_"):
                continue
            site_has_device = True

            rpt = _scan_device(site_dir.name, device_dir.name, device_dir)
            if rpt.total_bytes == 0 and not rpt.stray_tmp and not rpt.captures:
                empty_devices.append(str(device_dir))
            reports.append(rpt)

        if not site_has_device:
            empty_sites.append(str(site_dir))

    return reports, empty_devices, empty_sites


# ---------------------------------------------------------------------------
# Reconcile against DCIM
# ---------------------------------------------------------------------------

def reconcile(reports: List[DeviceReport], dcim_devices: List[dict]) -> None:
    """
    Classify each DeviceReport in place against the DCIM device list.

    dcim_devices: list of dicts with at least 'name', 'site_slug',
    'collection_enabled', 'status' (the shape of db.list_devices()).
    """
    if not dcim_devices:
        for r in reports:
            r.classification = CLASS_NO_DCIM
            r.reason = "DCIM inventory empty/unavailable — size only, no verdict"
        return

    by_site_name: Dict[Tuple[str, str], dict] = {}
    name_to_sites: Dict[str, List[str]] = {}
    for d in dcim_devices:
        site = d.get("site_slug") or ""
        name = d.get("name") or ""
        by_site_name[(site, name)] = d
        name_to_sites.setdefault(name, []).append(site)

    for r in reports:
        key = (r.site, r.device)
        match = by_site_name.get(key)
        if match is not None:
            r.dcim_status = match.get("status")
            if match.get("collection_enabled"):
                r.classification = CLASS_ACTIVE
                r.reason = "live DCIM device"
            else:
                r.classification = CLASS_DISABLED
                r.reason = "DCIM device exists but collection is disabled"
            continue

        other_sites = [s for s in name_to_sites.get(r.device, []) if s != r.site]
        if other_sites:
            r.classification = CLASS_WRONG_SITE
            r.elsewhere_sites = sorted(set(other_sites))
            r.reason = (
                f"name exists in DCIM under {', '.join(r.elsewhere_sites)}, "
                f"not '{r.site}' — likely a re-site/rename leftover"
            )
        else:
            r.classification = CLASS_ORPHAN
            r.reason = "no DCIM device by this name"


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def build_report(
    base_path: Path,
    reports: List[DeviceReport],
    empty_devices: List[str],
    empty_sites: List[str],
    dcim_count: int,
) -> CleanupReport:
    cr = CleanupReport(
        base_path=str(base_path),
        generated_at=datetime.now(timezone.utc).isoformat(),
        dcim_devices=dcim_count,
        device_count=len(reports),
    )

    cr.total_bytes = sum(r.total_bytes for r in reports)
    for r in reports:
        cr.class_counts[r.classification] = cr.class_counts.get(r.classification, 0) + 1
        cr.class_bytes[r.classification] = cr.class_bytes.get(r.classification, 0) + r.total_bytes
        if r.reclaimable:
            cr.reclaimable_bytes += r.total_bytes
        cr.stray_tmp_total += len(r.stray_tmp)
        if r.empty_history:
            cr.empty_history_dirs += 1

    cr.devices = reports
    cr.empty_device_dirs = empty_devices
    cr.empty_site_dirs = empty_sites

    # Per-capture rollup across ACTIVE devices only (the live retention surface)
    active = [r for r in reports if r.classification == CLASS_ACTIVE]
    agg: Dict[str, dict] = {}
    for r in active:
        for ctype, cs in r.captures.items():
            a = agg.setdefault(ctype, {
                "devices": 0, "bytes": 0, "versions": 0,
                "snap_sizes": [], "cadences": [], "spans": [],
            })
            a["devices"] += 1
            a["bytes"] += cs.total_bytes
            a["versions"] += cs.history_versions
            if cs.median_snapshot_bytes:
                a["snap_sizes"].append(cs.median_snapshot_bytes)
            if cs.cadence_seconds:
                a["cadences"].append(cs.cadence_seconds)
            if cs.oldest and cs.newest:
                o = _parse_ts_iso(cs.oldest)
                n = _parse_ts_iso(cs.newest)
                if o and n:
                    a["spans"].append((n - o).total_seconds())

    rollups: List[CaptureRollup] = []
    for ctype, a in agg.items():
        ru = CaptureRollup(
            capture_type=ctype,
            devices=a["devices"],
            total_bytes=a["bytes"],
            total_versions=a["versions"],
            avg_versions=round(a["versions"] / a["devices"], 1) if a["devices"] else 0,
            median_snapshot_bytes=_median_int(a["snap_sizes"]),
            cadence_seconds=statistics.median(a["cadences"]) if a["cadences"] else None,
            span_days=round(statistics.median(a["spans"]) / 86400, 1) if a["spans"] else None,
        )
        rollups.append(ru)
    rollups.sort(key=lambda x: x.total_bytes, reverse=True)
    cr.capture_rollup = rollups

    return cr


def _parse_ts_iso(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


def _human_cadence(sec: Optional[float]) -> str:
    if not sec:
        return "—"
    if sec < 90 * 60:
        return f"{sec / 60:.0f}m"
    if sec < 36 * 3600:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


def format_text(cr: CleanupReport, top: int = 20) -> str:
    out: List[str] = []
    w = out.append

    w("=" * 72)
    w("  NETLAPSE STORAGE CLEANUP — REPORT (dry run, nothing deleted)")
    w("=" * 72)
    w(f"  tree:          {cr.base_path}")
    w(f"  generated:     {cr.generated_at}")
    w(f"  dcim devices:  {cr.dcim_devices}")
    w(f"  on-disk dirs:  {cr.device_count}")
    w(f"  total size:    {_human(cr.total_bytes)}")
    w("")

    # --- Classification summary ---
    w("  RECONCILE vs DCIM")
    w("  " + "-" * 68)
    order = [CLASS_ACTIVE, CLASS_DISABLED, CLASS_WRONG_SITE, CLASS_ORPHAN, CLASS_NO_DCIM]
    for cls in order:
        cnt = cr.class_counts.get(cls, 0)
        if not cnt:
            continue
        b = cr.class_bytes.get(cls, 0)
        tag = "  <- reclaimable" if cls in RECLAIMABLE_CLASSES else ""
        w(f"    {cls:<12} {cnt:>4} dirs   {_human(b):>10}{tag}")
    w("")
    w(f"    reclaimable now (orphans):   {_human(cr.reclaimable_bytes)}")
    w(f"    stray .tmp files:            {cr.stray_tmp_total}")
    w(f"    empty _history dirs:         {cr.empty_history_dirs}")
    w(f"    empty device dirs:           {len(cr.empty_device_dirs)}")
    w(f"    empty site dirs:             {len(cr.empty_site_dirs)}")
    w("")

    # --- Orphans / review buckets ---
    flagged = [r for r in cr.devices
               if r.classification in (CLASS_ORPHAN, CLASS_WRONG_SITE, CLASS_DISABLED)]
    if flagged:
        flagged.sort(key=lambda r: (r.classification, -r.total_bytes))
        w("  FLAGGED DIRECTORIES")
        w("  " + "-" * 68)
        w(f"    {'class':<11}{'size':>9}  {'idle':>6}  device")
        for r in flagged:
            idle = f"{r.idle_days:.0f}d" if r.idle_days is not None else "—"
            w(f"    {r.classification:<11}{_human(r.total_bytes):>9}  {idle:>6}  "
              f"{r.site}/{r.device}")
            w(f"      └ {r.reason}")
        w("")

    # --- Top devices by size ---
    by_size = sorted(cr.devices, key=lambda r: r.total_bytes, reverse=True)[:top]
    if by_size:
        w(f"  TOP {len(by_size)} DEVICES BY SIZE")
        w("  " + "-" * 68)
        w(f"    {'size':>9}  {'class':<11} device")
        for r in by_size:
            w(f"    {_human(r.total_bytes):>9}  {r.classification:<11} "
              f"{r.site}/{r.device}")
        w("")

    # --- Capture rollup (retention-policy informer) ---
    if cr.capture_rollup:
        w("  CAPTURE FOOTPRINT (active fleet) — informs retention policy")
        w("  " + "-" * 68)
        w(f"    {'capture':<16}{'total':>9} {'devs':>5} {'avg/dev':>8} "
          f"{'med snap':>9} {'cadence':>8} {'span':>6}")
        for ru in cr.capture_rollup:
            w(f"    {ru.capture_type:<16}{_human(ru.total_bytes):>9} "
              f"{ru.devices:>5} {ru.avg_versions:>8.1f} "
              f"{_human(ru.median_snapshot_bytes):>9} "
              f"{_human_cadence(ru.cadence_seconds):>8} "
              f"{(str(ru.span_days) + 'd') if ru.span_days else '—':>6}")
        w("")
        w("    Note: 'avg/dev' is version count, capped by storage.max_versions.")
        w("    A high cadence + large 'med snap' = the capture eating the tree;")
        w("    its time 'span' is short precisely because the count cap fills fast.")
        w("")

    w("  No files were changed. This is a report only.")
    w("=" * 72)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Orchestration + CLI
# ---------------------------------------------------------------------------

def run_report(
    base_path: Path,
    dcim_devices: List[dict],
    site_filter: Optional[str] = None,
) -> CleanupReport:
    reports, empty_devices, empty_sites = scan_tree(base_path, site_filter)
    reconcile(reports, dcim_devices)
    return build_report(base_path, reports, empty_devices, empty_sites, len(dcim_devices))


def cli_cleanup(args: List[str]) -> int:
    """
    `python -m netlapse cleanup [--json] [--top N] [--site SLUG]`

    Reports only. Resolves storage path and DCIM db from config.yaml the
    same way the daemon does (NETLAPSE_CONFIG or ~/.netlapse/config.yaml).
    """
    import argparse

    p = argparse.ArgumentParser(prog="netlapse cleanup")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--top", type=int, default=20, help="largest N devices to list")
    p.add_argument("--site", default=None, help="limit scan to one site slug")
    ns = p.parse_args(args)

    from netlapse.app import load_config
    from netlapse.dcim.db_schema import NetlapseDB

    config = load_config()
    base_path = Path(
        (config.get("storage") or {}).get("path", "~/.netlapse/data")
    ).expanduser()
    db_path = Path(config.get("dcim_db", "~/.netlapse/netlapse.db")).expanduser()

    dcim_devices: List[dict] = []
    if db_path.exists():
        try:
            db = NetlapseDB(db_path)
            dcim_devices = db.list_devices()
        except Exception as e:
            logger.warning("Could not read DCIM (%s); reporting size only", e)
    else:
        logger.warning("No DCIM db at %s; reporting size only", db_path)

    cr = run_report(base_path, dcim_devices, site_filter=ns.site)

    if ns.json:
        print(json.dumps(asdict(cr), indent=2, default=str))
    else:
        print(format_text(cr, top=ns.top))
    return 0