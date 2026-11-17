#!/usr/bin/env python3
"""
netlapse-parse-test — Single-device collect + parse with verbose output.

Collects from one device, runs the parser step-by-step with full
diagnostic output, and optionally stores the result. Built for
fast iteration when troubleshooting template matching.

Usage:
    python -m netlapse.parse_test border01           # by device name
    python -m netlapse.parse_test border01 --cmd "show ip bgp summary"
    python -m netlapse.parse_test border01 --capture arp
    python -m netlapse.parse_test border01 --no-store  # parse only, don't write
    python -m netlapse.parse_test border01 --raw-file /tmp/border01-arp.txt  # skip SSH, parse file

Environment:
    NETLAPSE_VAULT_PASSWORD — required for SSH collection
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Bootstrap ────────────────────────────────────────────────────
# Add parent to path so this works as `python -m netlapse.parse_test`
# or as `python netlapse/parse_test.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netlapse.dcim import NetlapseDB
from netlapse.storage import create_backend
from netlapse.parser.engine import ParseEngine
from netlapse.parser.tfsm_fire import TextFSMAutoEngine


def banner(msg):
    print(f"\n{'═' * 60}")
    print(f"  {msg}")
    print(f"{'═' * 60}")


def section(msg):
    print(f"\n── {msg} {'─' * max(0, 54 - len(msg))}")


def main():
    parser = argparse.ArgumentParser(
        description="Single-device collect + parse diagnostic"
    )
    parser.add_argument("device", help="Device name (hostname match)")
    parser.add_argument("--cmd", default=None,
                        help="CLI command to run (default: from job or 'show ip arp')")
    parser.add_argument("--capture", default="arp",
                        help="Capture type label (default: arp)")
    parser.add_argument("--no-store", action="store_true",
                        help="Parse only, don't write to storage")
    parser.add_argument("--raw-file", default=None,
                        help="Skip SSH, parse this file instead")
    parser.add_argument("--min-score", type=float, default=15.0,
                        help="Minimum parse score (default: 15.0)")
    parser.add_argument("--db", default="~/.netlapse/netlapse.db",
                        help="DCIM database path")
    parser.add_argument("--template-db", default=None,
                        help="tfsm_templates.db path (auto-detected)")
    parser.add_argument("--emulate", action="store_true",
                        help="Use NetEmulate for SSH")
    args = parser.parse_args()

    banner(f"Parse test: {args.device}")

    # ── Open DCIM ────────────────────────────────────────────────
    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"ERROR: DCIM database not found: {db_path}")
        sys.exit(1)

    db = NetlapseDB(db_path)
    print(f"DCIM: {db_path}")

    # ── Find device ──────────────────────────────────────────────
    section("Device lookup")
    device = db.get_device_by_name(args.device)
    if not device:
        # Try partial match
        all_devices = db.list_devices()
        matches = [d for d in all_devices if args.device.lower() in d["name"].lower()]
        if len(matches) == 1:
            device = db.get_device_by_id(matches[0]["id"])
        elif matches:
            print(f"Multiple matches for '{args.device}':")
            for m in matches[:10]:
                print(f"  {m['name']} ({m.get('primary_ip4', '?')})")
            sys.exit(1)
        else:
            print(f"No device matching '{args.device}'")
            sys.exit(1)

    platform_profile = device.get("platform_profile", "") or ""
    print(f"  Name:     {device['name']}")
    print(f"  IP:       {device.get('primary_ip4', '?')}")
    print(f"  Platform: {device.get('platform_name', '?')} (profile: {platform_profile})")
    print(f"  Site:     {device.get('site_slug', '?')}")

    # ── Determine command ────────────────────────────────────────
    command = args.cmd
    if not command:
        # Default commands per capture type
        cmd_map = {
            "config": "show running-config",
            "arp": "show ip arp",
            "bgp": "show ip bgp summary",
            "interfaces": "show interfaces",
            "routes": "show ip route",
        }
        command = cmd_map.get(args.capture, "show ip arp")
    print(f"  Command:  {command}")
    print(f"  Capture:  {args.capture}")

    # ── Get raw output ───────────────────────────────────────────
    raw_text = None

    if args.raw_file:
        section(f"Reading raw output from {args.raw_file}")
        with open(args.raw_file) as f:
            raw_text = f.read()
        print(f"  {len(raw_text)} bytes read")
    else:
        section("SSH collection")

        # Vault
        from netlapse.vault.bridge import get_vault, resolve_shared_credentials
        vault = get_vault(auto_unlock=True)
        if not vault.is_unlocked:
            print("ERROR: Vault locked — set NETLAPSE_VAULT_PASSWORD")
            sys.exit(1)
        creds = resolve_shared_credentials(vault)
        print(f"  Credentials resolved")

        # Emulation
        if args.emulate:
            from netlapse.ssh.emulation import enable_emulation
            count = enable_emulation()
            print(f"  Emulation enabled: {count} IPs")

        # Collect
        from netlapse.ssh.executor import collect_device
        print(f"  Connecting to {device.get('primary_ip4')}...")

        t0 = time.time()
        result = collect_device(
            device=device,
            commands=[command],
            capture_type=args.capture,
            credentials=creds,
        )
        elapsed = time.time() - t0

        if not result.success:
            print(f"  FAILED: {result.error_category} — {result.error_message}")
            sys.exit(1)

        snap = result.snapshots[0]
        raw_text = snap.raw_text
        print(f"  OK: {len(raw_text)} bytes in {elapsed:.1f}s")

    # ── Show raw output (truncated) ──────────────────────────────
    section("Raw output (first 600 chars)")
    print(raw_text[:600])
    if len(raw_text) > 600:
        print(f"  ... ({len(raw_text)} total bytes)")

    # ── Initialize parse engine ──────────────────────────────────
    section("Parse engine")
    try:
        engine = ParseEngine(
            db_path=args.template_db,
            min_score=args.min_score,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    print(f"  Templates: {engine.template_count}")
    print(f"  Min score: {engine.min_score}")

    # ── Step 1: Clean output ─────────────────────────────────────
    section("Step 1: Clean output")
    cleaned = ParseEngine._clean_output(raw_text)
    print(f"  Raw:     {len(raw_text)} bytes, {raw_text.count(chr(10))} lines")
    print(f"  Cleaned: {len(cleaned)} bytes, {cleaned.count(chr(10))} lines")
    print()
    print("  Cleaned output (first 400 chars):")
    for line in cleaned[:400].split("\n"):
        print(f"    | {line}")
    if len(cleaned) > 400:
        print(f"    ... ({len(cleaned)} total)")

    # ── Step 2: Build filter string ──────────────────────────────
    section("Step 2: Filter string")
    filter_str = ParseEngine._build_filter(platform_profile, command)
    print(f"  Platform:  {platform_profile}")
    print(f"  Command:   {command}")
    print(f"  Filter:    {filter_str}")

    # Show what templates match this filter
    matching = engine.list_templates(filter_str)
    print(f"  Matching templates ({len(matching)}):")
    for t in matching:
        print(f"    {t}")

    # ── Step 3: Parse (specific filter) ──────────────────────────
    section("Step 3: Parse (specific filter)")
    t0 = time.time()
    result1 = engine.parse(raw_text, platform_profile, command)
    t1 = time.time()
    print(f"  Time:     {t1 - t0:.2f}s")
    print(f"  Success:  {result1.success}")
    print(f"  Template: {result1.template}")
    print(f"  Score:    {result1.score}")
    print(f"  Records:  {result1.record_count}")
    if result1.error:
        print(f"  Error:    {result1.error}")

    if result1.success and result1.records:
        print(f"  Fields:   {list(result1.records[0].keys())}")
        print(f"  First:    {result1.records[0]}")

    # ── Step 4: Vendor fallback (if needed) ──────────────────────
    if not result1.success:
        section("Step 4: Vendor fallback")
        vendor = platform_profile.split("_")[0] if platform_profile else ""
        print(f"  Vendor filter: {vendor}")

        vendor_templates = engine.list_templates(vendor)
        print(f"  Matching templates ({len(vendor_templates)}):")
        for t in vendor_templates[:15]:
            print(f"    {t}")
        if len(vendor_templates) > 15:
            print(f"    ... and {len(vendor_templates) - 15} more")

        t0 = time.time()
        result2 = engine.parse_vendor_fallback(raw_text, platform_profile)
        t2 = time.time()
        print(f"\n  Time:     {t2 - t0:.2f}s")
        print(f"  Success:  {result2.success}")
        print(f"  Template: {result2.template}")
        print(f"  Score:    {result2.score}")
        print(f"  Records:  {result2.record_count}")
        if result2.error:
            print(f"  Error:    {result2.error}")

        if result2.success and result2.records:
            print(f"  Fields:   {list(result2.records[0].keys())}")
            print(f"  First:    {result2.records[0]}")

        final = result2 if result2.success else result1
    else:
        final = result1

    # ── Step 5: Store (optional) ─────────────────────────────────
    if final.success and not args.no_store and not args.raw_file:
        section("Step 5: Store")
        from netlapse.storage.backend import Snapshot
        from netlapse.app import load_config as load_app_config
        from datetime import datetime, timezone

        snap = result.snapshots[0]
        snap.parsed_data = {"records": final.records}
        snap.template_name = final.template
        snap.template_score = final.score
        snap.raw_text = snap.raw_text.replace("\r", "")

        # Use the same config the app uses
        app_config = load_app_config()
        storage = create_backend(app_config)
        storage.initialize()
        storage.store_batch([snap])

        # Verify files on disk
        base = Path(storage.base_path).expanduser()
        site = snap.site or "unknown"
        raw_path = base / site / snap.device / f"{args.capture}.txt"
        json_path = base / site / snap.device / f"{args.capture}.json"

        print(f"  Stored: {device['name']}/{args.capture}")
        print(f"    Storage base: {base}")
        print(f"    Raw:    {raw_path} ({'EXISTS' if raw_path.exists() else 'MISSING'}) {len(snap.raw_text)} bytes")
        print(f"    Parsed: {json_path} ({'EXISTS' if json_path.exists() else 'MISSING'}) {final.record_count} records")
        print(f"    Template: {final.template}")
    elif not final.success:
        section("Result: NO MATCH")
        print("  No template matched above the minimum score.")
        print("  Try: --min-score 1 to see low-confidence matches")
    else:
        section("Result: PARSED (not stored)")
        print(f"  Template: {final.template}")
        print(f"  Score:    {final.score}")
        print(f"  Records:  {final.record_count}")

    banner("Done")


if __name__ == "__main__":
    main()