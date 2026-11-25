"""
forwarding.yaml loading.

Lookup order: an explicit path, then forwarding.yaml in the definitions
directory (next to captures.yaml), then the packaged default. Missing keys
fall back to DEFAULTS, so a user file only needs the settings it changes.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

from netlapse.forwarding.normalize import parse_prefix

DEFAULTS = {
    "endpoints": {"roles": ["leaf"], "match": None},
    "exclude": {"roles": [], "match": r"^(oob|c\d+[a-z]?\.)|-lab\."},
    "destinations": {
        "source": "unique_connected",
        "within": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
        "min_plen": 16, "max_plen": 29, "interface": None, "host_offset": 10, "list": [],
    },
    "vrf": "default",
    "fib_only": True,
    "captures": {"routes": "routes", "lldp": "lldp", "addresses": ["ip_interfaces", "interfaces"]},
    "stale_minutes": 120,
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _rx(value: Optional[str]):
    return re.compile(value, re.I) if value else None


@dataclass
class ForwardingConfig:
    endpoint_roles: List[str]
    endpoint_re: Optional[re.Pattern]
    exclude_roles: List[str]
    exclude_re: Optional[re.Pattern]
    dest_source: str
    dest_within: List[Tuple[int, int]]
    min_plen: int
    max_plen: int
    dest_if_re: Optional[re.Pattern]
    host_offset: int
    dest_list: List[dict]
    vrf: str
    fib_only: bool
    routes_capture: str
    lldp_capture: str
    address_captures: List[str]
    stale_minutes: int
    source_path: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[dict], source_path: str = "") -> "ForwardingConfig":
        d = _merge(DEFAULTS, (data or {}).get("forwarding", data or {}))
        within = [parse_prefix(p) for p in d["destinations"]["within"]]
        bad = [p for p, w in zip(d["destinations"]["within"], within) if w is None]
        if bad:
            raise ValueError(f"forwarding.destinations.within: not IPv4 prefixes: {bad}")
        if d["destinations"]["source"] not in ("unique_connected", "list"):
            raise ValueError("forwarding.destinations.source must be unique_connected or list")
        return cls(
            endpoint_roles=[r.lower() for r in d["endpoints"].get("roles") or []],
            endpoint_re=_rx(d["endpoints"].get("match")),
            exclude_roles=[r.lower() for r in d["exclude"].get("roles") or []],
            exclude_re=_rx(d["exclude"].get("match")),
            dest_source=d["destinations"]["source"],
            dest_within=within,
            min_plen=int(d["destinations"]["min_plen"]),
            max_plen=int(d["destinations"]["max_plen"]),
            dest_if_re=_rx(d["destinations"].get("interface")),
            host_offset=int(d["destinations"]["host_offset"]),
            dest_list=list(d["destinations"].get("list") or []),
            vrf=str(d["vrf"]),
            fib_only=bool(d["fib_only"]),
            routes_capture=d["captures"]["routes"],
            lldp_capture=d["captures"]["lldp"],
            address_captures=list(d["captures"]["addresses"] or []),
            stale_minutes=int(d["stale_minutes"]),
            source_path=source_path,
            raw=d,
        )

    def is_endpoint(self, device: dict) -> bool:
        role = (device.get("role_slug") or "").lower()
        if self.endpoint_roles and role in self.endpoint_roles:
            return True
        return bool(self.endpoint_re and self.endpoint_re.search(device.get("name") or ""))

    def is_excluded(self, device: dict) -> bool:
        role = (device.get("role_slug") or "").lower()
        if role and role in self.exclude_roles:
            return True
        return bool(self.exclude_re and self.exclude_re.search(device.get("name") or ""))


def _packaged() -> Path:
    return Path(__file__).resolve().parent.parent / "definitions" / "forwarding.yaml"


def load_forwarding_config(path: Optional[str] = None, app_config: Optional[dict] = None) -> ForwardingConfig:
    candidates = []
    if path:
        candidates.append(Path(path).expanduser())
    else:
        try:
            from netlapse.registry.service import resolve_definitions_dir
            candidates.append(resolve_definitions_dir(app_config) / "forwarding.yaml")
        except Exception:
            pass
        candidates.append(_packaged())
    for p in candidates:
        if p.is_file():
            with open(p, encoding="utf-8") as fh:
                return ForwardingConfig.from_dict(yaml.safe_load(fh) or {}, str(p))
        if path:
            raise FileNotFoundError(f"forwarding config not found: {p}")
    return ForwardingConfig.from_dict({}, "built-in defaults")
