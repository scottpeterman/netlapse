#!/usr/bin/env python3
"""
demo_lab.py — a synthetic three-site network for demos, screenshots and docs.

Builds a self-contained netlapse instance (DCIM database, file storage with
two days of history, definitions, config.yaml) with no real hostnames or
addresses. Every capture is written in the shape the parse templates produce:
routes, LLDP, interface addresses, interfaces, ARP, MAC table, version and
config. Forwarding, Topology, the device dialog and Changes all have data.

Sites
  den1  spine pair + 17 EOS leafs, a Junos pod (aggb1/2 + 6 leafs), core1,
        edge1/edge2 to transit, an OOB switch
  den2  spine pair + 12 EOS leafs, a Junos leaf on an SVI, core2
  ams1  spine pair + 4 leafs + edge1.ams1; no links to Denver
  den1 and den2 are joined by two core1-core2 links.

Planted findings
  Forwarding
    leaf07.den1, leaf08.den1  single-homed: their spine2 ports were never
                              finished; spine2 reaches them the long way
                              (core1 -> edge -> spine1). Two days ago they
                              were dual-homed (the --as-of history).
    den2 -> den1              crosses the edges, den1 -> den2 doesn't
                              (core1 holds den1 only as an aggregate via the
                              edges): cross-site asymmetry.
    leaf21-26.den1 (Junos)    RIB lists both uplinks, one is installed.
    leaf12.den2               subnet never advertised; core2 has a Reject
                              10.20.0.0/16: black hole.
    leaf20.den2 (Junos)       uplink is an SVI, resolved by address ownership.
    edge2.den1                route capture 8 hours behind: stale.
  Topology
    leaf17.den1               new since two days ago: two uplinks added.
    leaf09.den2               re-patched on spine1.den2 (Ethernet9/1 ->
                              Ethernet14/1): one link removed, one added.
    leaf04.ams1 <-> spine2.ams1  one-sided: the spine doesn't list the leaf.
    edge1.ams1                in DCIM, no LLDP capture: its links unverified.
    srv-* on every leaf        servers running lldpd: unmanaged neighbors.

Usage:
    python tools/demo_lab.py --out /tmp/netlapse-demo
    NETLAPSE_CONFIG=/tmp/netlapse-demo/config.yaml NETLAPSE_ADMIN_PASSWORD=demo-pass-1 NETLAPSE_SKIP_TEMPLATE_DOWNLOAD=1 python -m netlapse serve
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netlapse.dcim.db_schema import NetlapseDB  # noqa: E402
from netlapse.storage.backend import Snapshot  # noqa: E402
from netlapse.storage.file_backend import FileBackend  # noqa: E402

EOS_V2, EOS, JUNOS = "eos_v2", "eos", "junos"
ROLE_TAG = {"leaf": "LEAF", "spine": "SPINE", "core": "CORE", "router": "EDGE", "access": "OOB"}
SITE_NAMES = {"den1": "Denver 1", "den2": "Denver 2", "ams1": "Amsterdam 1"}
MODELS = {
    (EOS, "spine"): "DCS-7280CR3-32P4", (EOS, "access"): "DCS-7010TX-48",
    (EOS_V2, "leaf"): "DCS-7050CX3-32S",
    (JUNOS, "leaf"): "qfx5120-48y", (JUNOS, "spine"): "qfx5120-32c",
    (JUNOS, "core"): "qfx10002-36q", (JUNOS, "router"): "mx204",
}


OUI = {EOS: "001c.73", EOS_V2: "001c.73", JUNOS: "88e0.f3", "server": "0c42.a1"}


def _mac(*parts, oui="0c42.a1") -> str:
    """Deterministic MAC under a vendor OUI (Arista, Juniper, Mellanox for servers)."""
    h = hashlib.md5("|".join(parts).encode()).hexdigest()
    return f"{oui}{h[0:2]}.{h[2:6]}"


class Lab:
    def __init__(self):
        self.devices = []           # (name, site, platform, role, style)
        self.style, self.role, self.site = {}, {}, {}
        self.ifaces = {}            # dev -> {if: {"desc", "ip"}} (insertion ordered)
        self.links = {}             # (dev, if) -> peer ip
        self.peer = {}              # (dev, if) -> (peer, peer_if)
        self.lldp = {}              # dev -> [(if, neighbor, neighbor_port)]
        self.servers = {}           # leaf -> [(if, name, ip, mac)]
        self.routes = {}
        self.no_lldp = set()        # devices without an LLDP capture
        self.hide_lldp = set()      # (dev, neighbor) pairs a device doesn't report
        self._p2p = 0

    def dev(self, name, site, style, role):
        platform = "juniper_junos" if style == JUNOS else "arista_eos"
        self.devices.append((name, site, platform, role, style))
        self.style[name], self.role[name], self.site[name] = style, role, site
        self.ifaces[name] = {}
        self.routes[name] = []
        self.lldp[name] = []

    def iface(self, d, ifn, desc="", ip=""):
        e = self.ifaces[d].setdefault(ifn, {"desc": "", "ip": ""})
        e["desc"] = desc or e["desc"]
        e["ip"] = ip or e["ip"]

    def link(self, a, ai, b, bi, b_has_ip=True):
        n = self._p2p
        self._p2p += 2
        ipa = f"10.254.{n // 256}.{n % 256}"
        ipb = f"10.254.{(n + 1) // 256}.{(n + 1) % 256}"
        self.links[(a, ai)], self.links[(b, bi)] = ipb, ipa
        self.peer[(a, ai)], self.peer[(b, bi)] = (b, bi), (a, ai)
        self.iface(a, ai, f"{ROLE_TAG[self.role[b]]}::{b}:{bi}", f"{ipa}/31")
        self.iface(b, bi, f"{ROLE_TAG[self.role[a]]}::{a}:{ai}", f"{ipb}/31" if b_has_ip else "")
        self.lldp[a].append((ai, b, bi))
        self.lldp[b].append((bi, a, ai))

    def server(self, leaf, ifn, idx, subnet_base):
        name = f"srv-{leaf.split('.')[0]}-{self.site[leaf]}-{idx:02d}"
        ip = f"{subnet_base}.{10 + idx}"
        mac = _mac(name)
        self.servers.setdefault(leaf, []).append((ifn, name, ip, mac))
        self.iface(leaf, ifn, f"SRV::{name}:eth0")
        self.lldp[leaf].append((ifn, name, "eth0"))

    def via(self, d, ifn):
        return (self.links[(d, ifn)], ifn)

    def rt(self, d, prefix, hops=(), kind="fwd", proto="O", ifname=None, sel=None):
        self.routes[d].append({"prefix": prefix, "hops": list(hops), "kind": kind,
                               "proto": proto, "if": ifname, "sel": sel})

    def mac_of(self, d, ifn):
        return _mac(d, ifn, oui=OUI[self.style[d]])

    # ── records in each template family's shape ─────────────────────────

    def route_records(self, d):
        out = []
        style = self.style[d]
        for r in self.routes[d]:
            nh = [h for h, _ in r["hops"]]
            ifs = [i for _, i in r["hops"]]
            if style == JUNOS:
                rec = {"TABLE": "inet.0", "PREFIX": r["prefix"], "ACTIVE": "*", "PROTOCOL": r["proto"],
                       "PREFERENCE": "10", "AGE": "12w3d 04:11:52", "LOCALPREF": "", "AS_PATH": "",
                       "SELECTED": [], "NEXT_HOP": [], "INTERFACE": [], "NH_TYPE": ""}
                if r["kind"] == "conn":
                    rec.update(PROTOCOL="Direct", SELECTED=[">"], INTERFACE=[r["if"]])
                elif r["kind"] == "local":
                    rec.update(PROTOCOL="Local", NH_TYPE="Local", INTERFACE=[r["if"]])
                elif r["kind"] == "drop":
                    rec.update(NH_TYPE="Reject")
                else:
                    rec.update(NEXT_HOP=nh, INTERFACE=[i if "." in i else i + ".0" for i in ifs],
                               SELECTED=r["sel"] or [">"] * len(nh))
            elif style == EOS_V2:
                rec = {"VRF": "default", "PREFIX": r["prefix"], "PROTOCOL": r["proto"], "ADMIN_DIST": "200",
                       "METRIC": "0", "NEXT_HOP": ["connected"] if r["kind"] == "conn" else nh,
                       "INTERFACE": [r["if"]] if r["kind"] == "conn" else ifs}
            else:
                net, plen = r["prefix"].split("/")
                rec = {"VRF": "default", "PROTOCOL": r["proto"], "NETWORK": net, "PREFIX_LENGTH": plen,
                       "DISTANCE": "110", "METRIC": "20", "DIRECT": "directly" if r["kind"] == "conn" else "",
                       "NEXT_HOP": ["connected"] if r["kind"] == "conn" else nh,
                       "INTERFACE": [r["if"]] if r["kind"] == "conn" else ifs}
            out.append(rec)
        return out

    def lldp_records(self, d):
        junos = self.style[d] == JUNOS
        out = []
        for i, nbr, nport in self.lldp[d]:
            if (d, nbr) in self.hide_lldp:
                continue
            fqdn = nbr if nbr.startswith("srv-") else f"{nbr}.example.net"
            rec = {"LOCAL_INTERFACE": i, "NEIGHBOR_NAME": fqdn, "NEIGHBOR_INTERFACE": nport,
                   "CHASSIS_ID": _mac(nbr, "chassis") if nbr.startswith("srv-") else self.mac_of(nbr, "chassis"),
                   "MGMT_ADDRESS": "" if nbr.startswith("srv-") else f"192.0.2.{self.index[nbr]}"}
            if junos:
                rec["PARENT_INTERFACE"] = "-"
            out.append(rec)
        return out

    def addr_records(self, d):
        if self.style[d] == JUNOS:
            return [{"INTERFACE": f"{i}.0" if "." not in i else i, "LOCAL": e["ip"], "STATUS": "up"}
                    for i, e in self.ifaces[d].items() if e["ip"]]
        return [{"INTERFACE": i, "IP_ADDRESS": e["ip"], "STATUS": "up", "PROTO": "up"}
                for i, e in self.ifaces[d].items() if e["ip"]]

    def interface_records(self, d):
        if self.style[d] == JUNOS:
            out = []
            for i, e in self.ifaces[d].items():
                phys = i.split(".")[0]
                if "." not in i and not i.startswith(("irb", "lo0")):
                    out.append({"INTERFACE": phys, "ADMIN_STATE": "up", "LINK_STATE": "up",
                                "PROTOCOL": "", "LOCAL": "", "REMOTE": ""})
                unit = i if "." in i else f"{i}.0"
                out.append({"INTERFACE": unit, "ADMIN_STATE": "up", "LINK_STATE": "up",
                            "PROTOCOL": "inet" if e["ip"] else "eth-switch", "LOCAL": e["ip"], "REMOTE": ""})
            return out
        out = []
        for i, e in self.ifaces[d].items():
            ip, plen = (e["ip"].split("/") + [""])[:2] if e["ip"] else ("", "")
            phys = i.startswith("Ethernet")
            out.append({
                "INTERFACE": i, "LINK_STATUS": "up", "PROTOCOL_STATUS": "up",
                "HARDWARE_TYPE": "Ethernet" if phys else ("Loopback" if i.startswith("Loop") else "Vlan"),
                "MAC_ADDRESS": self.mac_of(d, i), "DESCRIPTION": e["desc"],
                "IP_ADDRESS": ip, "PREFIX_LENGTH": plen, "MTU": "9214" if phys else "1500",
                "BANDWIDTH": ("100000000 kbit" if "/" in i else "25000000 kbit") if phys else "",
            })
        return out

    def arp_records(self, d):
        junos = self.style[d] == JUNOS
        out = []
        for i, e in self.ifaces[d].items():
            if not e["ip"] or (d, i) not in self.peer:
                continue
            peer, pif = self.peer[(d, i)]
            peer_ip = self.links[(d, i)]
            if not self.ifaces[peer].get(pif, {}).get("ip"):
                continue
            mac = self.mac_of(peer, pif)
            out.append({"IP_ADDRESS": peer_ip, "MAC_ADDRESS": mac, "INTERFACE": f"{i}.0" if junos else i}
                       if junos else
                       {"ADDRESS": peer_ip, "AGE": "0:00:41", "MAC_ADDRESS": mac, "INTERFACE": i})
        own_if = "irb.500" if junos else "Vlan500"
        for ifn, name, ip, mac in self.servers.get(d, []):
            out.append({"IP_ADDRESS": ip, "MAC_ADDRESS": mac, "INTERFACE": own_if} if junos else
                       {"ADDRESS": ip, "AGE": "0:01:12", "MAC_ADDRESS": mac, "INTERFACE": f"{own_if}, {ifn}"})
        return out

    def mac_records(self, d):
        return [{"DESTINATION_ADDRESS": mac, "TYPE": "DYNAMIC", "VLAN": "500", "DESTINATION_PORT": ifn}
                for ifn, name, ip, mac in self.servers.get(d, [])]

    def version_records(self, d):
        style, role = self.style[d], self.role[d]
        model = MODELS.get((style, role), "DCS-7050CX3-32S")
        serial = "JPE" + hashlib.md5(d.encode()).hexdigest()[:8].upper()
        if style == JUNOS:
            return [{"HOSTNAME": d, "MODEL": model, "JUNOS_VERSION": "22.4R3-S2.11", "SERIAL_NUMBER": serial,
                     "UPTIME": "286 days, 4 hours, 11 minutes"}]
        return [{"MODEL": model, "HW_VERSION": "11.01", "SERIAL_NUMBER": serial, "VERSION": "4.31.2F",
                 "UPTIME": "41 weeks, 2 days, 3 hours and 12 minutes", "SYS_MAC": self.mac_of(d, "sys")}]

    def config_text(self, d):
        if self.style[d] == JUNOS:
            lines = [f"set system host-name {d.split('.')[0]}", "set system domain-name example.net"]
            for i, e in self.ifaces[d].items():
                base = i.split(".")[0]
                if e["desc"]:
                    lines.append(f"set interfaces {base} description \"{e['desc']}\"")
                if e["ip"]:
                    unit = i.split(".")[1] if "." in i else "0"
                    lines.append(f"set interfaces {base} unit {unit} family inet address {e['ip']}")
            lines += ["set protocols ospf area 0.0.0.0 interface lo0.0 passive",
                      "set protocols lldp interface all"]
            return "\n".join(lines) + "\n"
        lines = [f"hostname {d.split('.')[0]}", "ip domain-name example.net", "!"]
        for i, e in self.ifaces[d].items():
            lines.append(f"interface {i}")
            if e["desc"]:
                lines.append(f"   description {e['desc']}")
            if i.startswith("Ethernet") and "/" in i:
                lines += ["   mtu 9214", "   no switchport"]
            if e["ip"]:
                lines.append(f"   ip address {e['ip']}")
                if i.startswith("Ethernet"):
                    lines.append("   ip ospf network point-to-point")
            lines.append("!")
        lines += ["router ospf 1", "   router-id " + (self.ifaces[d].get("Loopback0", {}).get("ip", "").split("/")[0]
                                                       or "0.0.0.0"),
                  "   max-lsa 12000", "!", "lldp run", "!", "end"]
        return "\n".join(lines) + "\n"


def build(before: bool) -> Lab:
    """The demo network. before=True is the state two days ago."""
    L = Lab()
    pod_a = [f"leaf{n:02d}.den1" for n in range(1, 17)] + ([] if before else ["leaf17.den1"])
    pod_b = [f"leaf{n:02d}.den1" for n in range(21, 27)]
    den2 = [f"leaf{n:02d}.den2" for n in range(1, 13)]
    ams = [f"leaf{n:02d}.ams1" for n in range(1, 5)]
    broken = {"leaf07.den1", "leaf08.den1"} if not before else set()
    leaf09_port = "Ethernet9/1" if before else "Ethernet14/1"

    for n in ("spine1.den1", "spine2.den1"):
        L.dev(n, "den1", EOS, "spine")
    for n in pod_a:
        L.dev(n, "den1", EOS_V2, "leaf")
    for n in ("aggb1.den1", "aggb2.den1"):
        L.dev(n, "den1", JUNOS, "spine")
    for n in pod_b:
        L.dev(n, "den1", JUNOS, "leaf")
    L.dev("core1.den1", "den1", JUNOS, "core")
    for n in ("edge1.den1", "edge2.den1"):
        L.dev(n, "den1", JUNOS, "router")
    L.dev("oob1.den1", "den1", EOS, "access")
    for n in ("spine1.den2", "spine2.den2"):
        L.dev(n, "den2", EOS, "spine")
    for n in den2:
        L.dev(n, "den2", EOS_V2, "leaf")
    L.dev("leaf20.den2", "den2", JUNOS, "leaf")
    L.dev("core2.den2", "den2", JUNOS, "core")
    for n in ("spine1.ams1", "spine2.ams1"):
        L.dev(n, "ams1", EOS, "spine")
    for n in ams:
        L.dev(n, "ams1", EOS_V2, "leaf")
    L.dev("edge1.ams1", "ams1", JUNOS, "router")
    # stable indexes (mgmt/loopback/LLDP addresses) across both time points: new devices go last
    order = sorted((d[0] for d in L.devices), key=lambda n: n == "leaf17.den1")
    L.index = {name: i for i, name in enumerate(order, 1)}

    def subnet(name):
        num = int(name[4:6])
        third = {"den1": 10, "den2": 20, "ams1": 30}[name.split(".")[1]]
        return f"10.{third}.{num}.0/24"

    # ── links ──
    for leaf in pod_a:
        if leaf == "leaf17.den1":
            continue  # linked last so p2p addressing is stable across both time points
        n = int(leaf[4:6])
        L.link(leaf, "Ethernet49/1", "spine2.den1", f"Ethernet{n}/1", b_has_ip=leaf not in broken)
        L.link(leaf, "Ethernet50/1", "spine1.den1", f"Ethernet{n}/1")
    for i, leaf in enumerate(pod_b):
        L.link(leaf, "et-0/0/48", "aggb1.den1", f"xe-0/0/{i}")
        L.link(leaf, "et-0/0/49", "aggb2.den1", f"xe-0/0/{i}")
    for s, port in (("spine1.den1", 1), ("spine2.den1", 2)):
        L.link(s, "Ethernet31/1", "edge1.den1", f"et-0/0/{port}")
        L.link(s, "Ethernet32/1", "edge2.den1", f"et-0/0/{port}")
        L.link(s, "Ethernet30/1", "core1.den1", f"et-0/0/{port}")
    for a, port in (("aggb1.den1", 3), ("aggb2.den1", 4)):
        L.link(a, "et-0/0/30", "core1.den1", f"et-0/0/{port}")
    L.link("core1.den1", "et-0/0/10", "edge1.den1", "et-0/0/10")
    L.link("core1.den1", "et-0/0/11", "edge2.den1", "et-0/0/10")
    L.link("core1.den1", "et-0/0/20", "core2.den2", "et-0/0/20")
    L.link("core1.den1", "et-0/0/21", "core2.den2", "et-0/0/21")
    for leaf in den2:
        n = int(leaf[4:6])
        L.link(leaf, "Ethernet49/1", "spine2.den2", f"Ethernet{n}/1")
        L.link(leaf, "Ethernet50/1", "spine1.den2", leaf09_port if leaf == "leaf09.den2" else f"Ethernet{n}/1")
    for s, port in (("spine1.den2", 1), ("spine2.den2", 2)):
        L.link(s, "Ethernet30/1", "core2.den2", f"et-0/0/{port}")
    for leaf in ams:
        n = int(leaf[4:6])
        L.link(leaf, "Ethernet49/1", "spine2.ams1", f"Ethernet{n}/1")
        L.link(leaf, "Ethernet50/1", "spine1.ams1", f"Ethernet{n}/1")
    for s, port in (("spine1.ams1", 1), ("spine2.ams1", 2)):
        L.link(s, "Ethernet31/1", "edge1.ams1", f"et-0/0/{port}")
    L.link("oob1.den1", "Ethernet1", "leaf01.den1", "Ethernet48")
    if "leaf17.den1" in pod_a:
        L.link("leaf17.den1", "Ethernet49/1", "spine2.den1", "Ethernet17/1")
        L.link("leaf17.den1", "Ethernet50/1", "spine1.den1", "Ethernet17/1")
    # leaf20.den2 hangs off an SVI on spine1.den2
    L.iface("spine1.den2", "Vlan200", "LEAF::leaf20.den2 SVI", "10.20.200.1/24")
    L.iface("spine1.den2", "Ethernet20/1", "LEAF::leaf20.den2:et-0/0/48")
    L.iface("leaf20.den2", "et-0/0/48", "SPINE::spine1.den2:Ethernet20/1")
    L.iface("leaf20.den2", "irb.200", "", "10.20.200.20/24")
    L.lldp["leaf20.den2"].append(("et-0/0/48", "spine1.den2", "Ethernet20/1"))
    L.lldp["spine1.den2"].append(("Ethernet20/1", "leaf20.den2", "et-0/0/48"))

    # ams1 quirks: one-sided and unverified links
    L.hide_lldp.add(("spine2.ams1", "leaf04.ams1"))
    L.no_lldp.add("edge1.ams1")

    # ── leafs: own subnet, shared mgmt, servers, default ──
    for n, site, platform, role, style in L.devices:
        if role != "leaf":
            continue
        junos = style == JUNOS
        own_if, mgmt_if = ("irb.500", "irb.99") if junos else ("Vlan500", "Vlan99")
        sn = subnet(n)
        base = sn.rsplit(".", 1)[0]
        L.iface(n, own_if, "servers", base + ".1/24")
        L.iface(n, mgmt_if, "mgmt", f"10.99.{L.index[n]}.1/16")
        L.rt(n, sn, kind="conn", proto="C", ifname=own_if)
        L.rt(n, "10.99.0.0/16", kind="conn", proto="C", ifname=mgmt_if)
        for k in range(1, 4):
            L.server(n, f"ge-0/0/{k}" if junos else f"Ethernet{k}", k, base)
    for leaf in pod_a:
        if leaf in broken:
            L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "Ethernet50/1")], proto="B I")
        else:
            L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "Ethernet49/1"), L.via(leaf, "Ethernet50/1")], proto="B I")
    for leaf in pod_b:
        L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "et-0/0/48"), L.via(leaf, "et-0/0/49")], proto="BGP", sel=[">", ""])
    for leaf in den2 + ams:
        L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "Ethernet49/1"), L.via(leaf, "Ethernet50/1")], proto="B I")
    L.rt("leaf20.den2", "0.0.0.0/0", [("10.20.200.1", "irb.200")], proto="BGP")

    # ── den1 spines (pod A) ──
    for s in ("spine1.den1", "spine2.den1"):
        for leaf in pod_a:
            n = int(leaf[4:6])
            if s == "spine2.den1" and leaf in broken:
                L.rt(s, subnet(leaf), [L.via(s, "Ethernet30/1")], proto="O E2")
            else:
                L.rt(s, subnet(leaf), [L.via(s, f"Ethernet{n}/1")], proto="O")
        for leaf in pod_b:
            L.rt(s, subnet(leaf), [L.via(s, "Ethernet30/1")], proto="O")
        L.rt(s, "10.20.0.0/16", [L.via(s, "Ethernet30/1")], proto="O E2")
        L.rt(s, "0.0.0.0/0", [L.via(s, "Ethernet31/1"), L.via(s, "Ethernet32/1")], proto="B I")
    # ── pod B aggs ──
    for a in ("aggb1.den1", "aggb2.den1"):
        for i, leaf in enumerate(pod_b):
            L.rt(a, subnet(leaf), [L.via(a, f"xe-0/0/{i}")], proto="OSPF")
        L.rt(a, "0.0.0.0/0", [L.via(a, "et-0/0/30")], proto="OSPF")
    # ── core1: pod B specific, the rest of den1 only as an aggregate via the edges ──
    for leaf in pod_b:
        L.rt("core1.den1", subnet(leaf), [L.via("core1.den1", "et-0/0/3"), L.via("core1.den1", "et-0/0/4")],
             proto="OSPF", sel=[">", ">"])
    L.rt("core1.den1", "10.10.0.0/16", [L.via("core1.den1", "et-0/0/10"), L.via("core1.den1", "et-0/0/11")],
         proto="OSPF", sel=[">", ">"])
    L.rt("core1.den1", "10.20.0.0/16", [L.via("core1.den1", "et-0/0/20"), L.via("core1.den1", "et-0/0/21")],
         proto="OSPF", sel=[">", ">"])
    L.rt("core1.den1", "0.0.0.0/0", [L.via("core1.den1", "et-0/0/10"), L.via("core1.den1", "et-0/0/11")],
         proto="OSPF", sel=[">", ">"])
    # ── den1 edges ──
    for e, transit in (("edge1.den1", "198.51.100.1"), ("edge2.den1", "198.51.100.5")):
        for leaf in pod_a:
            if leaf in broken:
                L.rt(e, subnet(leaf), [L.via(e, "et-0/0/1")], proto="OSPF")
            else:
                L.rt(e, subnet(leaf), [L.via(e, "et-0/0/1"), L.via(e, "et-0/0/2")], proto="OSPF", sel=[">", ">"])
        for leaf in pod_b:
            L.rt(e, subnet(leaf), [L.via(e, "et-0/0/10")], proto="OSPF")
        L.rt(e, "10.20.0.0/16", [L.via(e, "et-0/0/10")], proto="OSPF")
        L.rt(e, "0.0.0.0/0", [(transit, "et-0/1/0.0"), ("203.0.113.9", "et-0/1/1.0")], proto="BGP", sel=[">", ""])
        L.iface(e, "et-0/1/0", "TRANSIT::as64500", transit.rsplit(".", 1)[0] + f".{int(transit.rsplit('.', 1)[1]) + 1}/30")
    # ── den2 ──
    for s in ("spine1.den2", "spine2.den2"):
        for leaf in den2:
            n = int(leaf[4:6])
            if leaf == "leaf12.den2":
                continue
            port = leaf09_port if (s == "spine1.den2" and leaf == "leaf09.den2") else f"Ethernet{n}/1"
            L.rt(s, subnet(leaf), [L.via(s, port)], proto="O")
        L.rt(s, "0.0.0.0/0", [L.via(s, "Ethernet30/1")], proto="O E2")
    L.rt("spine1.den2", "10.20.20.0/24", [("10.20.200.20", "Vlan200")], proto="B E")
    L.rt("spine2.den2", "10.20.20.0/24", [L.via("spine2.den2", "Ethernet30/1")], proto="O E2")
    for leaf in den2:
        if leaf != "leaf12.den2":
            L.rt("core2.den2", subnet(leaf), [L.via("core2.den2", "et-0/0/1"), L.via("core2.den2", "et-0/0/2")],
                 proto="OSPF", sel=[">", ">"])
    L.rt("core2.den2", "10.20.20.0/24", [L.via("core2.den2", "et-0/0/1")], proto="OSPF")
    L.rt("core2.den2", "10.20.0.0/16", kind="drop", proto="Aggregate")
    L.rt("core2.den2", "10.10.0.0/16", [L.via("core2.den2", "et-0/0/20"), L.via("core2.den2", "et-0/0/21")],
         proto="OSPF", sel=[">", ">"])
    # ── ams1 ──
    for s in ("spine1.ams1", "spine2.ams1"):
        for leaf in ams:
            L.rt(s, subnet(leaf), [L.via(s, f"Ethernet{int(leaf[4:6])}/1")], proto="O")
        L.rt(s, "0.0.0.0/0", [L.via(s, "Ethernet31/1")], proto="O E2")
    for leaf in ams:
        L.rt("edge1.ams1", subnet(leaf), [L.via("edge1.ams1", "et-0/0/1"), L.via("edge1.ams1", "et-0/0/2")],
             proto="OSPF", sel=[">", ">"])
    L.rt("edge1.ams1", "0.0.0.0/0", [("203.0.113.33", "et-0/1/0.0")], proto="BGP")
    L.rt("oob1.den1", "0.0.0.0/0", [L.via("oob1.den1", "Ethernet1")], proto="S")

    # Junos Local /32s, EOS loopbacks
    for name, site, platform, role, style in L.devices:
        i = L.index[name]
        if style == JUNOS:
            L.iface(name, "lo0.0", "", f"10.255.255.{i}/32")
            for ifn, e in L.ifaces[name].items():
                if e["ip"]:
                    L.rt(name, e["ip"].split("/")[0] + "/32", kind="local", proto="Local",
                         ifname=ifn if "." in ifn else ifn + ".0")
        else:
            L.iface(name, "Loopback0", "router-id", f"10.255.255.{i}/32")
            L.rt(name, f"10.255.255.{i}/32", kind="conn", proto="C", ifname="Loopback0")
    return L


def _store(storage, site, name, capture, records, when, command, raw=None):
    storage.store(Snapshot(device=name, site=site, capture_type=capture, command=command,
                           raw_text=raw if raw is not None else "\n".join(repr(r) for r in records) + "\n",
                           parsed_data={"records": records} if records is not None else None,
                           collected_at=when))


COMMANDS = {
    JUNOS: {"routes": "show route", "lldp": "show lldp neighbors", "ip_interfaces": "show interfaces terse",
            "interfaces": "show interfaces terse", "arp": "show arp no-resolve",
            "mac": "show ethernet-switching table", "version": "show version",
            "config": "show configuration | display set"},
    "eos": {"routes": "show ip route", "lldp": "show lldp neighbors detail", "ip_interfaces": "show ip interface brief",
            "interfaces": "show interfaces", "arp": "show ip arp", "mac": "show mac address-table",
            "version": "show version", "config": "show running-config"},
}


def seed(out: Path, now: datetime):
    if out.exists():
        shutil.rmtree(out)
    (out / "defs").mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parent.parent / "netlapse" / "definitions" / "forwarding.yaml",
                out / "defs" / "forwarding.yaml")
    storage = FileBackend(out / "data")
    storage.initialize()
    db = NetlapseDB(out / "netlapse.db")
    db.init_schema()

    lab_before, lab_now = build(before=True), build(before=False)
    plats = {p["slug"]: p["id"] for p in db.list_platforms()}
    mfr = {"arista_eos": "Arista", "juniper_junos": "Juniper"}
    c = db.conn
    for s, name in SITE_NAMES.items():
        c.execute("INSERT OR IGNORE INTO dcim_site (name, slug) VALUES (?, ?)", (name, s))
    for r in ("leaf", "spine", "core", "router", "access"):
        c.execute("INSERT OR IGNORE INTO dcim_device_role (name, slug) VALUES (?, ?)", (r.title(), r))
    c.commit()
    sites = {r["slug"]: r["id"] for r in c.execute("SELECT id, slug FROM dcim_site")}
    roles = {r["slug"]: r["id"] for r in c.execute("SELECT id, slug FROM dcim_device_role")}
    for name, site, platform, role, style in lab_now.devices:
        db.create_device(name, sites[site], platform_id=plats[platform], role_id=roles[role],
                         status="active", primary_ip4=f"192.0.2.{lab_now.index[name]}")
    del mfr

    then = now - timedelta(days=2)
    epoch_then = (then - datetime(1970, 1, 1)).total_seconds()

    for lab, when in ((lab_before, then), (lab_now, now)):
        for name, site, platform, role, style in lab.devices:
            t = when - timedelta(hours=8) if (lab is lab_now and name == "edge2.den1") else when
            cmd = COMMANDS[JUNOS if style == JUNOS else "eos"]
            put = lambda ct, recs, raw=None: _store(storage, site, name, ct, recs, t, cmd[ct], raw)  # noqa: E731
            put("routes", lab.route_records(name))
            if name not in lab.no_lldp:
                put("lldp", lab.lldp_records(name))
            put("ip_interfaces", lab.addr_records(name))
            put("interfaces", lab.interface_records(name))
            put("arp", lab.arp_records(name))
            if role == "leaf":
                put("mac", lab.mac_records(name))
            put("version", lab.version_records(name))
            put("config", None, lab.config_text(name))
            if lab is lab_before:
                ddir = storage.base_path / site / name
                for f in ddir.glob("*.*"):
                    os.utime(f, (epoch_then, epoch_then))

    (out / "config.yaml").write_text(
        f"storage:\n  backend: file\n  path: {out / 'data'}\n"
        f"dcim_db: {out / 'netlapse.db'}\n"
        f"definitions_dir: {out / 'defs'}\n"
        f"listen:\n  host: 127.0.0.1\n  port: 8898\n"
    )
    return lab_now, then


def main():
    ap = argparse.ArgumentParser(description="Build the synthetic demo network")
    ap.add_argument("--out", default="/tmp/netlapse-demo")
    args = ap.parse_args()
    out = Path(args.out).expanduser().resolve()
    lab, then = seed(out, datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0))
    print(f"demo lab: {len(lab.devices)} devices in {out}")
    print(f"history:  {then.isoformat()}Z (as_of an hour later shows leaf07/08.den1 dual-homed, no leaf17.den1)")
    print(f"run:      NETLAPSE_CONFIG={out / 'config.yaml'} NETLAPSE_ADMIN_PASSWORD=demo-pass-1 "
          f"NETLAPSE_SKIP_TEMPLATE_DOWNLOAD=1 python -m netlapse serve")


if __name__ == "__main__":
    main()
