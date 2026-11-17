#!/usr/bin/env python3
"""
Audit the resolved record key for every stored capture.

The diff test suite proves the engine is correct against fixtures. This
proves the key model matches what YOUR devices actually emit — the two are
different claims, and only this one covers a template that names a column
something nobody anticipated.

For each capture type found under the storage tree it reports:

  key         the composite key resolve_key_fields() picked
  collisions  records sharing that key within a single snapshot (the diff
              degrades to hash comparison whenever this is non-zero, so a
              non-zero count means "semantic diff is off for this type")
  document    routed to the text diff rather than the record engine

Anything under PROBLEMS at the end is a capture type whose semantic diff is
not currently trustworthy. Silence there is the result you want.

Usage:
    python audit_keys.py <storage-root> [--verbose]

The storage root is the directory holding <site>/<device>/ — whatever
`storage.path` resolves to in your config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

from netlapse.storage.diff import (
    detect_key_collisions,
    explain_key_selection,
    is_record_capture,
    resolve_key_fields,
)


def capture_type_of(path: Path) -> str:
    """
    Derive the capture type from a stored artifact filename.

    Current layout is {capture_type}.json for the live state and
    {capture_type}.{timestamp}.json under _history/.
    """
    return path.name.split(".", 1)[0]


def redact(path: Path, root: Path, enabled: bool) -> str:
    """
    Render a path with site/device components replaced by stable hashes.

    The audit is most useful on a real fleet, and a real fleet's paths carry
    site and device names. Hashing keeps distinct devices distinguishable in
    the report while removing the names themselves.
    """
    if not enabled:
        return str(path)
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    parts = list(rel.parts)
    for i, part in enumerate(parts[:-1]):
        digest = hashlib.sha256(part.encode()).hexdigest()[:8]
        parts[i] = f"<{digest}>"
    return "/".join(parts)


def load_records(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return (data or {}).get("records") or []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root", type=Path)
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="list every offending snapshot, not just a count")
    ap.add_argument("--redact", action="store_true",
                    help="replace site/device path components with stable "
                         "hashes so the output is safe to paste into an "
                         "issue, a commit message, or a chat")
    args = ap.parse_args()

    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 2

    keys: dict[str, set] = defaultdict(set)
    counts: dict[str, int] = defaultdict(int)
    empty: dict[str, int] = defaultdict(int)
    documents: set[str] = set()
    unkeyed: dict[str, int] = defaultdict(int)
    # capture_type -> {rejection reason -> count}, so a capture falling back
    # to hashing reports WHICH candidate failed and why, rather than leaving
    # the operator to guess at what to add.
    reasons: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    columns: dict[str, set] = defaultdict(set)
    collisions: dict[str, int] = defaultdict(int)
    examples: dict[str, list] = defaultdict(list)

    for path in args.root.rglob("*.json"):
        records = load_records(path)
        ctype = capture_type_of(path)
        counts[ctype] += 1

        if not records:
            empty[ctype] += 1
            continue

        if not is_record_capture(ctype, records, []):
            documents.add(ctype)
            continue

        key = resolve_key_fields(ctype, records, [])
        if not key:
            unkeyed[ctype] += 1
            for verdict in explain_key_selection(ctype, records):
                reasons[ctype][
                    f"{'+'.join(verdict['fields'])}: {verdict['reason']}"
                ] += 1
            if not explain_key_selection(ctype, records):
                # No candidate's fields were even present — the parse itself
                # is producing columns nothing knows about.
                columns[ctype] |= set(records[0].keys())
                reasons[ctype]["no candidate columns present"] += 1
            if args.verbose and len(examples[ctype]) < 5:
                examples[ctype].append(
                    f"  no key: {redact(path, args.root, args.redact)}")
            continue

        keys[ctype].add(tuple(key))

        found = detect_key_collisions(ctype, records)
        if found:
            collisions[ctype] += 1
            if args.verbose and len(examples[ctype]) < 5:
                worst = max(found, key=lambda c: c["count"])
                examples[ctype].append(
                    f"  {redact(path, args.root, args.redact)} — "
                    f"{len(found)} naive-key collision(s), "
                    f"worst {worst['key']} x{worst['count']}"
                )

    if not counts:
        print(f"no parsed artifacts found under {args.root}")
        return 0

    width = max(len(c) for c in counts)
    print(f"{'capture':<{width}}  {'snaps':>6}  {'key':<44}  naive-collide")
    print("-" * (width + 66))

    problems = []
    notes = []
    for ctype in sorted(counts):
        if ctype in documents:
            print(f"{ctype:<{width}}  {counts[ctype]:>6}  "
                  f"{'(document — text diff)':<44}  -")
            continue

        resolved = sorted(keys.get(ctype, set()))
        if len(resolved) > 1:
            # Several key shapes for one capture type is EXPECTED in a mixed
            # -vendor fleet — it is the vendor-variant candidates doing their
            # job, and each snapshot is only ever diffed against another
            # snapshot of the same device. Informational, not a problem.
            label = f"{len(resolved)} shapes: " + " | ".join(
                "+".join(k) for k in resolved
            )
        elif resolved:
            label = "+".join(resolved[0])
        else:
            label = "(none resolved)"

        col = collisions.get(ctype, 0)
        print(f"{ctype:<{width}}  {counts[ctype]:>6}  {label[:44]:<44}  {col or '-'}")

        if col:
            # These are collisions the PRESENCE-ONLY selection would have hit.
            # The live resolver rejects such a candidate and takes the next
            # one, so this is a signal about which columns are unsuitable —
            # not a statement that the diff is currently wrong.
            notes.append(
                f"{ctype}: {col} of {counts[ctype]} snapshots would collide "
                f"on the naive key — validation is routing around it"
            )
        if unkeyed.get(ctype):
            problems.append(
                f"{ctype}: {unkeyed[ctype]} snapshots resolved NO key "
                f"(diff degrades to hash comparison)"
            )
            for reason, n in sorted(
                reasons[ctype].items(), key=lambda kv: -kv[1]
            )[:4]:
                problems.append(f"        {n:>6} x  {reason}")
            if columns.get(ctype):
                problems.append(
                    f"               columns seen: "
                    f"{', '.join(sorted(columns[ctype])[:12])}"
                )
        if empty.get(ctype) == counts[ctype]:
            problems.append(
                f"{ctype}: every snapshot parsed to zero records — "
                f"check template selection for this capture"
            )

        for line in examples.get(ctype, []):
            print(line)

    print()
    if notes:
        print("NOTES (not failures)")
        for n in notes:
            print(f"  - {n}")
        print()
    if problems:
        print("PROBLEMS")
        for p in problems:
            print(p if p.startswith(" ") else f"  - {p}")
        return 1

    print("No problems: every record capture resolved a unique key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())