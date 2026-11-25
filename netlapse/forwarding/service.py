"""
Model cache for the API.

Building a model reads every in-scope device's route table, so it's done once
per (sites, as_of) and reused until something it was built from changes:

  latest   the signature is the mtime of every in-scope device's route, LLDP
           and address capture, plus forwarding.yaml and the DCIM device set.
           A collection run that rewrites any of them invalidates the entry;
           stat() on a few hundred files is cheap next to reloading
           300k-route tables.
  as_of    the past doesn't change: keyed by the resolved timestamp only.

One build per key at a time (a second request waits for the first and gets
its result). A few entries are kept, least recently used evicted first.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from netlapse.forwarding.config import ForwardingConfig, load_forwarding_config
from netlapse.forwarding.model import ForwardingModel
from netlapse.forwarding.walk import build_matrix, trace

logger = logging.getLogger(__name__)

MAX_ENTRIES = 4


class _Entry:
    __slots__ = ("signature", "model", "data", "built_at", "seconds")

    def __init__(self, signature, model, data, built_at, seconds):
        self.signature = signature
        self.model = model
        self.data = data
        self.built_at = built_at
        self.seconds = seconds


class ForwardingService:
    def __init__(self, storage, devices_fn: Callable[[], List[dict]],
                 app_config: Optional[dict] = None, config_path: Optional[str] = None):
        self.storage = storage
        self.devices_fn = devices_fn
        self.app_config = app_config or {}
        self.config_path = config_path
        self._entries: "OrderedDict[tuple, _Entry]" = OrderedDict()
        self._locks: Dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    # ── config / scope ───────────────────────────────────────────────────

    def config(self) -> ForwardingConfig:
        return load_forwarding_config(self.config_path, self.app_config)

    @staticmethod
    def _norm_sites(sites: Optional[Iterable[str]]) -> Tuple[str, ...]:
        out = set()
        for s in sites or []:
            out.update(x.strip() for x in str(s).split(",") if x.strip())
        return tuple(sorted(out))

    def _signature(self, devices: List[dict], sites: Tuple[str, ...], cfg: ForwardingConfig,
                   as_of: Optional[datetime]) -> tuple:
        dev_sig = tuple(sorted((d["id"], d["name"], d.get("site_slug"), d.get("role_slug"))
                               for d in devices if not sites or d.get("site_slug") in sites))
        cfg_mtime = 0.0
        if cfg.source_path and Path(cfg.source_path).is_file():
            cfg_mtime = Path(cfg.source_path).stat().st_mtime
        if as_of is not None:
            return ("as_of", as_of.isoformat(), dev_sig, cfg.source_path, cfg_mtime)
        base = Path(self.storage.base_path)
        caps = [cfg.routes_capture, cfg.lldp_capture] + list(cfg.address_captures)
        mt = []
        for d in devices:
            if sites and d.get("site_slug") not in sites:
                continue
            ddir = base / (d.get("site_slug") or "") / d["name"]
            for c in caps:
                p = ddir / f"{c}.json"
                try:
                    mt.append(p.stat().st_mtime_ns)
                except OSError:
                    mt.append(0)
        return ("latest", tuple(mt), dev_sig, cfg.source_path, cfg_mtime)

    def _lock_for(self, key) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(key)
            if lk is None:
                lk = self._locks[key] = threading.Lock()
            return lk

    # ── public ───────────────────────────────────────────────────────────

    def matrix(self, sites=None, as_of: Optional[datetime] = None, refresh: bool = False) -> Tuple[dict, dict]:
        """(matrix dict, cache info). Builds or reuses the model for (sites, as_of)."""
        entry, cached = self._get(sites, as_of, refresh)
        info = {"cached": cached, "built_at": entry.built_at, "build_seconds": round(entry.seconds, 2)}
        return entry.data, info

    def model(self, sites=None, as_of: Optional[datetime] = None) -> ForwardingModel:
        return self._get(sites, as_of, False)[0].model

    def trace(self, src: str, dest: str, sites=None, as_of: Optional[datetime] = None) -> dict:
        """Offline trace. Uses the cached model when it can answer exactly; else a one-off model."""
        entry, cached = self._get(sites, as_of, False)
        model = entry.model
        exact = model.node_for_name(dest) in model.destinations or model.covers(dest)
        built = False
        if not exact:
            from netlapse.topology.history import CaptureReader
            t0 = time.monotonic()
            model = ForwardingModel(self.devices_fn(), CaptureReader(self.storage, as_of), self.config(),
                                    sites=self._norm_sites(sites), extra_destinations=[dest])
            built = round(time.monotonic() - t0, 2)
        out = trace(model, src, dest)
        out["model"] = {"cached": cached and not built, "one_off_build_seconds": built or None,
                        "built_at": entry.built_at}
        return out

    def check(self, sites=None, as_of: Optional[datetime] = None) -> dict:
        model = self.model(sites, as_of)
        rows = []
        for node in sorted(model.by_node, key=lambda n: (model.site_of(n), n)):
            st = model.by_node[node]
            d = model.destinations.get(node)
            rows.append({
                "device": node, "id": st.device.get("id"), "site": model.site_of(node),
                "role": st.device.get("role_slug"), "platform": st.device.get("platform_slug"),
                "endpoint": st.endpoint, "routes": st.routes_total,
                "usable": st.route_info.get("usable", 0), "family": st.route_info.get("family"),
                "owned": len(st.owned), "lldp": len(st.lldp),
                "routes_at": st.routes_at.isoformat(timespec="seconds") if st.routes_at else None,
                "destination": ({"subnet": d.subnet, "interface": d.interface, "unique": d.unique}
                                if d else None),
                "problem": st.route_info.get("problem") or "",
            })
        return {"devices": rows, "excluded": model.excluded, "config": model.cfg.source_path}

    def invalidate(self):
        with self._guard:
            self._entries.clear()

    # ── internals ────────────────────────────────────────────────────────

    def _get(self, sites, as_of, refresh) -> Tuple[_Entry, bool]:
        from netlapse.topology.history import CaptureReader

        sites_t = self._norm_sites(sites)
        key = (sites_t, as_of.isoformat() if as_of else None)
        with self._lock_for(key):
            devices = self.devices_fn()
            cfg = self.config()
            sig = self._signature(devices, sites_t, cfg, as_of)
            with self._guard:
                entry = self._entries.get(key)
                if entry is not None and entry.signature == sig and not refresh:
                    self._entries.move_to_end(key)
                    return entry, True
            t0 = time.monotonic()
            model = ForwardingModel(devices, CaptureReader(self.storage, as_of), cfg, sites=sites_t)
            result = build_matrix(model)
            built_at = datetime.now().isoformat(timespec="seconds")
            result.meta["built_at"] = built_at
            data = result.to_dict(model)
            entry = _Entry(sig, model, data, built_at, time.monotonic() - t0)
            logger.info("forwarding model built for sites=%s as_of=%s in %.1fs (%d endpoints)",
                        ",".join(sites_t) or "all", key[1] or "latest", entry.seconds, len(result.endpoints))
            with self._guard:
                self._entries[key] = entry
                self._entries.move_to_end(key)
                while len(self._entries) > MAX_ENTRIES:
                    self._entries.popitem(last=False)
            return entry, False
