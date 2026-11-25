"""
Forwarding model tests.

A two-site lab stored in a FileBackend the way the collector stores it, with
route records in each template family's own shape:

  iad1                                     iad2
  tor-a, tor-b  EOS v2 (fixed)  ECMP       tor-x  Junos, default via SVI to agg9
  tor-c         EOS v2, single-homed       tor-z  EOS original, subnet hidden by a
  agg1, agg2    EOS original template             Reject aggregate on core2
  edge1         Junos, default to transit  agg9   EOS original, SVI to tor-x
  core          Junos                      core2  Junos
  oob1          excluded by name           tor-x and tor-z share mgmt 10.250.0.0/16

Paths:
  iad1 TOR subnets sit under 10.1.0.0/16, iad2 under 10.2.0.0/16.
  aggs send 10.2/16 to core; core sends 10.1/16 to edge1 (so iad2 -> iad1
  crosses edge1 and iad1 -> iad2 doesn't); agg2 reaches tor-c the long way.
"""

import os
from datetime import datetime, timedelta

import pytest

from netlapse.forwarding import ForwardingConfig, ForwardingModel, build_matrix, trace
from netlapse.forwarding.normalize import normalize_routes, owned_from_records
from netlapse.storage.backend import Snapshot
from netlapse.storage.file_backend import FileBackend
from netlapse.topology.history import CaptureReader

FMT = {}
LINKS = {}
ROUTES = {}
LLDP = {}
ADDR = {}
_n = [0]


def _dev(i, name, site, platform, role):
    return {"id": i, "name": name, "site_slug": site, "platform_slug": platform,
            "role_slug": role, "primary_ip4": f"192.0.2.{i}", "status": "active"}


DEVICES = [
    _dev(1, "tor-a.iad1", "iad1", "arista_eos", "leaf"),
    _dev(2, "tor-b.iad1", "iad1", "arista_eos", "leaf"),
    _dev(3, "tor-c.iad1", "iad1", "arista_eos", "leaf"),
    _dev(4, "agg1.iad1", "iad1", "arista_eos", "distribution"),
    _dev(5, "agg2.iad1", "iad1", "arista_eos", "distribution"),
    _dev(6, "edge1.iad1", "iad1", "juniper_junos", "router"),
    _dev(7, "core.iad1", "iad1", "juniper_junos", "core"),
    _dev(8, "oob1.iad1", "iad1", "arista_eos", "leaf"),
    _dev(9, "tor-x.iad2", "iad2", "juniper_junos", "leaf"),
    _dev(10, "tor-z.iad2", "iad2", "arista_eos", "leaf"),
    _dev(11, "agg9.iad2", "iad2", "arista_eos", "distribution"),
    _dev(12, "core2.iad2", "iad2", "juniper_junos", "core"),
]
STYLE = {"tor-a.iad1": "v2", "tor-b.iad1": "v2", "tor-c.iad1": "v2", "agg1.iad1": "orig",
         "agg2.iad1": "orig", "edge1.iad1": "junos", "core.iad1": "junos", "oob1.iad1": "orig",
         "tor-x.iad2": "junos", "tor-z.iad2": "orig", "agg9.iad2": "orig", "core2.iad2": "junos"}


def link(a, ai, b, bi):
    k = _n[0]
    _n[0] += 2
    ipa, ipb = f"10.255.0.{k}", f"10.255.0.{k + 1}"
    LINKS[(a, ai)] = ipb
    LINKS[(b, bi)] = ipa
    ADDR.setdefault(a, []).append((ai, f"{ipa}/31"))
    ADDR.setdefault(b, []).append((bi, f"{ipb}/31"))
    LLDP.setdefault(a, []).append((ai, b))
    LLDP.setdefault(b, []).append((bi, a))


def via(d, ifn):
    return (LINKS[(d, ifn)], ifn)


def rt(d, prefix, hops=(), kind="fwd", proto="O", ifname=None, sel=None):
    ROUTES.setdefault(d, []).append({"prefix": prefix, "hops": list(hops), "kind": kind,
                                     "proto": proto, "if": ifname, "sel": sel})


def _build_lab():
    for x in (FMT, LINKS, ROUTES, LLDP, ADDR):
        x.clear()
    _n[0] = 0
    link("tor-a.iad1", "Ethernet49/1", "agg2.iad1", "Ethernet10/1")
    link("tor-a.iad1", "Ethernet50/1", "agg1.iad1", "Ethernet10/1")
    link("tor-b.iad1", "Ethernet49/1", "agg2.iad1", "Ethernet11/1")
    link("tor-b.iad1", "Ethernet50/1", "agg1.iad1", "Ethernet11/1")
    link("tor-c.iad1", "Ethernet50/1", "agg1.iad1", "Ethernet20/1")
    link("agg1.iad1", "Ethernet1/1", "edge1.iad1", "et-0/0/1")
    link("agg2.iad1", "Ethernet1/1", "edge1.iad1", "et-0/0/2")
    link("agg1.iad1", "Ethernet3/1", "core.iad1", "xe-0/0/1")
    link("agg2.iad1", "Ethernet3/1", "core.iad1", "xe-0/0/2")
    link("edge1.iad1", "et-0/0/3", "core.iad1", "xe-0/0/3")
    link("core.iad1", "xe-0/0/9", "core2.iad2", "xe-0/0/9")
    link("core2.iad2", "xe-0/0/1", "agg9.iad2", "Ethernet60/1")
    link("agg9.iad2", "Ethernet61/1", "tor-z.iad2", "Ethernet49/1")
    # SVI between agg9 (Vlan505 100.64.0.1) and tor-x (irb.505 100.64.0.5)
    ADDR.setdefault("agg9.iad2", []).append(("Vlan505", "100.64.0.1/24"))
    ADDR.setdefault("tor-x.iad2", []).append(("irb.505", "100.64.0.5/24"))
    LLDP.setdefault("tor-x.iad2", []).append(("et-0/0/49", "agg9.iad2"))
    LLDP.setdefault("agg9.iad2", []).append(("Ethernet63/1", "tor-x.iad2"))
    LLDP.setdefault("oob1.iad1", []).append(("Ethernet1", "tor-a.iad1"))

    subnets = {"tor-a.iad1": "10.1.1.0/24", "tor-b.iad1": "10.1.2.0/24", "tor-c.iad1": "10.1.3.0/24",
               "tor-x.iad2": "10.2.1.0/24", "tor-z.iad2": "10.2.9.0/24", "oob1.iad1": "10.1.99.0/24"}
    for t, s in subnets.items():
        rt(t, s, kind="conn", proto="C", ifname="irb.500" if STYLE[t] == "junos" else "Vlan500")
    rt("tor-x.iad2", "10.250.0.0/16", kind="conn", proto="C", ifname="irb.127")
    rt("tor-z.iad2", "10.250.0.0/16", kind="conn", proto="C", ifname="Vlan127")
    rt("tor-a.iad1", "10.1.1.1/32", kind="conn", proto="C", ifname="Loopback0")

    for t in ("tor-a.iad1", "tor-b.iad1"):
        rt(t, "0.0.0.0/0", [via(t, "Ethernet49/1"), via(t, "Ethernet50/1")], proto="B I")
    rt("tor-c.iad1", "0.0.0.0/0", [via("tor-c.iad1", "Ethernet50/1")], proto="B I")
    for a in ("agg1.iad1", "agg2.iad1"):
        rt(a, "10.1.1.0/24", [via(a, "Ethernet10/1")])
        rt(a, "10.1.2.0/24", [via(a, "Ethernet11/1")])
        rt(a, "10.2.0.0/16", [via(a, "Ethernet3/1")], proto="O E2")
        rt(a, "0.0.0.0/0", [via(a, "Ethernet1/1")], proto="B I")
    rt("agg1.iad1", "10.1.3.0/24", [via("agg1.iad1", "Ethernet20/1")])
    rt("agg2.iad1", "10.1.3.0/24", [via("agg2.iad1", "Ethernet1/1")])
    for p in ("10.1.1.0/24", "10.1.2.0/24"):
        rt("edge1.iad1", p, [via("edge1.iad1", "et-0/0/1"), via("edge1.iad1", "et-0/0/2")], proto="OSPF", sel=[">", ">"])
    rt("edge1.iad1", "10.1.3.0/24", [via("edge1.iad1", "et-0/0/1")], proto="OSPF")
    rt("edge1.iad1", "10.2.0.0/16", [via("edge1.iad1", "et-0/0/3")], proto="OSPF")
    rt("edge1.iad1", "0.0.0.0/0", [("203.0.113.1", "et-0/1/0.0"), ("203.0.113.5", "et-0/1/1.0")], proto="BGP", sel=[">", ""])
    rt("core.iad1", "10.1.0.0/16", [via("core.iad1", "xe-0/0/3")], proto="OSPF")
    rt("core.iad1", "10.2.0.0/16", [via("core.iad1", "xe-0/0/9")], proto="OSPF")
    rt("core2.iad2", "10.2.1.0/24", [via("core2.iad2", "xe-0/0/1")], proto="OSPF")
    rt("core2.iad2", "10.2.0.0/16", kind="drop", proto="Aggregate")
    rt("core2.iad2", "10.1.0.0/16", [via("core2.iad2", "xe-0/0/9")], proto="OSPF")
    rt("agg9.iad2", "10.2.1.0/24", [("100.64.0.5", "Vlan505")], proto="B E")
    rt("agg9.iad2", "0.0.0.0/0", [via("agg9.iad2", "Ethernet60/1")], proto="O E2")
    rt("tor-x.iad2", "0.0.0.0/0", [("100.64.0.1", "irb.505")], proto="BGP")
    rt("tor-z.iad2", "0.0.0.0/0", [via("tor-z.iad2", "Ethernet49/1")], proto="B E")
    rt("oob1.iad1", "0.0.0.0/0", [via("tor-a.iad1", "Ethernet49/1")], proto="S")
    for d, lst in ADDR.items():
        if STYLE[d] == "junos":
            for ifn, ipl in lst:
                rt(d, ipl.split("/")[0] + "/32", kind="local", proto="Local", ifname=ifn + ".0" if "." not in ifn else ifn)


def route_records(d):
    out = []
    for r in ROUTES.get(d, []):
        nh = [h for h, _ in r["hops"]]
        ifs = [i for _, i in r["hops"]]
        style = STYLE[d]
        if style == "junos":
            rec = {"TABLE": "inet.0", "PREFIX": r["prefix"], "ACTIVE": "*", "PROTOCOL": r["proto"],
                   "PREFERENCE": "10", "AGE": "1d 00:00:01", "LOCALPREF": "", "AS_PATH": "",
                   "SELECTED": [], "NEXT_HOP": [], "INTERFACE": [], "NH_TYPE": ""}
            if r["kind"] == "conn":
                rec.update(PROTOCOL="Direct", SELECTED=[">"], INTERFACE=[r["if"]])
            elif r["kind"] == "local":
                rec.update(NH_TYPE="Local", INTERFACE=[r["if"]])
            elif r["kind"] == "drop":
                rec.update(NH_TYPE="Reject")
            else:
                rec.update(NEXT_HOP=nh, INTERFACE=[i if "." in i else i + ".0" for i in ifs],
                           SELECTED=r["sel"] or [">"] * len(nh))
        elif style == "v2":
            rec = {"VRF": "default", "PREFIX": r["prefix"], "PROTOCOL": r["proto"], "ADMIN_DIST": "", "METRIC": "",
                   "NEXT_HOP": ["connected"] if r["kind"] == "conn" else nh,
                   "INTERFACE": [r["if"]] if r["kind"] == "conn" else ifs}
        else:
            net, plen = r["prefix"].split("/")
            rec = {"VRF": "default", "PROTOCOL": r["proto"], "NETWORK": net, "PREFIX_LENGTH": plen,
                   "DISTANCE": "", "METRIC": "", "DIRECT": "directly" if r["kind"] == "conn" else "",
                   "NEXT_HOP": ["connected"] if r["kind"] == "conn" else nh,
                   "INTERFACE": [r["if"]] if r["kind"] == "conn" else ifs}
        out.append(rec)
    return out


def put(storage, site, name, capture, records):
    storage.store(Snapshot(device=name, site=site, capture_type=capture, command="x",
                           raw_text=repr(records), parsed_data={"records": records},
                           collected_at=datetime.utcnow()))


def seed(storage):
    _build_lab()
    for d in DEVICES:
        n, s = d["name"], d["site_slug"]
        put(storage, s, n, "routes", route_records(n))
        put(storage, s, n, "lldp", [{"LOCAL_INTERFACE": i, "NEIGHBOR_NAME": p + ".example.net",
                                     **({"PARENT_INTERFACE": "-"} if STYLE[n] == "junos" else {})}
                                    for i, p in LLDP.get(n, [])])
        if STYLE[n] == "junos":
            put(storage, s, n, "ip_interfaces", [{"INTERFACE": i, "LOCAL": a} for i, a in ADDR.get(n, [])])
        else:
            put(storage, s, n, "ip_interfaces", [{"INTERFACE": i, "IP_ADDRESS": a} for i, a in ADDR.get(n, [])])


@pytest.fixture
def storage(tmp_path):
    s = FileBackend(tmp_path / "data")
    s.initialize()
    seed(s)
    return s


@pytest.fixture
def cfg():
    return ForwardingConfig.from_dict({})


@pytest.fixture
def model(storage, cfg):
    return ForwardingModel(DEVICES, CaptureReader(storage), cfg)


@pytest.fixture
def matrix(model):
    return build_matrix(model)


# ── normalization ──────────────────────────────────────────────────────────

def test_eos_v2_connected_and_ecmp():
    recs = [
        {"VRF": "default", "PREFIX": "0.0.0.0/0", "PROTOCOL": "B I", "NEXT_HOP": ["10.0.0.1", "10.0.0.3"],
         "INTERFACE": ["Ethernet49/1", "Ethernet50/1"]},
        {"VRF": "default", "PREFIX": "10.7.22.0/24", "PROTOCOL": "C", "NEXT_HOP": ["connected"], "INTERFACE": ["Vlan500"]},
        {"VRF": "default", "PREFIX": "10.7.0.0/16", "PROTOCOL": "A B", "NEXT_HOP": ["connected"], "INTERFACE": ["Null0"]},
        {"VRF": "MGMT", "PREFIX": "0.0.0.0/0", "PROTOCOL": "S", "NEXT_HOP": ["10.9.9.1"], "INTERFACE": ["Management1"]},
    ]
    by_vrf, info = normalize_routes(recs)
    kinds = {(r[1], r[2]) for r in by_vrf["default"]}
    assert kinds == {(0, "fwd"), (24, "conn"), (16, "drop")}
    assert len(next(r for r in by_vrf["default"] if r[1] == 0)[4]) == 2
    assert "MGMT" in by_vrf and info["family"] == "eos" and not info["problem"]


def test_eos_without_connected_flags_old_template():
    recs = [{"VRF": "default", "PREFIX": "0.0.0.0/0", "PROTOCOL": "B I", "NEXT_HOP": "", "INTERFACE": ""},
            {"VRF": "default", "PREFIX": "10.1.1.0/24", "PROTOCOL": "B I", "NEXT_HOP": "10.0.0.1", "INTERFACE": "Ethernet50/1"}]
    _, info = normalize_routes(recs)
    assert "connected" in info["problem"]


def test_junos_fib_only_tables_and_types():
    recs = [
        {"TABLE": "inet.0", "PREFIX": "0.0.0.0/0", "ACTIVE": "*", "PROTOCOL": "BGP", "SELECTED": [">", ""],
         "NEXT_HOP": ["1.1.1.1", "2.2.2.2"], "INTERFACE": ["et-0/1/0.0", "et-0/1/1.0"], "NH_TYPE": ""},
        {"TABLE": "inet.0", "PREFIX": "10.0.0.0/8", "ACTIVE": "*", "PROTOCOL": "Aggregate", "NH_TYPE": "Reject",
         "SELECTED": [], "NEXT_HOP": [], "INTERFACE": []},
        {"TABLE": "inet.0", "PREFIX": "10.7.255.1/32", "ACTIVE": "*", "PROTOCOL": "Local", "NH_TYPE": "Local",
         "SELECTED": [], "NEXT_HOP": [], "INTERFACE": ["et-0/0/1.0"]},
        {"TABLE": "BLUE.inet.0", "PREFIX": "0.0.0.0/0", "ACTIVE": "*", "PROTOCOL": "Static", "NH_TYPE": "",
         "SELECTED": [">"], "NEXT_HOP": ["3.3.3.3"], "INTERFACE": ["ge-0/0/0.0"]},
        {"TABLE": "inet6.0", "PREFIX": "::/0", "ACTIVE": "*", "PROTOCOL": "BGP", "NH_TYPE": "",
         "SELECTED": [">"], "NEXT_HOP": [], "INTERFACE": []},
        {"TABLE": "inet.0", "PREFIX": "10.9.0.0/16", "ACTIVE": "*", "PROTOCOL": "EVPN", "NH_TYPE": "",
         "SELECTED": [">"], "NEXT_HOP": ["10.0.0.9"], "INTERFACE": ["vtep.32769"]},
    ]
    by_vrf, _ = normalize_routes(recs, fib_only=True)
    d = {r[1]: r for r in by_vrf["default"]}
    assert d[0][4] == (("1.1.1.1", "et-0/1/0.0"),)
    assert d[8][2] == "drop" and d[32][2] == "local" and d[16][2] == "overlay"
    assert set(by_vrf) == {"default", "BLUE"}
    by_vrf_all, _ = normalize_routes(recs, fib_only=False)
    assert len({r[1]: r for r in by_vrf_all["default"]}[0][4]) == 2


def test_junos_old_template_is_reported_not_modeled():
    recs = [{"PREFIX": "0.0.0.0/0", "PROTOCOL": "BGP", "PREFERENCE": "170", "AGE": "1d", "LOCALPREF": "", "AS_PATH": ""}]
    by_vrf, info = normalize_routes(recs)
    assert by_vrf == {} and "no next hops" in info["problem"]


def test_cisco_rows_grouped_per_prefix():
    ios = [
        {"VRF": "", "PROTOCOL": "O", "TYPE": "", "NETWORK": "10.1.0.0", "PREFIX_LENGTH": "16",
         "NEXTHOP_IP": "10.0.0.1", "NEXTHOP_IF": "GigabitEthernet0/1"},
        {"VRF": "", "PROTOCOL": "O", "TYPE": "", "NETWORK": "10.1.0.0", "PREFIX_LENGTH": "16",
         "NEXTHOP_IP": "10.0.0.5", "NEXTHOP_IF": "GigabitEthernet0/2"},
        {"VRF": "", "PROTOCOL": "C", "TYPE": "", "NETWORK": "10.0.0.0", "PREFIX_LENGTH": "31",
         "NEXTHOP_IP": "", "NEXTHOP_IF": "GigabitEthernet0/1"},
        {"VRF": "", "PROTOCOL": "L", "TYPE": "", "NETWORK": "10.0.0.0", "PREFIX_LENGTH": "32",
         "NEXTHOP_IP": "", "NEXTHOP_IF": "GigabitEthernet0/1"},
        {"VRF": "", "PROTOCOL": "S", "TYPE": "", "NETWORK": "10.99.0.0", "PREFIX_LENGTH": "16",
         "NEXTHOP_IP": "", "NEXTHOP_IF": "Null0"},
    ]
    by_vrf, info = normalize_routes(ios)
    assert info["family"] == "cisco" and len(by_vrf["default"]) == 4
    ecmp = [r for r in by_vrf["default"] if r[1] == 16 and r[2] == "fwd"][0]
    assert len(ecmp[4]) == 2
    assert sorted(r[2] for r in by_vrf["default"]) == ["conn", "drop", "fwd", "local"]

    nxos = [{"VRF": "default", "PROTOCOL": "direct", "TYPE": "", "NETWORK": "10.5.0.0", "PREFIX_LENGTH": "24",
             "NEXTHOP_IP": "10.5.0.1", "NEXTHOP_IF": "Vlan5"},
            {"VRF": "default", "PROTOCOL": "local", "TYPE": "", "NETWORK": "10.5.0.1", "PREFIX_LENGTH": "32",
             "NEXTHOP_IP": "10.5.0.1", "NEXTHOP_IF": "Vlan5"},
            {"VRF": "default", "PROTOCOL": "ospf-1", "TYPE": "intra", "NETWORK": "10.6.0.0", "PREFIX_LENGTH": "24",
             "NEXTHOP_IP": "10.5.0.9", "NEXTHOP_IF": "Vlan5"}]
    by_vrf, _ = normalize_routes(nxos)
    assert sorted(r[2] for r in by_vrf["default"]) == ["conn", "fwd", "local"]


def test_owned_addresses_from_interface_captures():
    recs = [{"INTERFACE": "Ethernet1", "IP_ADDRESS": "10.0.0.1/31"},
            {"INTERFACE": "Management1", "IP_ADDRESS": "unassigned"},
            {"INTERFACE": "xe-0/0/1.0", "LOCAL": "10.0.0.2/31"},
            {"INTERFACE": "em1.0", "LOCAL": "128.0.0.1/2"}]
    assert owned_from_records(recs) == ["10.0.0.1", "10.0.0.2"]


# ── model ──────────────────────────────────────────────────────────────────

def test_endpoints_destinations_and_exclusion(model):
    assert "oob1.iad1" in model.excluded and "oob1.iad1" not in model.by_node
    assert model.endpoints == ["tor-a.iad1", "tor-b.iad1", "tor-c.iad1", "tor-x.iad2", "tor-z.iad2"]
    dest = {n: d.subnet for n, d in model.destinations.items()}
    # the shared 10.250.0.0/16 mgmt segment is never picked over a TOR's own subnet
    assert dest["tor-x.iad2"] == "10.2.1.0/24" and dest["tor-z.iad2"] == "10.2.9.0/24"
    assert all(d.unique for d in model.destinations.values())


def test_transit_tables_are_filtered(model):
    edge = model.by_node["edge1.iad1"]
    assert edge.lpm.count < edge.routes_total


def test_matrix_statuses(matrix):
    p = matrix.pairs
    assert p[("tor-a.iad1", "tor-b.iad1")]["status"] == "ok"
    assert p[("tor-a.iad1", "tor-b.iad1")]["sym"] is True
    for src in ("tor-a.iad1", "tor-c.iad1", "tor-x.iad2"):
        z = p[(src, "tor-z.iad2")]
        assert z["status"] == "fail" and z["terms"] == ["blackhole"]
    # SVI next hops resolve by address ownership in both directions
    assert p[("tor-a.iad1", "tor-x.iad2")]["status"] == "ok"
    assert p[("tor-x.iad2", "tor-a.iad1")]["status"] == "ok"


def test_single_homed_and_long_way(matrix):
    summ = {s["tor"]: s for s in matrix.endpoint_summary}
    assert summ["tor-c.iad1"]["fanout_max"] == 1
    assert summ["tor-a.iad1"]["fanout_max"] == 2
    fwd = matrix.pairs[("tor-a.iad1", "tor-c.iad1")]
    rev = matrix.pairs[("tor-c.iad1", "tor-a.iad1")]
    assert "edge1.iad1" in fwd["reach"] and "edge1.iad1" not in rev["reach"]
    assert fwd["sym"] is False and fwd["only_fwd"] == ["agg2.iad1", "edge1.iad1"]
    assert fwd["minh"] == 2 and fwd["maxh"] == 4


def test_cross_site_asymmetry_and_match_classes(matrix):
    fwd = matrix.pairs[("tor-a.iad1", "tor-x.iad2")]
    rev = matrix.pairs[("tor-x.iad2", "tor-a.iad1")]
    assert "edge1.iad1" not in fwd["reach"] and "edge1.iad1" in rev["reach"]
    assert rev["sym"] is False
    acts = matrix.actions["tor-a.iad1"]
    assert acts["core.iad1"]["c"] == "aggregate" and acts["tor-x.iad2"]["c"] == "default"
    assert any(x["how"] == "ip" for x in acts["tor-x.iad2"]["x"])


def test_trace_to_transit_exit(storage, cfg):
    model = ForwardingModel(DEVICES, CaptureReader(storage), cfg, extra_destinations=["198.51.100.7"])
    t = trace(model, "tor-a.iad1", "198.51.100.7")
    assert t["status"] == "fail" and t["terms"] == ["exit"]
    assert any("edge1.iad1 -> 203.0.113.1" in e for e in t["exits"])
    assert [l[0]["device"] for l in t["layers"]][:1] == ["tor-a.iad1"]
    t2 = trace(model, "tor-z.iad2", "tor-a.iad1")
    assert t2["status"] == "ok" and t2["dest_endpoint"] == "tor-a.iad1"


def test_out_of_scope_site_is_an_exit(storage, cfg):
    model = ForwardingModel(DEVICES, CaptureReader(storage), cfg, sites=["iad1"])
    m = build_matrix(model)
    assert m.meta["tors"] == 3
    t = trace(ForwardingModel(DEVICES, CaptureReader(storage), cfg, sites=["iad1"],
                              extra_destinations=["10.2.1.10"]), "tor-a.iad1", "10.2.1.10")
    assert t["terms"] == ["exit"] and any("out-of-scope" in str(x) or "core2" in str(x) for x in t["exits"])


def test_as_of_reads_older_routes(tmp_path, cfg):
    s = FileBackend(tmp_path / "hist")
    s.initialize()
    seed(s)
    t0 = datetime(2026, 11, 1, 12, 0, 0)
    old = [r for r in route_records("tor-c.iad1")]
    for r in old:
        if r["PREFIX"] == "0.0.0.0/0":
            r["NEXT_HOP"] = ["10.255.0.99", r["NEXT_HOP"][0]]
            r["INTERFACE"] = ["Ethernet49/1", r["INTERFACE"][0]]
    put(s, "iad1", "tor-c.iad1", "routes", old)
    ADDR.setdefault("agg2.iad1", []).append(("Ethernet20/1", "10.255.0.99/31"))
    put(s, "iad1", "agg2.iad1", "ip_interfaces", [{"INTERFACE": i, "IP_ADDRESS": a} for i, a in ADDR["agg2.iad1"]])
    epoch = (t0 - datetime(1970, 1, 1)).total_seconds()
    for dev_site, dev_name, cap in (("iad1", "tor-c.iad1", "routes"), ("iad1", "agg2.iad1", "ip_interfaces")):
        for ext in ("txt", "json"):
            os.utime(s.base_path / dev_site / dev_name / f"{cap}.{ext}", (epoch, epoch))
    for d in DEVICES:
        for cap in ("routes", "lldp", "ip_interfaces"):
            for ext in ("txt", "json"):
                p = s.base_path / d["site_slug"] / d["name"] / f"{cap}.{ext}"
                if p.exists() and p.stat().st_mtime > epoch:
                    os.utime(p, (epoch, epoch))
    # now: tor-c single-homed again
    _build_lab()
    put(s, "iad1", "tor-c.iad1", "routes", route_records("tor-c.iad1"))

    then = build_matrix(ForwardingModel(DEVICES, CaptureReader(s, t0 + timedelta(days=1)), cfg))
    now = build_matrix(ForwardingModel(DEVICES, CaptureReader(s), cfg))
    fan_then = {x["tor"]: x["fanout_max"] for x in then.endpoint_summary}
    fan_now = {x["tor"]: x["fanout_max"] for x in now.endpoint_summary}
    assert fan_then["tor-c.iad1"] == 2 and fan_now["tor-c.iad1"] == 1


def test_list_destinations_and_config_validation(storage):
    cfg = ForwardingConfig.from_dict({"forwarding": {"destinations": {
        "source": "list", "list": [{"device": "tor-a.iad1", "prefix": "10.1.1.0/24"},
                                   {"device": "tor-b.iad1", "prefix": "10.1.2.0/24"}]}}})
    m = build_matrix(ForwardingModel(DEVICES, CaptureReader(storage), cfg))
    assert m.meta["tors"] == 2 and m.meta["pairs"] == 2
    with pytest.raises(ValueError):
        ForwardingConfig.from_dict({"forwarding": {"destinations": {"within": ["not-a-prefix"]}}})


def test_report_renders(matrix, model):
    from netlapse.forwarding.report import render_html
    data = matrix.to_dict(model)
    data["meta"]["built_at"] = "now"
    html = render_html(data)
    assert html.startswith("<!DOCTYPE html>") and "</script>" in html and "tor-c.iad1" in html
