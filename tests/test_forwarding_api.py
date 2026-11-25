"""
Forwarding API: the router on a bare FastAPI app over the same two-site lab
as test_forwarding.py (no auth gate, no daemon).
"""

import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from netlapse.api.forwarding import router
from netlapse.storage.file_backend import FileBackend
from tests.test_forwarding import DEVICES, seed


class _DB:
    def list_devices(self, status=None):
        return [d for d in DEVICES if status is None or d["status"] == status]


@pytest.fixture
def client(tmp_path):
    storage = FileBackend(tmp_path / "data")
    storage.initialize()
    seed(storage)
    app = FastAPI()
    app.include_router(router)
    app.state.storage = storage
    app.state.db = _DB()
    app.state.config = {"definitions_dir": str(tmp_path / "defs")}
    c = TestClient(app)
    c.storage = storage
    return c


def test_matrix_and_cache(client):
    r = client.get("/api/v1/forwarding/matrix")
    assert r.status_code == 200
    d = r.json()
    assert d["meta"]["tors"] == 5 and d["meta"]["status"] == {"ok": 16, "fail": 4}
    assert d["cache"]["cached"] is False
    assert d["devices"]["tor-a.iad1"]["id"] == 1

    assert client.get("/api/v1/forwarding/matrix").json()["cache"]["cached"] is True
    assert client.get("/api/v1/forwarding/matrix", params={"refresh": 1}).json()["cache"]["cached"] is False

    # a capture rewritten by a collection run invalidates the entry
    p = client.storage.base_path / "iad1" / "agg1.iad1" / "routes.json"
    later = time.time() + 5
    os.utime(p, (later, later))
    assert client.get("/api/v1/forwarding/matrix").json()["cache"]["cached"] is False


def test_matrix_site_scope(client):
    d = client.get("/api/v1/forwarding/matrix", params={"site": "iad2"}).json()
    assert [t["name"] for t in d["tors"]] == ["tor-x.iad2", "tor-z.iad2"]


def test_trace_cached_and_one_off(client):
    t = client.get("/api/v1/forwarding/trace", params={"src": "tor-x.iad2", "dest": "tor-a.iad1"}).json()
    assert t["status"] == "ok" and t["model"]["one_off_build_seconds"] is None
    assert t["layers"][0][0]["device"] == "tor-x.iad2" and t["layers"][0][0]["id"] == 9

    # any address inside an endpoint subnet comes from the cached model
    t = client.get("/api/v1/forwarding/trace", params={"src": "tor-a.iad1", "dest": "10.1.2.77"}).json()
    assert t["status"] == "ok" and t["model"]["cached"] is True

    # an outside address needs its covering routes: one-off model
    t = client.get("/api/v1/forwarding/trace", params={"src": "tor-a.iad1", "dest": "198.51.100.7"}).json()
    assert t["terms"] == ["exit"] and t["model"]["one_off_build_seconds"] is not None


def test_errors(client):
    assert client.get("/api/v1/forwarding/trace", params={"src": "nope", "dest": "10.1.1.1"}).status_code == 404
    assert client.get("/api/v1/forwarding/trace", params={"src": "tor-a.iad1", "dest": "zzz"}).status_code == 400
    assert client.get("/api/v1/forwarding/matrix", params={"as_of": "garbage"}).status_code == 400


def test_check_report_csv(client):
    c = client.get("/api/v1/forwarding/check").json()
    assert c["excluded"] == ["oob1.iad1"]
    row = next(r for r in c["devices"] if r["device"] == "tor-x.iad2")
    assert row["destination"]["subnet"] == "10.2.1.0/24" and row["family"] == "junos"

    r = client.get("/api/v1/forwarding/report")
    assert r.status_code == 200 and r.text.startswith("<!DOCTYPE html>")

    r = client.get("/api/v1/forwarding/pairs.csv")
    lines = r.text.strip().splitlines()
    assert r.headers["content-type"].startswith("text/csv") and len(lines) == 21
