"""
Map import: preview must say exactly what an import will do, and an import
must not undo hand edits it wasn't asked to touch.

Lab names, documentation address space.
"""

import json

import pytest

from netlapse.dcim import NetlapseDB
from netlapse.dcim.map_importer import clear_caches, sync_from_map

EOS = "Arista DCS-7050SX3-48YC8 EOS 4.33.1F"


def node(ip, platform=EOS):
    return {"node_details": {"ip": ip, "platform": platform}, "peers": {}}


@pytest.fixture
def db(tmp_path):
    d = NetlapseDB(tmp_path / "netlapse.db")
    d.init_schema(include_defaults=True)
    yield d
    d.close()


@pytest.fixture
def mapfile(tmp_path):
    def write(data):
        p = tmp_path / "map.json"
        p.write_text(json.dumps(data))
        clear_caches()
        return p
    return write


def row(db, name):
    return db.conn.execute(
        "SELECT d.primary_ip4, p.slug, r.slug FROM dcim_device d "
        "LEFT JOIN dcim_platform p ON p.id = d.platform_id "
        "LEFT JOIN dcim_device_role r ON r.id = d.role_id WHERE d.name = ?", (name,)).fetchone()


def by_name(result):
    return {d["name"]: d for d in result.devices}


def seed(db, mapfile):
    sync_from_map(db, mapfile({
        "leaf1.lab1.example.net": node("192.0.2.1"),
        "edge1.lab1.example.net": node("192.0.2.9", "Juniper JUNOS 23.2R1-S2.5"),
    }))


def test_preview_reports_actions_and_field_changes(db, mapfile):
    seed(db, mapfile)
    # hand edit: edge1 promoted to a role of the operator's own
    db.conn.execute("INSERT INTO dcim_device_role (name, slug) VALUES ('Edge', 'edge')")
    db.conn.execute("UPDATE dcim_device SET role_id = (SELECT id FROM dcim_device_role WHERE slug='edge') "
                    "WHERE name = 'edge1.lab1'")
    db.conn.commit()
    before = row(db, "edge1.lab1")

    m = mapfile({
        "leaf1.lab1.example.net": node("192.0.2.1"),                              # unchanged
        "edge1.lab1.example.net": node("198.51.100.9", "Juniper JUNOS 23.2R1"),  # ip moved
        "leaf2.lab1.example.net": node("192.0.2.2"),                              # new
    })
    prev = sync_from_map(db, m, dry_run=True)
    d = by_name(prev)
    assert d["leaf2.lab1"]["action"] == "create"
    assert d["leaf1.lab1"]["action"] == "unchanged" and d["leaf1.lab1"]["changes"] == {}
    assert d["edge1.lab1"]["action"] == "update"
    assert d["edge1.lab1"]["changes"]["ip"] == ["192.0.2.9", "198.51.100.9"]
    assert d["edge1.lab1"]["changes"]["role"][0] == "edge"
    assert (prev.created, prev.updated, prev.skipped) == (1, 1, 1)
    assert row(db, "edge1.lab1") == before, "preview writes nothing"

    # the import does what the preview said
    imp = sync_from_map(db, m)
    assert (imp.created, imp.updated, imp.skipped) == (prev.created, prev.updated, prev.skipped)
    assert row(db, "edge1.lab1")[0] == "198.51.100.9"


def test_new_only_keeps_existing_devices(db, mapfile):
    seed(db, mapfile)
    db.conn.execute("UPDATE dcim_device SET primary_ip4 = '203.0.113.1' WHERE name = 'leaf1.lab1'")
    db.conn.commit()
    m = mapfile({"leaf1.lab1.example.net": node("192.0.2.1"), "leaf3.lab1.example.net": node("192.0.2.3")})
    prev = sync_from_map(db, m, dry_run=True, update_existing=False)
    assert by_name(prev)["leaf1.lab1"]["action"] == "kept"
    assert by_name(prev)["leaf1.lab1"]["changes"]["ip"] == ["203.0.113.1", "192.0.2.1"]
    r = sync_from_map(db, m, update_existing=False)
    assert (r.created, r.updated) == (1, 0)
    assert row(db, "leaf1.lab1")[0] == "203.0.113.1"
    assert row(db, "leaf3.lab1") is not None


def test_unknown_platform_never_clears_a_known_one(db, mapfile):
    seed(db, mapfile)
    m = mapfile({"edge1.lab1.example.net": node("192.0.2.9", "")})
    d = by_name(sync_from_map(db, m, dry_run=True))["edge1.lab1"]
    assert "platform" not in d["changes"]
    sync_from_map(db, m)
    assert row(db, "edge1.lab1")[1] == "juniper_junos"
