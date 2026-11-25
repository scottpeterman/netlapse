"""
Forwarding API.

  GET /api/v1/forwarding/matrix   ?site=a,b&as_of=&refresh=1
  GET /api/v1/forwarding/trace    ?src=&dest=&site=&as_of=
  GET /api/v1/forwarding/check    ?site=&as_of=
  GET /api/v1/forwarding/report   ?site=&as_of=      standalone HTML report
  GET /api/v1/forwarding/pairs.csv ?site=&as_of=

All read stored captures only. The model behind them is built once per
(sites, as_of) and cached until a capture it was built from changes (see
netlapse/forwarding/service.py), so the first request after a collection run
pays the load and the rest don't. Sync endpoints: FastAPI runs them in its
threadpool, so a model build doesn't block the event loop.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from netlapse.forwarding.service import ForwardingService
from netlapse.topology.history import parse_as_of

router = APIRouter(prefix="/api/v1/forwarding", tags=["forwarding"])


def _service(request: Request) -> ForwardingService:
    svc = getattr(request.app.state, "forwarding", None)
    if svc is None:
        db = request.app.state.db
        svc = ForwardingService(
            storage=request.app.state.storage,
            devices_fn=lambda: db.list_devices(status="active"),
            app_config=getattr(request.app.state, "config", None) or {},
        )
        request.app.state.forwarding = svc
    return svc


def _as_of(value: Optional[str]) -> Optional[datetime]:
    try:
        return parse_as_of(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"bad timestamp: {value!r}")


def _sites(site: Optional[str]):
    return [s for s in (site or "").split(",") if s.strip()]


def _wrap_config_errors(fn):
    try:
        return fn()
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/matrix")
def matrix(request: Request, site: Optional[str] = None, as_of: Optional[str] = None,
           refresh: bool = Query(False, description="rebuild even if nothing changed")):
    svc = _service(request)
    data, info = _wrap_config_errors(lambda: svc.matrix(_sites(site), _as_of(as_of), refresh))
    return {**data, "cache": info}


@router.get("/trace")
def trace(request: Request, src: str, dest: str, site: Optional[str] = None, as_of: Optional[str] = None):
    svc = _service(request)
    try:
        return svc.trace(src, dest, _sites(site), _as_of(as_of))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/check")
def check(request: Request, site: Optional[str] = None, as_of: Optional[str] = None):
    svc = _service(request)
    return _wrap_config_errors(lambda: svc.check(_sites(site), _as_of(as_of)))


@router.get("/report", response_class=HTMLResponse)
def report(request: Request, site: Optional[str] = None, as_of: Optional[str] = None):
    from netlapse.forwarding.report import render_html
    svc = _service(request)
    data, _ = _wrap_config_errors(lambda: svc.matrix(_sites(site), _as_of(as_of)))
    return HTMLResponse(render_html(data))


@router.get("/pairs.csv")
def pairs_csv(request: Request, site: Optional[str] = None, as_of: Optional[str] = None):
    svc = _service(request)
    data, _ = _wrap_config_errors(lambda: svc.matrix(_sites(site), _as_of(as_of)))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["src", "dst", "status", "terminals", "min_hops", "max_hops", "paths", "fanout",
                "symmetric", "only_forward", "only_reverse", "default_dep_devices", "exits"])
    for key in sorted(data["pairs"]):
        a, b = key.split("|", 1)
        p = data["pairs"][key]
        w.writerow([a, b, p["status"], " ".join(p["terms"]), p["minh"], p["maxh"], p["np"], p["fanout"],
                    p["sym"], " ".join(p.get("only_fwd", [])), " ".join(p.get("only_rev", [])),
                    " ".join(p["dep_devs"]), " | ".join(p["exits"])])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="forwarding_pairs.csv"'})
