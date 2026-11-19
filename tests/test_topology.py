"""
Tests for the LLDP topology builder (netlapse/topology).

Lab names (eng-*) and documentation address space only. Record columns are
the ones the shipped templates emit:

    arista_eos_show_lldp_neighbors_detail   LOCAL_INTERFACE, NEIGHBOR_NAME,
                                            NEIGHBOR_INTERFACE, MGMT_ADDRESS,
                                            NEIGHBOR_DESCRIPTION, CHASSIS_ID
    cisco_ios_show_lldp_neighbors_detail    LOCAL_INTERFACE, NEIGHBOR_INTERFACE,
                                            NEIGHBOR_NAME, NEIGHBOR_DESCRIPTION,
                                            MGMT_ADDRESS
    juniper_junos_show_lldp_neighbors       LOCAL_INTERFACE, PARENT_INTERFACE,
                                            CHASSIS_ID, NEIGHBOR_INTERFACE,
                                            NEIGHBOR_NAME
"""

import os
import time
from datetime import datetime, timedelta

import pytest

from netlapse.storage.backend import Snapshot
from netlapse.storage.file_backend import FileBackend
from netlapse.topology import build_topology, diff_topologies
from netlapse.topology.history import CaptureReader, parse_as_of
from netlapse.topology.lldp import normalize_records
from netlapse.topology.names import NameResolver, interface_key


def dev(i, name, site, platform="arista_eos", ip=None):
    return {
        "id": i, "name": name, "site_slug": site, "platform_slug": platform,
        "platform_name": {"arista_eos": "Arista EOS", "cisco_ios": "Cisco IOS",
                          "juniper_junos": "Juniper Junos"}.get(platform, platform),
        "primary_ip4": ip or f"192.0.2.{i}", "role_slug": "switch",
    }


def eos(local, name, port, ip="", desc="Arista Networks EOS"):
    return {"LOCAL_INTERFACE": local, "NEIGHBOR_NAME": name, "CHASSIS_ID": "02aa.bb00.0001",
            "NEIGHBOR_INTERFACE": port, "MGMT_ADDRESS": ip, "NEIGHBOR_DESCRIPTION": desc}


def ios(local, name, port, ip=""):
    return {"LOCAL_INTERFACE": local, "NEIGHBOR_INTERFACE": port, "NEIGHBOR_NAME": name,
            "NEIGHBOR_DESCRIPTION": "Cisco IOS Software", "MGMT_ADDRESS": ip}


def junos(local, name, port, parent=""):
    return {"LOCAL_INTERFACE": local, "PARENT_INTERFACE": parent,
            "CHASSIS_ID": "02:aa:bb:00:00:09", "NEIGHBOR_INTERFACE": port, "NEIGHBOR_NAME": name}


def put(storage, site, name, records, capture="lldp", raw=None):
    storage.store(Snapshot(
        device=name, site=site, capture_type=capture, command="show lldp neighbors detail",
        raw_text=raw if raw is not None else repr(records), parsed_data={"records": records},
        collected_at=datetime.utcnow(),
    ))


def set_mtime(storage, site, name, capture, when):
    epoch = (when - datetime(1970, 1, 1)).total_seconds()
    for ext in ("txt", "json"):
        p = storage.base_path / site / name / f"{capture}.{ext}"
        if p.exists():
            os.utime(p, (epoch, epoch))


@pytest.fixture
def storage(tmp_path):
    s = FileBackend(tmp_path / "data")
    s.initialize()
    return s


@pytest.fixture
def lab(storage):
    devices = [
        dev(1, "eng-spine-01", "lab"),
        dev(2, "eng-leaf-01", "lab", "cisco_ios"),
        dev(3, "eng-edge-01.lab", "lab", "juniper_junos"),
        dev(4, "eng-leaf-02", "lab"),
        dev(5, "eng-fw-01", "lab", "cisco_ios"),        # in DCIM, no LLDP capture
    ]
    put(storage, "lab", "eng-spine-01", [
        eos("Ethernet1", "eng-leaf-01", "Gi0/1"),
        # Junos advertised a port description, not its interface name
        eos("Ethernet2", "eng-edge-01.lab.example.net", "uplink to spine"),
        eos("Ethernet4", "eng-fw-01", "Gi0/3"),
        eos("Ethernet9", "srv-db-07", "eno1", desc="Ubuntu 24.04 lldpd"),
    ])
    put(storage, "lab", "eng-leaf-01", [ios("Gi0/1", "eng-spine-01.lab.example.net", "Ethernet1")])
    put(storage, "lab", "eng-edge-01.lab", [junos("xe-0/0/1", "eng-spine-01", "Ethernet2", parent="ae0")])
    put(storage, "lab", "eng-leaf-02", [eos("Ethernet3", "eng-spine-01", "Ethernet3")])
    return devices


def by_pair(result):
    return {frozenset((l.a, l.b)): l for l in result.links}


# ── normalization ───────────────────────────────────────────────────────

def test_interface_keys_cross_vendor():
    assert interface_key("Ethernet1") == interface_key("Et1")
    assert interface_key("Ethernet1/1") == interface_key("Eth1/1")
    assert interface_key("GigabitEthernet0/1") == interface_key("Gi0/1")
    assert interface_key("TenGigabitEthernet1/1/1") == interface_key("Te1/1/1")
    assert interface_key("xe-0/0/1.0") == interface_key("xe-0/0/1")
    assert interface_key("Port-Channel10") == interface_key("Po10")
    assert interface_key("et-0/0/0") != interface_key("Ethernet0")


def test_normalize_variant_columns_and_lists():
    recs = [
        {"LOCAL_INTERFACE": "port1.0.1", "NEIGHBOR": "eng-sw-9", "NEIGHBOR_PORT_ID": "ge-0/0/1",
         "MANAGEMENT_IP": ["198.51.100.9"], "CHASSIS_ID": "x"},
        {"LOCAL_INTERFACE": "Ethernet7", "NEIGHBOR_NAME": "", "CHASSIS_ID": "02aa.bb00.00ff",
         "NEIGHBOR_INTERFACE": "02aa.bb00.00ff"},
        {"LOCAL_INTERFACE": "", "NEIGHBOR_NAME": "dropped"},
    ]
    out = normalize_records(recs)
    assert len(out) == 2
    assert (out[0].remote_name, out[0].remote_port, out[0].remote_ip) == ("eng-sw-9", "ge-0/0/1", "198.51.100.9")
    assert out[1].remote_name == "02aa.bb00.00ff"   # chassis-id fallback


def test_resolver_prefers_most_specific_and_site():
    devices = [dev(1, "eng-edge-01.fra1", "fra1"), dev(2, "eng-edge-01.iad1", "iad1"),
               dev(3, "eng-tor-01", "fra1"), dev(4, "eng-tor-01", "iad1")]
    r = NameResolver(devices)
    assert r.resolve("eng-edge-01.fra1.example.net")["id"] == 1
    assert r.resolve("eng-edge-01.iad1")["id"] == 2
    assert r.resolve("eng-edge-01") is None                      # ambiguous, no site
    assert r.resolve("eng-tor-01", from_site="iad1")["id"] == 4  # site breaks the tie
    assert r.resolve("unknown-name", ip="192.0.2.3")["id"] == 3  # mgmt address fallback
    assert r.node_id_for(devices[2]) == "eng-tor-01@fra1"        # duplicate name → @site


# ── builder ─────────────────────────────────────────────────────────────

def test_link_states(storage, lab):
    res = build_topology(lab, CaptureReader(storage), include_unmanaged=True)
    links = by_pair(res)

    l = links[frozenset(("eng-spine-01", "eng-leaf-01"))]
    assert l.state == "confirmed"
    assert {l.a_if, l.b_if} == {"Eth1", "Gi0/1"}

    l = links[frozenset(("eng-spine-01", "eng-edge-01.lab"))]
    assert l.state == "confirmed", "loose match: Junos advertised a description as its port"
    assert "xe-0/0/1" in (l.a_if, l.b_if), "each side's own port name wins"

    assert links[frozenset(("eng-leaf-02", "eng-spine-01"))].state == "one_sided"
    assert links[frozenset(("eng-spine-01", "eng-fw-01"))].state == "unverified"
    assert links[frozenset(("eng-spine-01", "srv-db-07"))].state == "unmanaged"

    assert res.missing_lldp == ["eng-fw-01"]
    assert res.stats["by_state"] == {"confirmed": 2, "one_sided": 1, "unverified": 1, "unmanaged": 1}


def test_confirmed_link_counted_once(storage, lab):
    res = build_topology(lab, CaptureReader(storage))
    keys = [l.key for l in res.links]
    assert len(keys) == len(set(keys))


def test_unmanaged_hidden_by_default(storage, lab):
    res = build_topology(lab, CaptureReader(storage))
    assert all(l.state != "unmanaged" for l in res.links)
    assert res.stats["unmanaged_hidden"] == 1
    assert "srv-db-07" not in res.map["eng-spine-01"]["peers"]


def test_map_is_sc2_shape(storage, lab):
    m = build_topology(lab, CaptureReader(storage)).map
    spine = m["eng-spine-01"]
    assert spine["node_details"]["platform"] == "Arista EOS"
    assert spine["node_details"]["metadata"]["device_dir"] == "lab/eng-spine-01"
    peer = spine["peers"]["eng-leaf-01"]
    assert peer["connections"] == [["Eth1", "Gi0/1"]]
    assert peer["link_states"] == ["confirmed"]
    # both managed ends carry the peer entry (SC2 / map importer expect it)
    assert m["eng-leaf-01"]["peers"]["eng-spine-01"]["connections"] == [["Gi0/1", "Eth1"]]


def test_site_scope_keeps_one_hop(storage):
    devices = [dev(1, "eng-a", "s1"), dev(2, "eng-b", "s2"), dev(3, "eng-c", "s2")]
    put(storage, "s1", "eng-a", [eos("Ethernet1", "eng-b", "Ethernet1")])
    put(storage, "s2", "eng-b", [eos("Ethernet1", "eng-a", "Ethernet1"), eos("Ethernet2", "eng-c", "Ethernet2")])
    put(storage, "s2", "eng-c", [eos("Ethernet2", "eng-b", "Ethernet2")])
    res = build_topology(devices, CaptureReader(storage), site="s1")
    assert set(res.map) == {"eng-a", "eng-b"}
    assert res.map["eng-b"]["node_details"]["metadata"]["out_of_scope"] is True
    assert "eng-c" not in res.map["eng-b"]["peers"]


# ── history + diff ──────────────────────────────────────────────────────

def test_as_of_and_diff_file_backend(storage):
    devices = [dev(1, "eng-a", "lab"), dev(2, "eng-b", "lab"), dev(3, "eng-c", "lab")]
    t0 = datetime(2026, 11, 1, 12, 0, 0)
    t1 = datetime(2026, 11, 10, 12, 0, 0)

    # t0: a—b on Eth1
    put(storage, "lab", "eng-a", [eos("Ethernet1", "eng-b", "Ethernet1")])
    put(storage, "lab", "eng-b", [eos("Ethernet1", "eng-a", "Ethernet1")])
    for n in ("eng-a", "eng-b"):
        set_mtime(storage, "lab", n, "lldp", t0)

    # t1: a—b gone, a—c on Eth2 (history rotation keeps t0 for a and b)
    put(storage, "lab", "eng-a", [eos("Ethernet2", "eng-c", "Ethernet2")])
    put(storage, "lab", "eng-b", [])
    put(storage, "lab", "eng-c", [eos("Ethernet2", "eng-a", "Ethernet2")])
    for n in ("eng-a", "eng-b", "eng-c"):
        set_mtime(storage, "lab", n, "lldp", t1)

    old = build_topology(devices, CaptureReader(storage, t0 + timedelta(days=1)))
    new = build_topology(devices, CaptureReader(storage, None))
    assert {frozenset((l.a, l.b)) for l in old.links} == {frozenset(("eng-a", "eng-b"))}
    assert "eng-c" in old.missing_lldp
    assert {frozenset((l.a, l.b)) for l in new.links} == {frozenset(("eng-a", "eng-c"))}

    d = diff_topologies(old, new)
    changes = {frozenset((x["a"], x["b"])): x["change"] for x in d["links"]}
    assert changes == {frozenset(("eng-a", "eng-b")): "removed",
                       frozenset(("eng-a", "eng-c")): "added"}
    # union map still draws the removed link
    assert "eng-b" in d["map"]["eng-a"]["peers"]
    assert "eng-c" in d["map"]["eng-a"]["peers"]

    # before anything was collected: nothing
    empty = build_topology(devices, CaptureReader(storage, t0 - timedelta(days=1)))
    assert empty.links == [] and len(empty.missing_lldp) == 3


def test_state_change_is_changed(storage):
    devices = [dev(1, "eng-a", "lab"), dev(2, "eng-b", "lab")]
    put(storage, "lab", "eng-a", [eos("Ethernet1", "eng-b", "Ethernet1")])
    put(storage, "lab", "eng-b", [eos("Ethernet1", "eng-a", "Ethernet1")])
    old = build_topology(devices, CaptureReader(storage))
    put(storage, "lab", "eng-b", [])
    new = build_topology(devices, CaptureReader(storage))
    d = diff_topologies(old, new)
    (x,) = d["links"]
    assert (x["change"], x["state_from"], x["state_to"]) == ("changed", "confirmed", "one_sided")


def test_parse_as_of_forms():
    assert parse_as_of("2026-11-17T10:00:00Z") == datetime(2026, 11, 17, 10, 0, 0)
    assert parse_as_of("2026-11-17T01:00:00-09:00") == datetime(2026, 11, 17, 10, 0, 0)
    assert parse_as_of("20261117T100000Z") == datetime(2026, 11, 17, 10, 0, 0)
    assert parse_as_of("latest") is None and parse_as_of("") is None


def test_as_of_git_backend(tmp_path):
    from netlapse.storage.git_backend import GitBackend
    git = GitBackend(tmp_path / "gitdata")
    git.initialize()
    devices = [dev(1, "eng-a", "lab"), dev(2, "eng-b", "lab")]
    put(git, "lab", "eng-a", [eos("Ethernet1", "eng-b", "Ethernet1")])
    put(git, "lab", "eng-b", [eos("Ethernet1", "eng-a", "Ethernet1")])
    first = datetime.utcfromtimestamp(git.repo.head.commit.committed_date)
    time.sleep(1.1)
    put(git, "lab", "eng-b", [])

    then = build_topology(devices, CaptureReader(git, first))
    now = build_topology(devices, CaptureReader(git))
    assert [l.state for l in then.links] == ["confirmed"]
    assert then.version is not None
    assert [l.state for l in now.links] == ["one_sided"]


def test_pair_fallback_when_neither_port_usable(storage):
    # IOS sees the Junos ifIndex; the Junos side sees a MAC for the IOS port.
    devices = [dev(1, "eng-sw-01", "lab", "cisco_ios"), dev(2, "eng-ex-01", "lab", "juniper_junos")]
    put(storage, "lab", "eng-sw-01", [ios("Gi1/0/2", "eng-ex-01", "502")])
    put(storage, "lab", "eng-ex-01", [junos("ge-0/0/0", "eng-sw-01", "7c25.86c9.aaaa", parent="-")])
    (l,) = build_topology(devices, CaptureReader(storage)).links
    assert l.state == "confirmed"
    assert {l.a_if, l.b_if} == {"Gi1/0/2", "ge-0/0/0"}


def test_pair_fallback_does_not_guess_parallel_links(storage):
    devices = [dev(1, "eng-a", "lab"), dev(2, "eng-b", "lab")]
    put(storage, "lab", "eng-a", [eos("Ethernet1", "eng-b", "0"), eos("Ethernet2", "eng-b", "0")])
    put(storage, "lab", "eng-b", [eos("Ethernet1", "eng-a", "0"), eos("Ethernet2", "eng-a", "0")])
    states = {l.state for l in build_topology(devices, CaptureReader(storage)).links}
    assert states == {"one_sided"}


def test_junos_placeholder_parent_is_empty():
    (c,) = normalize_records([junos("ge-0/0/0", "eng-x", "ge-0/0/1", parent="-")])
    assert c.parent_port == ""


def test_weak_cross_site_match_needs_confirmation(storage):
    # oob1.fra1 hears a bare "edge01" with an OOB address reused in iad1.
    # DCIM's edge01 lives in iad1 and doesn't list oob1.fra1: not a link.
    devices = [dev(1, "oob1.fra1", "fra1"), dev(2, "edge01.iad1", "iad1", ip="10.0.0.1"),
               dev(3, "agg1.iad1", "iad1")]
    put(storage, "fra1", "oob1.fra1", [eos("Ethernet5", "edge01", "fxp0", ip="10.0.0.1"),
                                      eos("Ethernet6", "pdu-a", "eth0", ip="10.0.0.1")])
    put(storage, "iad1", "edge01.iad1", [eos("Ethernet1", "agg1.iad1", "Ethernet1")])
    put(storage, "iad1", "agg1.iad1", [eos("Ethernet1", "edge01.iad1", "Ethernet1")])
    res = build_topology(devices, CaptureReader(storage), include_unmanaged=True)
    pairs = {frozenset((l.a, l.b)): l for l in res.links}
    assert frozenset(("oob1.fra1", "edge01.iad1")) not in pairs
    l = pairs[frozenset(("oob1.fra1", "edge01"))]
    assert l.state == "unmanaged" and "edge01.iad1" in l.detail
    assert pairs[frozenset(("oob1.fra1", "pdu-a"))].state == "unmanaged"


def test_weak_cross_site_match_kept_when_confirmed(storage):
    # iad1 <-> iad2 by bare hostnames, both ends agree: a real inter-site link.
    devices = [dev(1, "agg1.iad1", "iad1"), dev(2, "agg1.iad2", "iad2")]
    put(storage, "iad1", "agg1.iad1", [eos("Ethernet9", "agg1", "Ethernet9", ip="192.0.2.2")])
    put(storage, "iad2", "agg1.iad2", [eos("Ethernet9", "agg1.iad1.example.net", "Ethernet9")])
    (l,) = build_topology(devices, CaptureReader(storage)).links
    assert l.state == "confirmed" and {l.a, l.b} == {"agg1.iad1", "agg1.iad2"}


def test_duplicate_mgmt_ip_resolves_in_own_site_only():
    r = NameResolver([dev(1, "sw.a", "a", ip="10.0.0.9"), dev(2, "sw.b", "b", ip="10.0.0.9")])
    assert r.resolve("x", ip="10.0.0.9", from_site="b")["id"] == 2
    assert r.resolve("x", ip="10.0.0.9", from_site="c") is None


def test_multi_site_scope(storage):
    devices = [dev(1, "eng-a", "s1"), dev(2, "eng-b", "s2"), dev(3, "eng-c", "s3"), dev(4, "eng-d", "s4")]
    put(storage, "s1", "eng-a", [eos("Ethernet1", "eng-b", "Ethernet1")])
    put(storage, "s2", "eng-b", [eos("Ethernet1", "eng-a", "Ethernet1"), eos("Ethernet2", "eng-c", "Ethernet2")])
    put(storage, "s3", "eng-c", [eos("Ethernet2", "eng-b", "Ethernet2")])
    put(storage, "s4", "eng-d", [])
    for scope in ("s1,s3", ["s1", "s3"], {"s3", "s1"}):
        res = build_topology(devices, CaptureReader(storage), site=scope)
        assert set(res.map) == {"eng-a", "eng-b", "eng-c"}          # b is one hop from both
        assert res.map["eng-b"]["node_details"]["metadata"]["out_of_scope"] is True
        assert res.map["eng-a"]["node_details"]["metadata"]["out_of_scope"] is False
        assert len(res.links) == 2
    assert set(build_topology(devices, CaptureReader(storage), site=" , ").map) == {"eng-a", "eng-b", "eng-c", "eng-d"}


def test_list_captures_latest_and_as_of(storage):
    put(storage, "lab", "eng-a", [eos("Ethernet1", "eng-b", "Ethernet1")])
    t0 = datetime(2026, 11, 1, 12, 0, 0)
    set_mtime(storage, "lab", "eng-a", "lldp", t0)
    put(storage, "lab", "eng-a", [{"INTERFACE": "Ethernet1"}], capture="interfaces")
    assert CaptureReader(storage).list_captures("lab", "eng-a") == ["interfaces", "lldp"]
    assert CaptureReader(storage, t0 + timedelta(hours=1)).list_captures("lab", "eng-a") == ["lldp"]
    assert CaptureReader(storage).list_captures("lab", "nope") == []


def test_dialog_row_normalizers():
    from netlapse.api.topology import _arp_row, _iface_row
    eos_if = {"INTERFACE": "Ethernet1", "LINK_STATUS": "up", "PROTOCOL_STATUS": "up (connected)",
              "IP_ADDRESS": "192.0.2.1/30", "MAC_ADDRESS": "02aa.0000.0001", "DESCRIPTION": "uplink",
              "MTU": "9214", "BANDWIDTH": "100000000 kbit"}
    r = _iface_row(eos_if)
    assert (r["name"], r["speed_mbps"], r["status"], r["ip_address"]) == ("Ethernet1", "100000", "up/up (connected)", "192.0.2.1/30")
    ios_if = {"INTERFACE": "Gi0/1", "IP_ADDRESS": "192.0.2.5", "PREFIX_LENGTH": "30", "SPEED": "1000Mb/s"}
    assert _iface_row(ios_if)["ip_address"] == "192.0.2.5/30"
    junos_if = {"INTERFACE": "xe-0/0/1.0", "LOCAL": "192.0.2.9/31", "LINK_STATUS": "up", "ADMIN_STATE": "up"}
    assert _iface_row(junos_if)["ip_address"] == "192.0.2.9/31"
    assert _arp_row({"IP_ADDRESS": "192.0.2.7", "MAC_ADDRESS": "02aa.0000.0007", "INTERFACE": "Vlan10", "AGE": "0"})["interface"] == "Vlan10"


def test_dialog_rows_status_and_terse_templates():
    from netlapse.api.topology import _iface_row
    eos_status = {"PORT": "Et49/1", "NAME": "INT::spine1:Et1", "STATUS": "connected", "VLAN_ID": "routed",
                  "DUPLEX": "full", "SPEED": "100G", "TYPE": "100GBASE-SR4"}
    r = _iface_row(eos_status)
    assert (r["name"], r["alias"], r["status"], r["speed_mbps"]) == ("Et49/1", "INT::spine1:Et1", "connected", "100G")
    terse = {"INTERFACE": "et-0/0/1.0", "ADMIN_STATE": "up", "LINK_STATE": "down", "PROTO": "inet",
             "LOCAL": "192.0.2.9/31", "REMOTE": ""}
    r = _iface_row(terse)
    assert (r["status"], r["ip_address"], r["alias"]) == ("up/down", "192.0.2.9/31", "")
