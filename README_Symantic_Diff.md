# Semantic Diff

Netlapse answers two different questions about a pair of snapshots, and keeps
them separate on purpose:

- **Text Diff** — *did the bytes change?* A line-aligned, config-aware diff of
  the raw captured output.
- **Semantic Diff** — *did the network change?* A record-level comparison of the
  parsed output that ignores volatile noise (age timers, counters) and reports
  only meaningful state changes.

The distinction is the whole point. An LLDP or OSPF capture changes on every
poll because a `Dead Time` or `age` field ticks, but the neighbor, the chassis,
and the adjacency state are identical. The text diff faithfully shows the timer
moving; the semantic diff says "No changes detected" — and both are correct for
the question they answer. For a state-backup daemon, the second question is the
one that matters at 3am, and it's the one Oxidized and RANCID can't answer
because they only diff text.

This works because parsing is the substrate: the semantic layer compares the
*parsed record* (neighbor, chassis, port, state), where volatile values are
separate fields it can drop — not text it has to pattern-match out.

---

## How it works

A diff request flows through three layers, all backend-agnostic (file and git
storage produce identical results).

### Semantic (record) diff

`storage/diff.py` compares two lists of parsed records — the `records` array
from a capture's parsed JSON. It resolves an identity key for the capture type,
matches records on that key, and reports `added` / `removed` / `changed`. A
`changed` entry carries the key plus a per-field `{from, to}` of what moved.

Records are keyed by the first field in `DIFF_KEY_FIELDS` that appears in the
data (e.g. `arp → ADDRESS`, `ospf → NEIGHBOR_ID`, `bgp → NEIGHBOR`/`BGP_NEIGH`).
If no key field resolves, it falls back to whole-record hash comparison — still
correct, but a modified record shows as a remove + add rather than a `changed`
(there's no stable identity to say "same record, changed").

### Text diff

`storage/config_diff.py` produces the side-by-side view: line alignment via
`difflib.SequenceMatcher` (with `autojunk=False`, which matters for long
configs), intra-line word highlighting on changed lines, fold-unchanged with
adjustable context, and optional masking of volatile lines (config-change
timestamps) against the comparison key — never the displayed text. The raw
unified diff is also retained for Oxidized compatibility.

### Field exclusion (volatility)

The semantic diff's "ignore the noise" behavior is policy, not algorithm, so it
lives in YAML, not code. `registry/volatility.py` loads `volatility.yaml` and,
for a given capture type and the columns actually present, resolves which fields
to exclude by default:

```
(heuristic glob matches on the columns)
  ∪ (explicit `volatile` for the type)
  − (explicit `keep` for the type)
  − key fields            ← a record's identity is never excludable
∩ the columns that actually exist
```

The diff engine itself excludes exactly the fields it is handed and decides
nothing about which those should be — `diff_records(..., ignore_fields=...)`.

### Collection hygiene

`ssh/client.py` strips the echoed command and trailing prompt from each
capture before storage, so the head and tail of a snapshot are deterministic.
Without this, the first one or two lines drift run-to-run (`show run` vs
`host#show run`) and register as a phantom diff on every device forever.

---

## Configuration: `volatility.yaml`

Lives in the definitions directory alongside `captures.yaml` and `jobs.yaml`.
Edit and restart — it is not hot-reloaded.

```yaml
version: 1

# Global name heuristics — globs (* and ?), case-insensitive, matched against
# every column of every capture type. The zero-curation baseline: a new
# template's timers and counters classify themselves on first collection.
heuristics:
  - AGE
  - "*_TIME"        # DEAD_TIME, HOLD_TIME, LAST_TIME, ...
  - UPTIME
  - "LAST_*"
  - "*_PACKETS"
  - "*_BYTES"
  - "*_RATE"
  # ...

# Explicit per-capture-type rules, authoritative on top of heuristics.
captures:
  arp:
    volatile: [AGE]          # exclude by default (in addition to heuristic hits)
  ospf:
    keep: [STATE]            # never exclude, even if a heuristic would match
```

`volatile` adds fields to the default exclusion set; `keep` rescues a field a
heuristic would otherwise hide. Use `keep` to protect real signal you can name —
e.g. OSPF `STATE` (FULL/2WAY/EXSTART) — as insurance against a future broadened
heuristic.

Prefer heuristics for the obvious-everywhere cases (timers, counters) and
explicit `volatile` for the targeted ones. Resist over-broadening a glob
(`*TIME` would also eat `LIFETIME`, `RUNTIME`, …) — explicit-per-capture is the
scalpel.

**Missing file** → no default exclusions (every field compared). **Invalid
file** → logged and treated as empty, so a typo never blocks diffing. The
per-diff UI chips always override whatever the file resolves; this only sets the
default a fresh diff opens with.

### Pointing the daemon at the file

The daemon reads definitions from, in order: `definitions_dir` in
`config.yaml`, else the directory of `$NETLAPSE_CONFIG`, else `~/.netlapse/`.
Both the registry sync *and* the volatility loader resolve from the same place,
pinned once at startup — so `volatility.yaml` must sit in the same directory
your `captures.yaml` is read from.

```yaml
# ~/.netlapse/config.yaml  (top-level key, sibling to dcim_db / storage)
definitions_dir: /home/you/PycharmProjects/netlapse/netlapse/definitions
```

If captures are collecting but volatility isn't being applied, this is almost
always the cause: captures were synced from one directory while the loader read
another. Setting `definitions_dir` aligns them.

---

## API

```
GET /api/v1/devices/{device_id}/diff
```

| Param          | Default  | Meaning                                                        |
|----------------|----------|----------------------------------------------------------------|
| `capture_type` | `config` | which capture to diff                                          |
| `from_commit`  | —        | older version id (omit → newest-history vs current)            |
| `to_commit`    | —        | newer version id                                               |
| `mask`         | `false`  | mask volatile *lines* in the text diff (timestamps, etc.)      |
| `ignore`       | —        | comma-separated columns to exclude from the *semantic* diff    |

`ignore` semantics: **absent** → apply `volatility.yaml` defaults; **present**
(even empty, `?ignore=`) → an explicit choice, where empty means "compare every
field". Key fields are never excluded regardless.

Response includes the semantic result (`added`, `removed`, `changed`), the text
diffs (`raw_diff` unified string for Oxidized compat, `aligned` `{rows, summary}`
for the side-by-side viewer), and the field-exclusion context: `schema` (all
columns), `key_fields` (never excludable), `ignored` (effective set applied).
The UI builds the "Ignore fields" chips from `schema` minus `key_fields`,
striking through whatever is in `ignored`.

---

## CLI: validate & inspect

```bash
# Validate captures, jobs, AND volatility.yaml without touching the DB
python -m netlapse definitions validate --dir ./netlapse/definitions/

# Full listing, including the resolved volatility policy
python -m netlapse definitions list --dir ./netlapse/definitions/
```

**Use the `--dir` flag, not a bare path.** A positional path is silently
absorbed by the `adopt` slug argument and ignored, so the command falls back to
`~/.netlapse/` and appears empty even when your real directory is populated.

`validate` reports counts and exits non-zero on a malformed file:

```
OK — 8 capture(s), 8 binding(s), 14 volatility heuristic(s), 2 volatility rule(s)
```

`list` prints the resolved heuristics and per-capture rules under `Volatility:`.
Confirm `*_TIME` (and friends) appear there — that line is what makes
`DEAD_TIME`, `HOLD_TIME`, etc. go quiet without per-capture config.

---

## Troubleshooting

**Volatility not applied even though the YAML looks right.**
Run `definitions list --dir <your dir>` and check the `Volatility:` block. If
the `heuristics:` line is empty, the loaded file is a partial one. If the file
is correct but the daemon ignores it, `definitions_dir` isn't set (or doesn't
match where captures are read) — captures and the volatility loader are
resolving different directories. Set `definitions_dir` and restart.

**A field that should match a heuristic isn't excluded.**
Heuristics need a restart (the catalog is cached for the process lifetime, and
the definitions dir is pinned at startup). Also check the glob: `*_TIME` matches
`DEAD_TIME` but **not** `DEADTIME` — there must be a literal `_TIME` suffix. If
the column lacks the separator, add an explicit `volatile` entry or adjust the
pattern.

**"ARP is clean but OSPF isn't" (or one type works, another doesn't).**
Classic heuristics-less file: the explicit `arp`/`mac` rules fire but the
`heuristics:` block is absent, so anything relying on a pattern (`*_TIME`)
falls through. Replace with the full shipped file.

**Changed records show as remove + add instead of `changed`.**
The key field didn't resolve — your template's identity column isn't in
`DIFF_KEY_FIELDS` for that capture type, so it fell back to hash matching. Field
exclusion still works (hash mode honors `ignore_fields`), but add the real
column name to `DIFF_KEY_FIELDS` to get proper per-field change reporting.

**Phantom diff on the first line(s) of every snapshot.**
Command-echo/prompt leaking into stored output. Fixed in `ssh/client.py`; if it
reappears, the strip isn't running. Note the first post-fix collection shows one
real one-time diff as the head normalizes — expected, not a regression.

---

## File map

| File                          | Responsibility                                             |
|-------------------------------|------------------------------------------------------------|
| `storage/diff.py`             | record comparison engine, `DIFF_KEY_FIELDS`, schema helper |
| `storage/config_diff.py`      | line-aligned, config-aware text diff                       |
| `storage/backend.py`          | `_structured_with_schema`, `_build_aligned` (shared)       |
| `storage/file_backend.py`     | file-mode `diff()`                                         |
| `storage/git_backend.py`      | git-mode `diff()`                                          |
| `registry/volatility.py`      | loads `volatility.yaml`, resolves default exclusions       |
| `definitions/volatility.yaml` | the volatility policy (heuristics + per-capture rules)     |
| `api/native.py`               | `GET /devices/{id}/diff`                                   |
| `ssh/client.py`               | command-echo / prompt stripping at collection              |
| `web/static/js/views/device.js` | diff rendering, chips, controls                         |

---

## Extending

**New capture type, proper keying** — add its identity column to
`DIFF_KEY_FIELDS` in `storage/diff.py` (must equal the TextFSM column name).

**New volatile fields** — add to `volatility.yaml`. A heuristic if it's an
everywhere-pattern; an explicit `volatile` if it's specific to one type. No code
change, no redeploy — edit and restart.

**Future: learned volatility** — because defaults resolve through a single
catalog object with a stable `resolve()` interface, an empirical churn audit
(walk consecutive snapshots, measure per-field change frequency) can write
suggested `volatile` entries into this same YAML — or later a Reference Data
table behind the identical interface — without touching the diff engine.