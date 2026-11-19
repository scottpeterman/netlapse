"""
Topology API — the network map built from stored LLDP captures.

    GET /api/v1/topology                 map at a point in time
    GET /api/v1/topology/diff            two points, union map tagged by change
    GET /api/v1/topology/changes         when LLDP last moved (time picker feed)
    GET /api/v1/topology/artifact        a device's raw capture at that time
    GET /api/v1/topology/mac             a device's MAC table at that time,
                                         joined to its own ARP, uplinks marked
    GET /api/v1/topology/node            everything else the device dialog
                                         shows: hardware, interfaces, ARP,
                                         and every capture with its own date

Every read goes through CaptureReader, so the node dialog's tabs show the
device as it was at the map's as_of, not as it is now.

No device is contacted. Behind the global auth gate like every /api/v1 route.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from netlapse.topology import build_topology, diff_topologies
from netlapse.topology.history import CaptureReader, parse_as_of
from netlapse.topology.lldp import normalize_records, records_from_envelope
from netlapse.topology.names import NameResolver, interface_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/topology", tags=["topology"])

_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_CYCLE_GAP = 120  # seconds between commits that still count as one cycle
_HEX = re.compile(r"[^0-9a-f]")


def _as_of(value: Optional[str]) -> Optional[datetime]:
    try:
        return parse_as_of(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad timestamp: {value!r}")


def _devices(request: Request, status: str):
    db = request.app.state.db
    return db.list_devices(status=None if status == "all" else status)


def _resolve_device(request: Request, device_dir: str) -> dict:
    """device_dir is 'site/name' from the map. Validated against DCIM so it
    can never be used as a path."""
    if "/" not in device_dir:
        raise HTTPException(status_code=400, detail="device_dir must be site/name")
    site, name = device_dir.split("/", 1)
    db = request.app.state.db
    dev = db.get_device_by_name_and_site(name, site)
    if not dev:
        raise HTTPException(status_code=404, detail=f"unknown device {device_dir}")
    return dev


@router.get("")
def topology(
    request: Request,
    site: Optional[str] = Query(None, description="site slug, or several comma-separated"),
    as_of: Optional[str] = Query(None, description="ISO-8601 or YYYYMMDDTHHMMSSZ; omit for latest"),
    unmanaged: bool = Query(False, description="include neighbors not in DCIM"),
    status: str = Query("active", description="DCIM device status filter, or 'all'"),
):
    reader = CaptureReader(request.app.state.storage, _as_of(as_of))
    result = build_topology(_devices(request, status), reader, site=site or None,
                            include_unmanaged=unmanaged)
    return result.to_dict()


@router.get("/diff")
def topology_diff(
    request: Request,
    from_: str = Query(..., alias="from"),
    to: Optional[str] = None,
    site: Optional[str] = Query(None, description="site slug, or several comma-separated"),
    unmanaged: bool = False,
    status: str = "active",
):
    storage = request.app.state.storage
    devices = _devices(request, status)
    old = build_topology(devices, CaptureReader(storage, _as_of(from_)),
                         site=site or None, include_unmanaged=unmanaged)
    new = build_topology(devices, CaptureReader(storage, _as_of(to)),
                         site=site or None, include_unmanaged=unmanaged)
    return diff_topologies(old, new)


@router.get("/changes")
def lldp_changes(request: Request, days: int = Query(30, ge=1, le=3650),
                 site: Optional[str] = Query(None, description="site slug, or several comma-separated")):
    """Distinct times LLDP data changed, newest first, with the devices
    involved — what the time picker offers as 'from' points."""
    storage = request.app.state.storage
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        sites = {s.strip() for s in (site or "").split(",") if s.strip()}
        one = next(iter(sites)) if len(sites) == 1 else None
        changes = storage.fleet_changes(since=since, capture_type="lldp", site=one)
        if len(sites) > 1:
            changes = [c for c in changes if c.site in sites]
    except NotImplementedError:
        return {"supported": False, "changes": []}
    # One collection cycle commits device by device over several minutes.
    # Cluster changes separated by less than _CYCLE_GAP into one point, so
    # the picker offers "that cycle" rather than one entry per device.
    # first/last bracket the cycle: compare first-1s against last.
    events = sorted(changes, key=lambda c: c.at)
    points = []
    for c in events:
        t = parse_as_of(c.at)
        if points and (t - points[-1]["_last"]).total_seconds() <= _CYCLE_GAP:
            p = points[-1]
        else:
            p = {"first": c.at, "_last": t, "devices": []}
            points.append(p)
        p["_last"] = t
        p["last"] = c.at
        p["devices"].append(f"{c.site}/{c.device}:{c.kind}")
    for p in points:
        p.pop("_last")
    points.reverse()
    return {"supported": True, "changes": points[:200]}


@router.get("/artifact")
def artifact(request: Request, device_dir: str, kind: str, as_of: Optional[str] = None):
    dev = _resolve_device(request, device_dir)
    if not re.fullmatch(r"[a-z0-9_\-]+", kind):
        raise HTTPException(status_code=400, detail="bad capture type")
    reader = CaptureReader(request.app.state.storage, _as_of(as_of))
    text, when = reader.raw(dev["site_slug"], dev["name"], kind)
    if text is None:
        return {"ok": False, "error": f"No {kind} capture for {device_dir} at that time"}
    truncated = len(text) > _MAX_ARTIFACT_BYTES
    return {
        "ok": True,
        "text": text[:_MAX_ARTIFACT_BYTES],
        "truncated": truncated,
        "path": f"{device_dir}/{kind}.txt" + (f" @ {when.isoformat()}Z" if when else ""),
    }


def _mac_hex(v) -> str:
    return _HEX.sub("", str(v or "").lower())


def _first(rec: dict, keys) -> str:
    for k in keys:
        v = rec.get(k)
        if isinstance(v, list):
            v = ",".join(str(x) for x in v if x)
        if v:
            return str(v).strip()
    return ""


@router.get("/mac")
def mac_table(request: Request, device_dir: str, as_of: Optional[str] = None):
    dev = _resolve_device(request, device_dir)
    site, name = dev["site_slug"], dev["name"]
    reader = CaptureReader(request.app.state.storage, _as_of(as_of))

    env, when = reader.parsed(site, name, "mac")
    if env is None:
        return {"ok": False, "error": f"No parsed MAC table for {device_dir} at that time"}

    arp_env, _ = reader.parsed(site, name, "arp")
    ip_by_mac = {}
    for r in records_from_envelope(arp_env):
        mac = _mac_hex(_first(r, ("MAC_ADDRESS", "HARDWARE_ADDRESS")))
        ip = _first(r, ("IP_ADDRESS", "ADDRESS"))
        if mac and ip:
            ip_by_mac.setdefault(mac, ip)

    # Uplink = a port whose LLDP neighbor is a device in DCIM. A server
    # running lldpd is a neighbor too, but its port is an access port.
    lldp_env, _ = reader.parsed(site, name, "lldp")
    resolver = NameResolver(request.app.state.db.list_devices())
    uplinks = set()
    for c in normalize_records(records_from_envelope(lldp_env)):
        if resolver.resolve(c.remote_name, c.remote_ip, from_site=site) is None:
            continue
        uplinks.add(interface_key(c.local_port))
        if c.parent_port:
            uplinks.add(interface_key(c.parent_port))

    rows = []
    for r in records_from_envelope(env):
        mac = _first(r, ("MAC_ADDRESS", "DESTINATION_ADDRESS"))
        port = _first(r, ("DESTINATION_PORT", "PORTS", "LOGICAL_INTERFACE", "INTERFACE"))
        rows.append({
            "port": port,
            "vlan": _first(r, ("VLAN_ID", "VLAN")),
            "mac": mac,
            "vendor": "",
            "category": _first(r, ("TYPE", "MAC_FLAG")),
            "ip": ip_by_mac.get(_mac_hex(mac), ""),
            "arp_source": "",
            "uplink": interface_key(port.split(",")[0]) in uplinks if port else False,
            "note": "",
        })
    per_port = {}
    for row in rows:
        per_port[row["port"]] = per_port.get(row["port"], 0) + 1
    for row in rows:
        row["port_macs"] = per_port[row["port"]]
    return {
        "ok": True,
        "rows": rows,
        "path": f"{device_dir}/mac.json" + (f" @ {when.isoformat()}Z" if when else ""),
    }



# ── device dialog detail ─────────────────────────────────────────────

# Hardware facts come only from captures that carry them, in this order:
# a version capture first (model, OS version, uptime), then an inventory or
# chassis-hardware capture fills what's still missing (serial on Cisco, the
# chassis row on Junos). Matching by name keeps unrelated captures (bgp6,
# routesum, custom jobs) from ever being mistaken for hardware.
_HW_CAPTURE_ORDER = ("version", "inventory", "hardware", "chassis")

_HW_KEYS = {
    "model": ("MODEL", "HARDWARE", "PLATFORM", "CHASSIS_DESCRIPTION", "PID", "DESCR"),
    "serial": ("SERIAL_NUMBER", "SERIAL", "CHASSIS_SERIAL_NUMBER", "SN"),
    "os_version": ("SOFTWARE_VERSION", "JUNOS_VERSION", "VERSION", "OS_VERSION", "IMAGE_VERSION"),
    "uptime": ("UPTIME",),
    "hw_rev": ("HW_VERSION", "HARDWARE_REV", "VID"),
}


def _iso(dt) -> str:
    return dt.isoformat() + "Z" if dt else ""


def _mbps(rec: dict) -> str:
    speed = _first(rec, ("SPEED",))
    if speed:
        return speed
    bw = _first(rec, ("BANDWIDTH",))
    m = re.match(r"\s*(\d+)\s*kbit", bw or "", re.I)
    if m:
        return str(int(m.group(1)) // 1000)
    return bw


def _iface_row(rec: dict) -> dict:
    """One interface row from any of the interface templates:
    `show interfaces` (INTERFACE, DESCRIPTION, LINK_STATUS/PROTOCOL_STATUS),
    `show interfaces status` (PORT, NAME = the description, STATUS), and Junos
    `show interfaces terse` (INTERFACE, ADMIN_STATE, LINK_STATE, LOCAL)."""
    ip = _first(rec, ("IP_ADDRESS", "LOCAL"))
    plen = _first(rec, ("PREFIX_LENGTH",))
    if ip and plen and "/" not in ip:
        ip = f"{ip}/{plen}"
    if "ADMIN_STATE" in rec and "LINK_STATE" in rec:       # Junos terse
        status = "/".join(x for x in (_first(rec, ("ADMIN_STATE",)), _first(rec, ("LINK_STATE",))) if x)
    else:
        link = _first(rec, ("LINK_STATUS", "LINK_STATE", "STATUS"))
        proto = _first(rec, ("PROTOCOL_STATUS",))
        status = "/".join(x for x in (link, proto) if x)
    # In the status-table templates NAME is the port description.
    alias = _first(rec, ("DESCRIPTION",)) or (_first(rec, ("NAME",)) if "PORT" in rec else "")
    return {
        "name": _first(rec, ("INTERFACE", "PORT", "INTF")),
        "if_index": _first(rec, ("IFINDEX", "SNMP_IFINDEX")),
        "alias": alias,
        "ip_address": ip,
        "mac_address": _first(rec, ("MAC_ADDRESS", "ADDRESS")),
        "speed_mbps": _mbps(rec),
        "mtu": _first(rec, ("MTU",)),
        "status": status,
    }


def _arp_row(rec: dict) -> dict:
    return {
        "ip": _first(rec, ("IP_ADDRESS", "ADDRESS")),
        "mac": _first(rec, ("MAC_ADDRESS", "HARDWARE_ADDRESS")),
        "interface": _first(rec, ("INTERFACE",)),
        "age": _first(rec, ("AGE",)),
        "vrf": _first(rec, ("VRF",)),
    }


@router.get("/node")
def node_detail(request: Request, device_dir: str, as_of: Optional[str] = None):
    """Everything the device dialog shows beyond what map.json carries, read
    at the map's time. Each section names the capture it came from and when
    that capture was collected, since captures run on their own intervals."""
    dev = _resolve_device(request, device_dir)
    site, name = dev["site_slug"], dev["name"]
    reader = CaptureReader(request.app.state.storage, _as_of(as_of))

    captures = []
    parsed = {}
    for ct in reader.list_captures(site, name):
        env, when = reader.parsed(site, name, ct)
        if env is None:
            _, when = reader.raw(site, name, ct)
        recs = records_from_envelope(env)
        parsed[ct] = (recs, when)
        captures.append({
            "capture_type": ct,
            "collected_at": _iso(when),
            "command": (env or {}).get("command") or "",
            "template": (env or {}).get("template") or "",
            "records": len(recs) if env is not None else None,
        })

    sources = {}

    def src(section, ct):
        recs, when = parsed.get(ct, ([], None))
        sources[section] = {"capture_type": ct, "collected_at": _iso(when), "records": len(recs)}
        return recs

    interfaces = [_iface_row(r) for r in src("interfaces", "interfaces")] if "interfaces" in parsed else []
    arp = [_arp_row(r) for r in src("arp", "arp")] if "arp" in parsed else []
    if "lldp" in parsed:
        src("neighbors", "lldp")

    hw = {}
    hw_from = []
    for want in _HW_CAPTURE_ORDER:
        for ct in sorted(c for c in parsed if want in c):
            recs, when = parsed[ct]
            if not recs or ct in hw_from:
                continue
            rec = recs[0]
            found = {k: _first(rec, keys) for k, keys in _HW_KEYS.items()}
            added = {k: v for k, v in found.items() if v and k not in hw}
            if added:
                hw.update(added)
                hw_from.append(ct)
                if "hardware" not in sources:
                    sources["hardware"] = {"capture_type": ct, "collected_at": _iso(when),
                                           "records": len(recs)}
    if len(hw_from) > 1:
        sources["hardware"]["capture_type"] = " + ".join(hw_from)

    return {
        "ok": True,
        "device_dir": device_dir,
        "as_of": _iso(reader.as_of),
        "patch": {
            "vendor": dev.get("manufacturer_name") or "",
            "serial": hw.get("serial") or dev.get("serial_number") or "",
            "model": hw.get("model") or "",
            "os_version": hw.get("os_version") or "",
            "metadata": {
                "interfaces": interfaces,
                "hardware_rev": hw.get("hw_rev") or "",
                "uptime_text": hw.get("uptime") or "",
                "inventory_source": f"{sources['hardware']['capture_type']} capture" if "hardware" in sources
                                    else ("DCIM" if dev.get("serial_number") else ""),
                "credential_used": dev.get("credential_name") or "",
                "asset_tag": dev.get("asset_tag") or "",
                "description": dev.get("description") or "",
                "counts": {"interfaces": len(interfaces), "arp_entries": len(arp)},
            },
        },
        "arp": arp,
        "captures": sorted(captures, key=lambda c: c["capture_type"]),
        "sources": sources,
        "dcim": {
            "status": dev.get("status") or "",
            "role": dev.get("role_name") or "",
            "site": dev.get("site_name") or site,
            "last_collected_at": dev.get("last_collected_at") or "",
            "last_collection_status": dev.get("last_collection_status") or "",
            "credential": dev.get("credential_name") or "",
            "netbox_id": dev.get("netbox_id"),
        },
    }
