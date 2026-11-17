"""
Minimal netlapse REST client for the MCP server.

stdlib only (urllib) so the MCP package has no HTTP dependency of its own and
doesn't care which httpx generation the installed `mcp` SDK pulls in.
Calls are blocking; the server runs them in a worker thread.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class NetlapseError(Exception):
    """Operator-facing failure; the message is returned to the model as-is."""


class NetlapseClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: Optional[float] = None,
        verify_tls: Optional[bool] = None,
    ):
        self.base = (base_url or os.environ.get("NETLAPSE_URL") or "http://127.0.0.1:8888").rstrip("/")
        self.token = token or os.environ.get("NETLAPSE_TOKEN") or ""
        self.timeout = float(timeout or os.environ.get("NETLAPSE_TIMEOUT") or 60)
        if verify_tls is None:
            verify_tls = os.environ.get("NETLAPSE_VERIFY_TLS", "1").lower() not in ("0", "false", "no")
        self._ctx = None
        if self.base.startswith("https") and not verify_tls:
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE
        self._devices: Optional[List[dict]] = None
        self._devices_at = 0.0

    # ── transport ─────────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Any] = None,
        body: Optional[Any] = None,
    ) -> Any:
        if not self.token:
            raise NetlapseError("NETLAPSE_TOKEN is not set — create one with "
                                "`python -m netlapse token --name mcp` and add it to the MCP server env")
        url = f"{self.base}/api/v1{path}"
        if params:
            if isinstance(params, dict):
                params = [(k, v) for k, v in params.items() if v is not None and v != ""]
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                payload = json.loads(e.read().decode("utf-8", "replace"))
                detail = payload.get("detail") or payload.get("error") or ""
                if isinstance(detail, list):        # FastAPI validation errors
                    detail = "; ".join(str(d.get("msg", d)) for d in detail)
            except Exception:
                pass
            if e.code == 401:
                raise NetlapseError("netlapse rejected the token (401) — check NETLAPSE_TOKEN") from None
            if e.code == 403 and not detail:
                detail = "not permitted for this token"
            raise NetlapseError(f"netlapse {e.code}: {detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise NetlapseError(f"cannot reach netlapse at {self.base}: {e.reason}") from None
        except TimeoutError:
            raise NetlapseError(f"netlapse at {self.base} timed out after {self.timeout:.0f}s") from None
        if not raw:
            return None
        return json.loads(raw)

    def get(self, path: str, params: Optional[Any] = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Optional[Any] = None, params: Optional[Any] = None) -> Any:
        return self.request("POST", path, params=params, body=body)

    # ── device resolution ─────────────────────────────────────────────

    def devices(self, refresh: bool = False) -> List[dict]:
        if refresh or self._devices is None or time.time() - self._devices_at > 60:
            self._devices = self.get("/devices") or []
            self._devices_at = time.time()
        return self._devices

    def resolve(self, ref: str) -> dict:
        """Device by id, exact name, name prefix, or unique substring."""
        ref = str(ref or "").strip()
        if not ref:
            raise NetlapseError("device is required")
        devs = self.devices()
        if ref.isdigit():
            for d in devs:
                if str(d["id"]) == ref:
                    return d
        low = ref.lower()
        for tier in (
            lambda n: n == low,
            lambda n: n.startswith(low + ".") or n.split(".")[0] == low,
            lambda n: n.startswith(low),
            lambda n: low in n,
        ):
            hits = [d for d in devs if tier((d.get("name") or "").lower())]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                names = ", ".join(sorted(d["name"] for d in hits)[:15])
                more = f" (+{len(hits) - 15} more)" if len(hits) > 15 else ""
                raise NetlapseError(f"'{ref}' matches {len(hits)} devices: {names}{more} — be more specific")
        by_ip = [d for d in devs if d.get("primary_ip4") and d["primary_ip4"].split("/")[0] == ref]
        if len(by_ip) == 1:
            return by_ip[0]
        if not refresh_attempted(self):
            self.devices(refresh=True)
            return self.resolve(ref)
        raise NetlapseError(f"no device matches '{ref}' — use find_devices to search")


def refresh_attempted(client: NetlapseClient) -> bool:
    """One forced refresh per lookup miss, so a just-imported device is found
    without hammering /devices on every typo."""
    fresh = time.time() - client._devices_at < 1.0
    return fresh


def trim(text: Any, n: int = 300) -> Any:
    if isinstance(text, str) and len(text) > n:
        return text[:n] + f"… (+{len(text) - n} chars)"
    return text


_WS = re.compile(r"\s+")


def one_line(s: str, n: int = 240) -> str:
    return trim(_WS.sub(" ", s or "").strip(), n)
