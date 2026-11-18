"""
LLDP record normalization.

The shipped TextFSM templates disagree on column names for the same facts.
Taken from the templates in tfsm_templates.db:

    remote system name   NEIGHBOR_NAME (most), NEIGHBOR (allied awplus),
                         SYSTEM_NAME_DATA (fortinet)
    remote port          NEIGHBOR_INTERFACE (most), NEIGHBOR_PORT_ID,
                         PORT_ID_DATA (fortinet)
    management address   MGMT_ADDRESS, MANAGEMENT_IP, ADDRESS_1_ADDR
    local port           LOCAL_INTERFACE, PORT (fortinet)
    parent (LAG)         PARENT_INTERFACE (junos)

Brocade's detail template carries both NEIGHBOR_INTERFACE and
NEIGHBOR_PORT_ID; NEIGHBOR_INTERFACE is preferred because it is the
interface name where both exist. Any field may come back as a TextFSM
List value, so every read goes through _scalar().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

_REMOTE_NAME = ("NEIGHBOR_NAME", "NEIGHBOR", "SYSTEM_NAME_DATA")
_REMOTE_PORT = ("NEIGHBOR_INTERFACE", "NEIGHBOR_PORT_ID", "PORT_ID_DATA")
_REMOTE_PORT_DESC = (
    "NEIGHBOR_PORT_DESCRIPTION", "NEIGHBOR_INTERFACE_DESCRIPTION", "PORT_DESC_DATA",
)
_MGMT = ("MGMT_ADDRESS", "MANAGEMENT_IP", "ADDRESS_1_ADDR")
_LOCAL_PORT = ("LOCAL_INTERFACE", "PORT")
_PARENT = ("PARENT_INTERFACE",)
_DESC = ("NEIGHBOR_DESCRIPTION", "SYSTEM_DESC_DATA")
_CHASSIS = ("CHASSIS_ID", "CHASSIS_DATA")
_CAPS = ("CAPABILITIES", "SYSTEM_CAPS_ENABLED_TXT")


# What templates leave in a column the device printed as "not there":
# Junos shows "-" for no parent interface.
_PLACEHOLDERS = {"-", "--", "n/a", "na", "none", "not advertised"}


def _scalar(value) -> str:
    """First non-empty element of a List value, or the stripped string."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        for v in value:
            s = _scalar(v)
            if s:
                return s
        return ""
    s = str(value).strip().strip('"')
    return "" if s.lower() in _PLACEHOLDERS else s


def _first(record: dict, keys: Iterable[str]) -> str:
    for k in keys:
        v = _scalar(record.get(k))
        if v:
            return v
    return ""


@dataclass(frozen=True)
class NeighborClaim:
    """One device's statement: 'on my local_port I hear remote_name on remote_port'."""
    local_port: str
    remote_name: str
    remote_port: str
    remote_port_desc: str = ""
    remote_ip: str = ""
    remote_desc: str = ""
    chassis_id: str = ""
    parent_port: str = ""
    capabilities: str = ""

    def as_neighbor_row(self) -> dict:
        """Row shape the viewer's Neighbors tab expects."""
        return {
            "local_interface": self.local_port,
            "remote_device": self.remote_name,
            "remote_interface": self.remote_port,
            "remote_ip": self.remote_ip,
            "remote_platform": self.remote_desc,
            "protocol": "lldp",
        }


def normalize_records(records: Optional[List[dict]]) -> List[NeighborClaim]:
    """Parsed LLDP records → claims. Records with no local port are dropped;
    a record with no system name falls back to the chassis ID so a neighbor
    that only advertises a MAC still shows up."""
    out: List[NeighborClaim] = []
    seen = set()
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        local = _first(rec, _LOCAL_PORT)
        if not local:
            continue
        chassis = _first(rec, _CHASSIS)
        name = _first(rec, _REMOTE_NAME) or chassis
        if not name:
            continue
        claim = NeighborClaim(
            local_port=local,
            remote_name=name,
            remote_port=_first(rec, _REMOTE_PORT),
            remote_port_desc=_first(rec, _REMOTE_PORT_DESC),
            remote_ip=_first(rec, _MGMT),
            remote_desc=_first(rec, _DESC),
            chassis_id=chassis,
            parent_port=_first(rec, _PARENT),
            capabilities=_first(rec, _CAPS),
        )
        key = (claim.local_port, claim.remote_name, claim.remote_port)
        if key in seen:
            continue
        seen.add(key)
        out.append(claim)
    return out


def records_from_envelope(envelope: Optional[dict]) -> List[dict]:
    """Stored parsed JSON is the standard envelope ({..., "records": [...]})."""
    if not isinstance(envelope, dict):
        return []
    recs = envelope.get("records")
    return recs if isinstance(recs, list) else []
