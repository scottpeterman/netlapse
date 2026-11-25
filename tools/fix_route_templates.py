#!/usr/bin/env python3
"""
Fix the route-table TextFSM templates in a netlapse template database.

arista_eos_show_ip_route_v2
    Recorded on the prefix line before the ECMP "via" continuation lines
    were read, so every row carried the previous route's last next hop:
    the first route (usually 0.0.0.0/0) parsed with no next hop, the first
    via of every route was dropped, and "is directly connected" routes
    (connected, Null0 aggregates) were not recorded at all. The fixed
    template records on the next prefix line, keeps every via as a list,
    records connected/Null0 routes (both the one-line "is directly
    connected" form and the newer two-line form where "directly connected,
    IF" sits under the prefix) and tracks the VRF.

juniper_junos_show_route
    Captured no next hops and no active marker, so it could not be used
    for forwarding. The fixed template keeps the existing fields, records
    only the active path per prefix, and adds TABLE, ACTIVE, SELECTED (">"
    per next hop), NEXT_HOP, INTERFACE and NH_TYPE (Reject, Discard,
    Receive, Local). AGE now keeps the time part, so LOCALPREF is captured
    on lines like "5w2d 03:12:44, localpref 100".

NEXT_HOP and INTERFACE become lists in both templates.

Idempotent. Writes a .bak copy of the database before the first change.

    python tools/fix_route_templates.py ~/.netlapse/tfsm_templates.db

Then reparse stored route captures (parse audit reparse, dry run first).
"""

import shutil
import sqlite3
import sys
from pathlib import Path

FIXED = {
    "arista_eos_show_ip_route_v2": r"""Value Filldown VRF (\S+)
Value Required PREFIX (\d+\.\d+\.\d+\.\d+/\d+)
Value PROTOCOL (\S+(?:\s[A-Z0-9]{1,2})?)
Value ADMIN_DIST (\d+)
Value METRIC (\d+)
Value List NEXT_HOP (\d+\.\d+\.\d+\.\d+|connected)
Value List INTERFACE ([^\s,]+)

Start
  ^VRF(?:\s+name)?:\s+\S+\s*$$ -> Continue.Record
  ^VRF(?:\s+name)?:\s+${VRF}\s*$$
  ^\s+\S.*?\s\d+\.\d+\.\d+\.\d+/\d+(?:\s|$$) -> Continue.Record
  ^\s+${PROTOCOL}\s+${PREFIX}\s+\[${ADMIN_DIST}/${METRIC}\]\s+via\s+${NEXT_HOP},\s+${INTERFACE}
  ^\s+${PROTOCOL}\s+${PREFIX}\s+\[${ADMIN_DIST}/${METRIC}\]
  ^\s+${PROTOCOL}\s+${PREFIX}\s+is\s+directly\s+${NEXT_HOP},\s+${INTERFACE}
  ^\s+${PROTOCOL}\s+${PREFIX}\s*$$
  ^\s+directly\s+${NEXT_HOP},\s+${INTERFACE}
  ^\s+via\s+${NEXT_HOP},\s+${INTERFACE}
""",
    "juniper_junos_show_route": r"""Value Filldown TABLE (\S+)
Value Required PREFIX (\d+\.\d+\.\d+\.\d+/\d+)
Value Required ACTIVE ([*+])
Value PROTOCOL (\w+)
Value PREFERENCE (\d+)
Value AGE (\S+(?:\s\d+:\d+:\d+)?)
Value LOCALPREF (\d+)
Value AS_PATH (.+?)
Value List SELECTED (>?)
Value List NEXT_HOP (\d+\.\d+\.\d+\.\d+)
Value List INTERFACE ([^\s,]+)
Value NH_TYPE (Reject|Discard|Receive|Local)

Start
  ^\S+:\s+\d+\s+destinations -> Continue.Record
  ^${TABLE}:\s+\d+\s+destinations
  ^\d+\.\d+\.\d+\.\d+/\d+\s -> Continue.Record
  ^${PREFIX}\s+${ACTIVE}\[${PROTOCOL}/${PREFERENCE}\]\s+${AGE},\s+localpref\s+${LOCALPREF} -> Active
  ^${PREFIX}\s+${ACTIVE}\[${PROTOCOL}/${PREFERENCE}\]\s+${AGE} -> Active
  ^${PREFIX}\s+\S?\[ -> Skip

Active
  ^\S+:\s+\d+\s+destinations -> Continue.Record
  ^${TABLE}:\s+\d+\s+destinations -> Start
  ^\d+\.\d+\.\d+\.\d+/\d+\s -> Continue.Record
  ^${PREFIX}\s+${ACTIVE}\[${PROTOCOL}/${PREFERENCE}\]\s+${AGE},\s+localpref\s+${LOCALPREF}
  ^${PREFIX}\s+${ACTIVE}\[${PROTOCOL}/${PREFERENCE}\]\s+${AGE}
  ^${PREFIX}\s+\S?\[ -> Skip
  ^\s+AS\spath:\s+${AS_PATH},\s+validation
  ^\s+AS\spath:\s+${AS_PATH}\s*$$
  ^\s+${SELECTED}\s*to\s+${NEXT_HOP}\s+via\s+${INTERFACE}
  ^\s+${NH_TYPE}\s+via\s+${INTERFACE}
  ^\s+${SELECTED}\s*via\s+${INTERFACE}
  ^\s+${NH_TYPE}\s*$$
  ^\s+\S?\[\w+/\d+\] -> Skip

Skip
  ^\S+:\s+\d+\s+destinations -> Continue.Record
  ^${TABLE}:\s+\d+\s+destinations -> Start
  ^\d+\.\d+\.\d+\.\d+/\d+\s -> Continue.Record
  ^${PREFIX}\s+${ACTIVE}\[${PROTOCOL}/${PREFERENCE}\]\s+${AGE},\s+localpref\s+${LOCALPREF} -> Active
  ^${PREFIX}\s+${ACTIVE}\[${PROTOCOL}/${PREFERENCE}\]\s+${AGE} -> Active
  ^${PREFIX}\s+\S?\[ -> Skip
""",
}


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    db = Path(argv[1]).expanduser()
    if not db.exists():
        print(f"not found: {db}")
        return 1
    names = tuple(FIXED)
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        f"SELECT cli_command, textfsm_content FROM templates WHERE cli_command IN ({','.join('?' * len(names))})",
        names,
    ).fetchall()
    found = {n for n, _ in rows}
    for n in names:
        if n not in found:
            print(f"missing (not changed): {n}")
    todo = sorted({n for n, t in rows if t != FIXED[n]})
    if not todo:
        conn.close()
        print("already fixed" if found else "no route templates in this database")
        return 0 if found else 1
    bak = db.with_suffix(db.suffix + ".bak")
    if not bak.exists():
        conn.close()
        shutil.copy2(db, bak)
        conn = sqlite3.connect(str(db))
        print(f"backup: {bak}")
    # By name, not id: template ids are not unique in every template DB.
    for n in todo:
        conn.execute("UPDATE templates SET textfsm_content = ? WHERE cli_command = ?", (FIXED[n], n))
        print(f"fixed: {n}")
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
