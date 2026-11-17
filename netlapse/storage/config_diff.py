"""
config_diff.py — line-aligned, config-aware diff built on difflib.

Two-stage design:
  1. normalize  -> a comparison KEY per line (never deletes lines; keeps a 1:1
     index mapping back to the ORIGINAL text so display is always the original)
  2. align      -> walk SequenceMatcher.get_opcodes() into side-by-side rows

The API returns aligned JSON rows. No HTML, no rendering decisions on the
server. The frontend stays dumb: map rows to two columns, color by type.

Row contract:
  {
    "type": "equal" | "replace" | "delete" | "insert",
    "left":  {"num": int|None, "text": str|None, "segments": [...optional]},
    "right": {"num": int|None, "text": str|None, "segments": [...optional]}
  }
Gap cells (one side of an insert/delete) have num=None, text=None.
`segments` is present only on `replace` rows and carries intra-line
word-level highlighting: a list of {"text": str, "changed": bool}.
"""

import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class NormalizeOptions:
    # Per-line transforms applied to the COMPARISON KEY only. Display is always
    # the original line, so none of these mangle what the operator sees.
    rstrip: bool = True                       # ignore trailing whitespace
    ignore_case: bool = False                 # off: case is meaningful in configs
    collapse_whitespace: bool = False         # off: indentation = hierarchy in IOS
    ignore_blank_lines: bool = False          # treat blank/ws-only lines as equal
    mask_patterns: List[str] = field(default_factory=list)  # regexes -> '' (volatile lines)
    drop_patterns: List[str] = field(default_factory=list)  # regexes -> line removed anywhere
    head_drop_patterns: List[str] = field(default_factory=list)  # regexes -> line removed, leading frame only


# NOTE: config-line volatile masks (the "mask volatile lines" set) are policy,
# not engine, so they live in the volatility registry (BASELINE_LINE_MASKS +
# volatility.yaml `line_masks`), resolved per-capture and passed in via
# NormalizeOptions.mask_patterns. This engine stays generic — it applies
# whatever masks it's handed. PREAMBLE_DROPS below is different: it's structural
# CLI framing (not operator-tunable noise), so it stays with the engine.

# CLI session framing — the prompt/command echo and RE-context lines a device
# emits BEFORE and AFTER the real output. These are NOT state, and (unlike a
# masked timestamp) they routinely appear on only ONE side of a diff — e.g.
# older snapshots captured the framing, newer clean-preamble collections
# didn't. Masking (blanking the key) leaves a one-sided empty line that still
# diffs as a delete, so framing must be DROPPED from the sequence, not masked.
# This is session noise, not volatility, so it's scrubbed unconditionally
# rather than behind the volatile-line toggle.
PREAMBLE_DROPS = [
    r"^\{(?:master|backup|line-card\d+|fpc\d+|re\d+|node\d+)[^}]*\}\s*$",  # Junos RE context (top and trailing)
    r"^\S+@\S+>\s",                                                        # Junos op-mode prompt + command echo
]

# Session HEADER framing — the pagination-disable shotgun (ssh/client.py fires
# every vendor's variant and lets the wrong ones fail harmlessly) plus the
# acknowledgements the device echoes back. Same problem as PREAMBLE_DROPS: not
# state, and one-sided across a collector change, so a capture taken before the
# shotgun landed diffs against one taken after and every header line reads as a
# deletion.
#
# These are matched ONLY in the leading frame — the run of blank/framing lines
# before the first line of real output — not anywhere in the file. That
# restriction is deliberate: 'no page' and 'terminal length 0' are unambiguous
# as a session header, but 'no <something>' is also ordinary config syntax, and
# a pattern that fires anywhere would silently eat a real config line further
# down. Anchoring to the header makes them safe to match loosely.
SESSION_HEAD_DROPS = [
    # Pagination-disable command echoes (mirrors PAGINATION_DISABLE_SHOTGUN).
    r"^terminal\s+(?:length|width)\s+\d+\s*$",          # IOS / IOS-XE / NX-OS / EOS / Dell
    r"^terminal\s+pager\s+\d+\s*$",                     # Cisco ASA
    r"^terminal\s+more\s+disable\s*$",                  # Extreme VOSS
    r"^set\s+cli\s+screen-(?:length|width)\s+\d+\s*$",  # Junos
    r"^screen-length\s+(?:\d+\s+temporary|disable)\s*$",# Huawei VRP / HP Comware
    r"^disable\s+clipaging\s*$",                        # Extreme EXOS
    r"^no\s+page\s*$",                                  # HP ProCurve
    r"^set\s+cli\s+pager\s+off\s*$",                    # Palo Alto
    # Acknowledgements the device writes back.
    r"^Screen\s+(?:length|width)\s+set\s+to\s+\d+\s*$", # Junos
    r"^pagination\s+disabled\s*$",                      # misc
]


def _normalize_line(line: str, opts: NormalizeOptions) -> str:
    s = line
    for pat in opts.mask_patterns:
        s = re.sub(pat, "", s)
    if opts.ignore_case:
        s = s.lower()
    if opts.collapse_whitespace:
        s = re.sub(r"\s+", " ", s).strip()
    elif opts.rstrip:
        s = s.rstrip()
    if opts.ignore_blank_lines and s.strip() == "":
        s = ""
    return s


_TOKEN_RE = re.compile(r"\s+|\S+")


def _merge_segments(segs):
    """Collapse adjacent segments that share a `changed` flag."""
    out = []
    for seg in segs:
        if out and out[-1]["changed"] == seg["changed"]:
            out[-1]["text"] += seg["text"]
        else:
            out.append(dict(seg))
    return out


def _intraline_segments(a: str, b: str):
    """Word-level intra-line diff. Returns (left_segments, right_segments)."""
    at = _TOKEN_RE.findall(a)
    bt = _TOKEN_RE.findall(b)
    sm = difflib.SequenceMatcher(None, at, bt, autojunk=False)
    left, right = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        ltext = "".join(at[i1:i2])
        rtext = "".join(bt[j1:j2])
        if tag == "equal":
            if ltext:
                left.append({"text": ltext, "changed": False})
                right.append({"text": rtext, "changed": False})
        else:
            if ltext:
                left.append({"text": ltext, "changed": True})
            if rtext:
                right.append({"text": rtext, "changed": True})
    return _merge_segments(left), _merge_segments(right)


def _cell(num: Optional[int], text: Optional[str]):
    return {"num": (num + 1) if num is not None else None, "text": text}


def _row(typ, lnum, ltext, rnum, rtext):
    return {"type": typ, "left": _cell(lnum, ltext), "right": _cell(rnum, rtext)}


def _strip_frame(lines: List[str], drop_res, head_res=()) -> List[str]:
    """
    Remove CLI session framing from a capture before it's diffed:
      0. consume the leading session header — the run of blank lines,
         pagination-disable echoes and their acknowledgements before the first
         line of real output. These stop at the first line that isn't framing,
         so the same patterns can't reach a real config line further down.
      1. drop prompt / command-echo / RE-context lines anywhere they appear
         (they aren't state, and often land on only one side of a diff), then
      2. trim leading and trailing blank lines — the empty rows a device emits
         around its output, which are framing too.
    Blank lines in the MIDDLE are preserved; only the top/bottom frame is
    trimmed, so a genuinely empty line inside an output still diffs normally.
    """
    # ── 0. leading session header ────────────────────────────────
    start = 0
    while start < len(lines):
        ln = lines[start]
        if (not ln.strip()
                or any(r.search(ln) for r in head_res)
                or any(r.search(ln) for r in drop_res)):
            start += 1
            continue
        break
    lines = lines[start:]

    kept = [ln for ln in lines if not any(r.search(ln) for r in drop_res)]
    lo, hi = 0, len(kept)
    while lo < hi and not kept[lo].strip():
        lo += 1
    while hi > lo and not kept[hi - 1].strip():
        hi -= 1
    return kept[lo:hi]


def diff_configs(a_text: str, b_text: str, opts: Optional[NormalizeOptions] = None):
    opts = opts or NormalizeOptions()
    a_lines = a_text.splitlines()
    b_lines = b_text.splitlines()

    # Strip CLI session framing (prompt/command echo, RE context, and the
    # blank edges around output) from BOTH sides before aligning. This framing
    # isn't state and often appears on only one side, so it must leave the
    # sequence entirely — masking would leave a one-sided empty line that still
    # diffs as a delete. Display uses the surviving lines.
    if opts.drop_patterns or opts.head_drop_patterns:
        drop_res = [re.compile(p) for p in opts.drop_patterns]
        head_res = [re.compile(p, re.IGNORECASE) for p in opts.head_drop_patterns]
        a_lines = _strip_frame(a_lines, drop_res, head_res)
        b_lines = _strip_frame(b_lines, drop_res, head_res)

    na = [_normalize_line(l, opts) for l in a_lines]
    nb = [_normalize_line(l, opts) for l in b_lines]

    # autojunk=False is NOT optional for configs: the heuristic treats lines
    # appearing >1% of the time (!, exit, indentation, description...) as junk
    # and silently wrecks alignment on long device configs.
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)

    rows = []
    added = removed = changed = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                rows.append(_row("equal", i1 + k, a_lines[i1 + k], j1 + k, b_lines[j1 + k]))
        elif tag == "delete":
            for k in range(i1, i2):
                rows.append(_row("delete", k, a_lines[k], None, None))
                removed += 1
        elif tag == "insert":
            for k in range(j1, j2):
                rows.append(_row("insert", None, None, k, b_lines[k]))
                added += 1
        elif tag == "replace":
            la, lb = i2 - i1, j2 - j1
            for k in range(max(la, lb)):
                li = i1 + k if k < la else None
                rj = j1 + k if k < lb else None
                lt = a_lines[li] if li is not None else None
                rt = b_lines[rj] if rj is not None else None
                row = _row("replace", li, lt, rj, rt)
                if lt is not None and rt is not None:
                    lseg, rseg = _intraline_segments(lt, rt)
                    row["left"]["segments"] = lseg
                    row["right"]["segments"] = rseg
                    changed += 1
                elif lt is not None:
                    removed += 1
                else:
                    added += 1
                rows.append(row)

    summary = {
        "added": added,
        "removed": removed,
        "changed": changed,
        "identical": added == 0 and removed == 0 and changed == 0,
        "left_lines": len(a_lines),
        "right_lines": len(b_lines),
    }
    return {"rows": rows, "summary": summary}


if __name__ == "__main__":
    import json
    A = """interface GigabitEthernet0/0
 description UPLINK to core
 ip address 10.0.0.1 255.255.255.252
 mtu 1500
 no shutdown
!
router ospf 1
 network 10.0.0.0 0.0.0.255 area 0
"""
    B = """interface GigabitEthernet0/0
 description UPLINK to core-01
 ip address 10.0.0.1 255.255.255.252
 mtu 9000
 no shutdown
 load-interval 30
!
router ospf 1
 network 10.0.0.0 0.0.0.255 area 0
"""
    print(json.dumps(diff_configs(A, B), indent=2))