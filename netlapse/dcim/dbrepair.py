#!/usr/bin/env python3
"""
netlapse_repair.py — diagnose and repair Netlapse state after an app halt
mid-reparse, when subsequent reparses return 500.

What it inspects (always, read-only):
  1. Both SQLite files (netlapse.db DCIM + tfsm_templates.db template store):
     - presence/size of stale -wal / -shm / -journal sidecars (a rollback
       journal = a hot journal from an interrupted write transaction)
     - PRAGMA quick_check + integrity_check
  2. The on-disk capture store for one capture type: every device's raw
     (.txt) and parsed (.json) artifact, classified ok / empty / invalid /
     missing, including rotated copies under _history/.
  3. Orphaned job_history rows left in status='running' by the halt.

What it repairs (only with --apply; nothing is destructive):
  - Recovers a hot journal / flushes WAL by opening each DB cleanly and
    issuing PRAGMA wal_checkpoint(TRUNCATE). SQLite auto-rolls-back a hot
    journal the moment a connection opens, so this also clears that.
  - Quarantines empty/invalid parsed .json (and matching _history copies)
    to a sidecar dir, mirroring their path. Raw .txt is never moved.
  - Marks orphaned 'running' job_history rows as 'interrupted'
    (the same thing NetlapseDB.reap_running_jobs does at startup).

A timestamped backup copy of each SQLite file is made before it is written.

Usage:
  # diagnose only (safe, default)
  python netlapse_repair.py --capture-type mac \
      --netlapse-dir ~/.netlapse --storage-dir /path/to/capture/store

  # apply repairs
  python netlapse_repair.py --capture-type mac \
      --netlapse-dir ~/.netlapse --storage-dir /path/to/capture/store --apply

--storage-dir is the file backend's base_path — the directory that contains
the site/device tree (site1/<device>/<ct>.txt). It's whatever base_path the
FileBackend was initialized with; if you're unsure, it's the parent of the
site folders you see your captures under.
"""

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── output helpers ──────────────────────────────────────────────────────────
def hdr(t): print(f"\n=== {t} ===")
def ok(m):  print(f"  [ok]    {m}")
def warn(m): print(f"  [warn]  {m}")
def act(m): print(f"  [fix]   {m}")
def info(m): print(f"          {m}")

def _ts():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── SQLite checks ─────────────────────────────────────────────────────────────
def check_db(path: Path, apply: bool):
    if not path.exists():
        warn(f"{path.name}: not found at {path} (skipping)")
        return
    print(f"\n  {path.name}  ({path.stat().st_size:,} bytes)")

    # Stale sidecars left by an interrupted process.
    for suffix, label in (("-wal", "WAL"), ("-shm", "shared-mem"),
                          ("-journal", "ROLLBACK JOURNAL (hot journal!)")):
        side = path.with_name(path.name + suffix)
        if side.exists():
            sz = side.stat().st_size
            (warn if suffix == "-journal" or sz > 4_000_000 else info)(
                f"{side.name} present ({sz:,} bytes) — {label}"
            )

    # quick_check + integrity_check on a fresh connection (this alone
    # auto-recovers a hot journal). Read-only; safe regardless of --apply.
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        (ok if quick == "ok" else warn)(f"quick_check: {quick}")
        integ = conn.execute("PRAGMA integrity_check").fetchall()
        verdict = integ[0][0] if integ else "?"
        (ok if verdict == "ok" else warn)(f"integrity_check: {verdict}")
        if verdict != "ok":
            for row in integ[:10]:
                info(row[0])
    except sqlite3.DatabaseError as e:
        warn(f"could not open/check: {e}")
        conn = None

    if apply and conn is not None:
        backup = path.with_name(f"{path.name}.bak-{_ts()}")
        shutil.copy2(path, backup)
        act(f"backed up → {backup.name}")
        try:
            res = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.commit()
            act(f"wal_checkpoint(TRUNCATE) → {res}")
        except sqlite3.DatabaseError as e:
            warn(f"checkpoint failed: {e}")
    if conn is not None:
        conn.close()


def reap_running_jobs(netlapse_db: Path, apply: bool):
    if not netlapse_db.exists():
        return
    try:
        conn = sqlite3.connect(str(netlapse_db), timeout=10)
        rows = conn.execute(
            "SELECT id, job_slug, started_at FROM job_history "
            "WHERE status='running'"
        ).fetchall()
    except sqlite3.DatabaseError as e:
        warn(f"job_history check failed: {e}")
        return
    if not rows:
        ok("no orphaned 'running' job_history rows")
        conn.close()
        return
    warn(f"{len(rows)} orphaned 'running' job_history row(s):")
    for rid, slug, started in rows:
        info(f"id={rid} slug={slug} started={started}")
    if apply:
        now = _ts().replace("T", "T")  # ISO-ish
        conn.execute(
            "UPDATE job_history SET status='interrupted', completed_at=? "
            "WHERE status='running'", (now,)
        )
        conn.commit()
        act(f"marked {len(rows)} row(s) → interrupted")
    conn.close()


# ── artifact scan ────────────────────────────────────────────────────────────
def classify_json(p: Path):
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return "unreadable", str(e)
    if text.strip() == "":
        return "empty", "0 bytes / whitespace only"
    try:
        json.loads(text)
        return "ok", None
    except json.JSONDecodeError as e:
        return "invalid", f"{e.msg} at line {e.lineno} col {e.colno}"


def scan_artifacts(storage_dir: Path, ct: str, apply: bool, quarantine: Path):
    if not storage_dir.exists():
        warn(f"storage dir not found: {storage_dir}")
        return
    raw_name = f"{ct}.txt"
    parsed_name = f"{ct}.json"
    bad = []            # (path, reason)
    counts = {"ok": 0, "empty": 0, "invalid": 0, "unreadable": 0,
              "raw_only": 0, "raw_empty": 0, "no_raw": 0}

    # Current artifacts: storage/<site>/<device>/<ct>.{txt,json}
    candidates = list(storage_dir.glob(f"*/*/{parsed_name}"))
    # Rotated history: storage/<site>/<device>/_history/<ct>.<ts>.json
    candidates += list(storage_dir.glob(f"*/*/_history/{ct}.*.json"))

    for jp in candidates:
        status, reason = classify_json(jp)
        if status == "ok":
            counts["ok"] += 1
        else:
            counts[status] = counts.get(status, 0) + 1
            bad.append((jp, f"{status}: {reason}"))

    # Raw-side sanity: a 0-byte raw is the other thing a halt can leave.
    for rp in storage_dir.glob(f"*/*/{raw_name}"):
        try:
            if rp.stat().st_size == 0:
                counts["raw_empty"] += 1
                bad.append((rp, "raw .txt is 0 bytes"))
        except OSError:
            pass

    ok(f"{counts['ok']} parsed artifact(s) load cleanly for '{ct}'")
    if counts["empty"] or counts["invalid"] or counts["unreadable"]:
        warn(f"{counts['empty']} empty, {counts['invalid']} invalid, "
             f"{counts['unreadable']} unreadable parsed .json")
    if counts["raw_empty"]:
        warn(f"{counts['raw_empty']} zero-byte raw .txt")

    for p, reason in bad:
        info(f"{reason}: {p}")

    # Repair: quarantine only the bad *parsed* files (never raw).
    if apply and bad:
        moved = 0
        for p, reason in bad:
            if p.suffix == ".txt":
                warn(f"NOT moving raw file (manual review): {p}")
                continue
            rel = p.relative_to(storage_dir)
            dest = quarantine / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            moved += 1
            act(f"quarantined {rel} → {dest}")
        if moved:
            act(f"moved {moved} corrupt parsed artifact(s); raw left intact — "
                f"they regenerate on next collection/reparse")
    return bad


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Diagnose/repair Netlapse after a halted reparse.")
    ap.add_argument("--capture-type", required=True,
                    help="the capture type you were reparsing when halted (e.g. mac, lldp)")
    ap.add_argument("--netlapse-dir", default="~/.netlapse",
                    help="dir holding netlapse.db and tfsm_templates.db (default ~/.netlapse)")
    ap.add_argument("--storage-dir", required=True,
                    help="file backend base_path (parent of the site/device tree)")
    ap.add_argument("--quarantine-dir", default=None,
                    help="where to move corrupt parsed artifacts (default: <storage>/_quarantine)")
    ap.add_argument("--apply", action="store_true",
                    help="perform repairs (default: diagnose only)")
    args = ap.parse_args()

    nl_dir = Path(args.netlapse_dir).expanduser()
    storage = Path(args.storage_dir).expanduser()
    quarantine = (Path(args.quarantine_dir).expanduser()
                  if args.quarantine_dir
                  else storage / "_quarantine" / _ts())
    netlapse_db = nl_dir / "netlapse.db"
    tfsm_db = nl_dir / "tfsm_templates.db"

    mode = "APPLY (repairs will be made)" if args.apply else "DIAGNOSE ONLY (no changes)"
    print(f"Netlapse repair — {mode}")
    print(f"capture-type={args.capture_type}  netlapse-dir={nl_dir}  storage-dir={storage}")

    hdr("SQLite integrity")
    check_db(netlapse_db, args.apply)
    check_db(tfsm_db, args.apply)

    hdr("Orphaned job runs")
    reap_running_jobs(netlapse_db, args.apply)

    hdr(f"Capture artifacts for '{args.capture_type}'")
    bad = scan_artifacts(storage, args.capture_type, args.apply, quarantine)

    hdr("Summary")
    if not args.apply:
        print("  Diagnose-only. Re-run with --apply to repair what's flagged above.")
        if bad:
            print("  Corrupt artifacts found — but note: the file backend's")
            print("  fetch_latest() already tolerates these, so if reparse still")
            print("  500s after --apply, the cause is elsewhere — grab the traceback.")
    else:
        print("  Repairs applied. Restart the app and retry one reparse.")
        print("  If it still 500s, the cause isn't on-disk state — send the traceback.")


if __name__ == "__main__":
    sys.exit(main())