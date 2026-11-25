#!/usr/bin/env python3
"""
demo_lab.py — a synthetic two-site network for demos, screenshots and docs.

Builds a self-contained netlapse instance (DCIM database, file storage,
definitions, config.yaml) with no real hostnames or addresses: 2 sites,
37 leafs, spines, a Junos pod, cores and edges. Route, LLDP and
interface-address captures are written in the shapes the parse templates
produce, so the forwarding model, Topology and Changes all have real data.

Planted findings (what a demo walks through):
  leaf07.den1, leaf08.den1  single-homed: their spine2 ports were never
                            finished, so spine2 reaches them the long way
                            (core1 -> edge -> spine1). Two days ago they
                            were dual-homed: the --as-of history shows it.
  den2 -> den1              crosses the edges; den1 -> den2 doesn't
                            (core1 carries den1 only as an aggregate via
                            the edges): cross-site asymmetry.
  leaf21-26.den1 (Junos)    RIB lists both uplinks, only one is installed
                            (no load-balance policy): single first hop.
  leaf12.den2               subnet never advertised; core2 holds a Reject
                            aggregate for 10.20.0.0/16: black hole.
  leaf20.den2 (Junos)       uplink is an SVI; resolved by address ownership.
  edge2.den1                route capture 8 hours behind the rest: stale.
  every leaf                also carries the shared mgmt 10.99.0.0/16.

Usage:
    python tools/demo_lab.py --out /tmp/netlapse-demo
    NETLAPSE_CONFIG=/tmp/netlapse-demo/config.yaml NETLAPSE_ADMIN_PASSWORD=demo-pass-1 NETLAPSE_SKIP_TEMPLATE_DOWNLOAD=1 python -m netlapse serve
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netlapse.dcim.db_schema import NetlapseDB  # noqa: E402
from netlapse.storage.backend import Snapshot  # noqa: E402
from netlapse.storage.file_backend import FileBackend  # noqa: E402

EOS_V2, EOS, JUNOS = "eos_v2", "eos", "junos"


class Lab:
    def __init__(self):
        self.devices = []          # (name, site, platform, role, style)
        self.style = {}
        self.links = {}            # (dev, if) -> peer ip
        self.addr = {}             # dev -> [(if, ip/len)]
        self.lldp = {}             # dev -> [(if, peer)]
        self.routes = {}           # dev -> [route dicts]
        self._p2p = 0

    def dev(self, name, site, style, role):
        platform = "juniper_junos" if style == JUNOS else "arista_eos"
        self.devices.append((name, site, platform, role, style))
        self.style[name] = style
        self.routes.setdefault(name, [])
        self.lldp.setdefault(name, [])
        self.addr.setdefault(name, [])

    def link(self, a, ai, b, bi, b_has_ip=True, lldp=True):
        n = self._p2p
        self._p2p += 2
        ipa = f"10.254.{n // 256}.{n % 256}"
        ipb = f"10.254.{(n + 1) // 256}.{(n + 1) % 256}"
        self.links[(a, ai)] = ipb
        self.links[(b, bi)] = ipa
        self.addr[a].append((ai, f"{ipa}/31"))
        if b_has_ip:
            self.addr[b].append((bi, f"{ipb}/31"))
        if lldp:
            self.lldp[a].append((ai, b))
            self.lldp[b].append((bi, a))

    def via(self, d, ifn):
        return (self.links[(d, ifn)], ifn)

    def rt(self, d, prefix, hops=(), kind="fwd", proto="O", ifname=None, sel=None):
        self.routes[d].append({"prefix": prefix, "hops": list(hops), "kind": kind,
                               "proto": proto, "if": ifname, "sel": sel})

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
        return [{"LOCAL_INTERFACE": i, "NEIGHBOR_NAME": f"{p}.example.net", "NEIGHBOR_INTERFACE": "",
                 **({"PARENT_INTERFACE": "-"} if junos else {})} for i, p in self.lldp[d]]

    def addr_records(self, d):
        if self.style[d] == JUNOS:
            return [{"INTERFACE": i if "." in i else i + ".0", "LOCAL": a, "STATUS": "up"} for i, a in self.addr[d]]
        return [{"INTERFACE": i, "IP_ADDRESS": a, "STATUS": "up", "PROTO": "up"} for i, a in self.addr[d]]


def build(before: bool) -> Lab:
    """The demo network. before=True is the state two days ago (leaf07/08 dual-homed)."""
    L = Lab()
    pod_a = [f"leaf{n:02d}.den1" for n in range(1, 17)]
    pod_b = [f"leaf{n:02d}.den1" for n in range(21, 27)]
    den2 = [f"leaf{n:02d}.den2" for n in range(1, 13)]
    broken = {"leaf07.den1", "leaf08.den1"} if not before else set()

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

    def subnet(name):
        num = int(name[4:6])
        return f"10.{10 if name.endswith('den1') else 20}.{num}.0/24"

    # ── links ──
    for i, leaf in enumerate(pod_a):
        L.link(leaf, "Ethernet49/1", "spine2.den1", f"Ethernet{i + 1}/1", b_has_ip=leaf not in broken)
        L.link(leaf, "Ethernet50/1", "spine1.den1", f"Ethernet{i + 1}/1")
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
    for i, leaf in enumerate(den2):
        L.link(leaf, "Ethernet49/1", "spine2.den2", f"Ethernet{i + 1}/1")
        L.link(leaf, "Ethernet50/1", "spine1.den2", f"Ethernet{i + 1}/1")
    for s, port in (("spine1.den2", 1), ("spine2.den2", 2)):
        L.link(s, "Ethernet30/1", "core2.den2", f"et-0/0/{port}")
    L.link("oob1.den1", "Ethernet1", "leaf01.den1", "Ethernet48")
    # leaf20.den2 hangs off an SVI on spine1.den2
    L.addr["spine1.den2"].append(("Vlan200", "10.20.200.1/24"))
    L.addr["leaf20.den2"].append(("irb.200", "10.20.200.20/24"))
    L.lldp["leaf20.den2"].append(("et-0/0/48", "spine1.den2"))
    L.lldp["spine1.den2"].append(("Ethernet20/1", "leaf20.den2"))

    # ── leafs: own subnet, shared mgmt, loopback, default ──
    for n, site, platform, role, style in L.devices:
        if role != "leaf":
            continue
        own_if, mgmt_if = ("irb.500", "irb.99") if style == JUNOS else ("Vlan500", "Vlan99")
        L.rt(n, subnet(n), kind="conn", proto="C", ifname=own_if)
        L.rt(n, "10.99.0.0/16", kind="conn", proto="C", ifname=mgmt_if)
    for leaf in pod_a:
        if leaf in broken:
            L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "Ethernet50/1")], proto="B I")
        else:
            L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "Ethernet49/1"), L.via(leaf, "Ethernet50/1")], proto="B I")
    for leaf in pod_b:
        L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "et-0/0/48"), L.via(leaf, "et-0/0/49")], proto="BGP", sel=[">", ""])
    for leaf in den2:
        L.rt(leaf, "0.0.0.0/0", [L.via(leaf, "Ethernet49/1"), L.via(leaf, "Ethernet50/1")], proto="B I")
    L.rt("leaf20.den2", "0.0.0.0/0", [("10.20.200.1", "irb.200")], proto="BGP")

    # ── den1 spines (pod A) ──
    for s in ("spine1.den1", "spine2.den1"):
        for i, leaf in enumerate(pod_a):
            if s == "spine2.den1" and leaf in broken:
                L.rt(s, subnet(leaf), [L.via(s, "Ethernet30/1")], proto="O E2")
            else:
                L.rt(s, subnet(leaf), [L.via(s, f"Ethernet{i + 1}/1")], proto="O")
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
    L.rt("core1.den1", "10.20.0.0/16", [L.via("core1.den1", "et-0/0/20")], proto="OSPF")
    L.rt("core1.den1", "0.0.0.0/0", [L.via("core1.den1", "et-0/0/10"), L.via("core1.den1", "et-0/0/11")],
         proto="OSPF", sel=[">", ">"])
    # ── edges ──
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
    # ── den2 ──
    for s in ("spine1.den2", "spine2.den2"):
        for i, leaf in enumerate(den2):
            if leaf != "leaf12.den2":
                L.rt(s, subnet(leaf), [L.via(s, f"Ethernet{i + 1}/1")], proto="O")
        L.rt(s, "0.0.0.0/0", [L.via(s, "Ethernet30/1")], proto="O E2")
    L.rt("spine1.den2", "10.20.20.0/24", [("10.20.200.20", "Vlan200")], proto="B E")
    L.rt("spine2.den2", "10.20.20.0/24", [L.via("spine2.den2", "Ethernet30/1")], proto="O E2")
    for leaf in den2:
        if leaf != "leaf12.den2":
            L.rt("core2.den2", subnet(leaf), [L.via("core2.den2", "et-0/0/1"), L.via("core2.den2", "et-0/0/2")],
                 proto="OSPF", sel=[">", ">"])
    L.rt("core2.den2", "10.20.20.0/24", [L.via("core2.den2", "et-0/0/1")], proto="OSPF")
    L.rt("core2.den2", "10.20.0.0/16", kind="drop", proto="Aggregate")
    L.rt("core2.den2", "10.10.0.0/16", [L.via("core2.den2", "et-0/0/20")], proto="OSPF")
    L.rt("oob1.den1", "0.0.0.0/0", [L.via("oob1.den1", "Ethernet1")], proto="S")

    # Junos Local /32s, EOS loopbacks
    for i, (n, site, platform, role, style) in enumerate(L.devices, 1):
        if style == JUNOS:
            for ifn, ipl in L.addr[n]:
                L.rt(n, ipl.split("/")[0] + "/32", kind="local", proto="Local",
                     ifname=ifn if "." in ifn else ifn + ".0")
        else:
            L.rt(n, f"10.255.255.{i}/32", kind="conn", proto="C", ifname="Loopback0")
    return L


def _store(storage, site, name, capture, records, when, command):
    storage.store(Snapshot(device=name, site=site, capture_type=capture, command=command,
                           raw_text="\n".join(repr(r) for r in records) + "\n",
                           parsed_data={"records": records}, collected_at=when))


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
    c = db.conn
    for s, name in (("den1", "Denver 1"), ("den2", "Denver 2")):
        c.execute("INSERT OR IGNORE INTO dcim_site (name, slug) VALUES (?, ?)", (name, s))
    for r in ("leaf", "spine", "core", "router", "access"):
        c.execute("INSERT OR IGNORE INTO dcim_device_role (name, slug) VALUES (?, ?)", (r.title(), r))
    c.commit()
    sites = {r["slug"]: r["id"] for r in c.execute("SELECT id, slug FROM dcim_site")}
    roles = {r["slug"]: r["id"] for r in c.execute("SELECT id, slug FROM dcim_device_role")}
    for i, (name, site, platform, role, style) in enumerate(lab_now.devices, 1):
        db.create_device(name, sites[site], platform_id=plats[platform], role_id=roles[role],
                         status="active", primary_ip4=f"192.0.2.{i}")

    then = now - timedelta(days=2)
    epoch_then = (then - datetime(1970, 1, 1)).total_seconds()
    cmds = {"routes": "show ip route", "lldp": "show lldp neighbors detail", "ip_interfaces": "show ip interface brief"}

    for lab, when in ((lab_before, then), (lab_now, now)):
        for name, site, platform, role, style in lab.devices:
            t = when - timedelta(hours=8) if (lab is lab_now and name == "edge2.den1") else when
            junos = style == JUNOS
            _store(storage, site, name, "routes", lab.route_records(name), t,
                   "show route" if junos else cmds["routes"])
            _store(storage, site, name, "lldp", lab.lldp_records(name), t,
                   "show lldp neighbors" if junos else cmds["lldp"])
            _store(storage, site, name, "ip_interfaces", lab.addr_records(name), t,
                   "show interfaces terse" if junos else cmds["ip_interfaces"])
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
    lab, then = seed(out, datetime.utcnow().replace(microsecond=0))
    print(f"demo lab: {len(lab.devices)} devices in {out}")
    print(f"history:  as_of {then.isoformat()}Z+1h shows leaf07/08.den1 dual-homed")
    print(f"run:      NETLAPSE_CONFIG={out / 'config.yaml'} NETLAPSE_ADMIN_PASSWORD=demo-pass-1 "
          f"NETLAPSE_SKIP_TEMPLATE_DOWNLOAD=1 python -m netlapse serve")


if __name__ == "__main__":
    main()
