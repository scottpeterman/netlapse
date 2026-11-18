#!/usr/bin/env python3
"""
Fix the Arista "show lldp neighbors detail" TextFSM templates in a netlapse
template database.

Both NTC templates (arista_eos_show_lldp_neighbors_detail and ..._detail2)
match every "Port ID :" line inside a neighbor block. EOS prints a second one
under the IEEE802.1/IEEE802.3 Link Aggregation TLV (the aggregation port id,
normally 0), and the last match wins, so every Arista neighbor's port parsed
as "0". The topology map then cannot match the two ends of an Arista link by
port. The fixed template skips the Link Aggregation block.

Idempotent. Writes a .bak copy of the database before the first change.

    python tools/fix_arista_lldp_template.py ~/.netlapse/tfsm_templates.db

Then reparse stored lldp captures (Template Lab / parse audit reparse) so
existing snapshots pick up the corrected ports.
"""

import shutil
import sqlite3
import sys
from pathlib import Path

NAMES = (
    "arista_eos_show_lldp_neighbors_detail",
    "arista_eos_show_lldp_neighbors_detail2",
)

FIXED = r"""Value Filldown LOCAL_INTERFACE (\S+)
Value Required NEIGHBOR_NAME (.+?)
Value CHASSIS_ID ([0-9a-fA-F.]+)
Value NEIGHBOR_INTERFACE (.+?)
Value MGMT_ADDRESS (\d+\.\d+\.\d+\.\d+)
Value NEIGHBOR_DESCRIPTION ([^"]+)

Start
  ^Interface\s+${LOCAL_INTERFACE}\s+detected\s+\d+\s+LLDP\s+neighbor
  ^\s+Neighbor\s+ -> Neighbor

Neighbor
  ^\s+Chassis\s+ID\s+:\s+${CHASSIS_ID}\s*$$
  ^\s+Port\s+ID\s+:\s+"?${NEIGHBOR_INTERFACE}"?\s*$$
  ^\s+-\s+System\s+Name:\s+"${NEIGHBOR_NAME}"\s*$$
  ^\s+-\s+System\s+Description:\s+"${NEIGHBOR_DESCRIPTION}
  ^\s+Management\s+Address\s+:\s+${MGMT_ADDRESS}\s*$$
  # The Link Aggregation TLV carries its own "Port ID" (the aggregation
  # port id, usually 0). Without this state it overwrote the neighbor's
  # real Port ID, so every Arista neighbor reported port "0".
  ^\s+-\s+IEEE802\.1/IEEE802\.3\s+Link\s+Aggregation -> LinkAgg
  ^Interface\s+${LOCAL_INTERFACE}\s+detected\s+\d+\s+LLDP\s+neighbor -> Record
  ^\s+Neighbor\s+ -> Record Neighbor
  ^\s*$$ -> Record Start

LinkAgg
  ^\s+Port\s+ID\s+:
  ^\s+-\s+ -> Neighbor
  ^Interface\s+${LOCAL_INTERFACE}\s+detected\s+\d+\s+LLDP\s+neighbor -> Record Neighbor
  ^\s+Neighbor\s+ -> Record Neighbor
  ^\s*$$ -> Record Start
"""


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    db = Path(argv[1]).expanduser()
    if not db.exists():
        print(f"not found: {db}")
        return 1
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT id, cli_command, textfsm_content FROM templates WHERE cli_command IN (?, ?)", NAMES
    ).fetchall()
    todo = sorted({n for _, n, t in rows if t != FIXED})
    if not rows:
        conn.close()
        print("no Arista LLDP detail templates in this database")
        return 1
    if not todo:
        conn.close()
        print("already fixed")
        return 0
    bak = db.with_suffix(db.suffix + ".bak")
    if not bak.exists():
        conn.close()
        shutil.copy2(db, bak)
        conn = sqlite3.connect(str(db))
        print(f"backup: {bak}")
    # By name, not id: template ids are not unique in every template DB.
    for n in todo:
        conn.execute("UPDATE templates SET textfsm_content = ? WHERE cli_command = ?", (FIXED, n))
        print(f"fixed: {n}")
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
