"""
Netlapse DCIM Admin API — reference-data CRUD for the four attribute tables.

Path: netlapse/api/dcim_admin.py

Mounts at /api/v1/admin/dcim, alongside the system / vault / template admin
views. Provides create/update/delete for sites, platforms, roles, and
manufacturers — the DCIM attribute tables that, until now, could only be
populated as a side effect of the map importer. This is the manual path that
unblocks hand-creating devices when no discovery map exists.

Routes (KIND in: sites, platforms, roles, manufacturers):

    GET    /admin/dcim/{KIND}            list rows + reference_count
    POST   /admin/dcim/{KIND}            create  (body: name, [slug], ...fields)
    PATCH  /admin/dcim/{KIND}/{id}       update  (body: any settable field)
    DELETE /admin/dcim/{KIND}/{id}       delete  (HARD-REFUSED while referenced)

    POST   /admin/dcim/devices/assign    bulk-reassign a device FK attribute
                                         (body: {device_ids, field, value})

All endpoints are plain `def` so FastAPI runs them in the threadpool over the
shared thread-safe NetlapseDB, matching the rest of the admin surface. The DB
layer (NetlapseDB.*_reference) is the single source of validation; routes are
thin and translate ValueError into 400s.
"""

import logging

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/dcim", tags=["dcim-admin"])

# URL segment (plural) -> NetlapseDB reference kind (singular).
_KIND_BY_PATH = {
    "sites": "site",
    "platforms": "platform",
    "roles": "role",
    "manufacturers": "manufacturer",
}


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _kind(path_kind: str):
    kind = _KIND_BY_PATH.get(path_kind)
    if kind is None:
        return None, _err(404, f"unknown reference type {path_kind!r}")
    return kind, None


# ── Generic list / create ────────────────────────────────────────────────

@router.get("/{path_kind}")
def list_reference(request: Request, path_kind: str):
    kind, err = _kind(path_kind)
    if err:
        return err
    db = request.app.state.db
    return {path_kind: db.list_reference(kind)}


@router.post("/{path_kind}", status_code=201)
def create_reference(request: Request, path_kind: str, body: dict = Body(...)):
    kind, err = _kind(path_kind)
    if err:
        return err
    db = request.app.state.db

    body = dict(body or {})
    name = body.pop("name", None)
    slug = body.pop("slug", None)
    if not name:
        return _err(400, "name is required")
    try:
        created = db.create_reference(kind, name=name, slug=slug, **body)
    except ValueError as e:
        return _err(400, str(e))
    except Exception:
        logger.exception("Failed to create %s %r", kind, name)
        return _err(400, f"could not create {kind}")
    logger.info("Created %s '%s' (id=%s)", kind, created["name"], created["id"])
    return created


# ── Generic update / delete ──────────────────────────────────────────────

@router.patch("/{path_kind}/{ref_id}")
def update_reference(
    request: Request, path_kind: str, ref_id: int, body: dict = Body(...)
):
    kind, err = _kind(path_kind)
    if err:
        return err
    db = request.app.state.db
    try:
        updated = db.update_reference(kind, ref_id, **(body or {}))
    except ValueError as e:
        return _err(400, str(e))
    if updated is None:
        return _err(404, f"{kind} {ref_id} not found")
    return updated


@router.delete("/{path_kind}/{ref_id}")
def delete_reference(request: Request, path_kind: str, ref_id: int):
    kind, err = _kind(path_kind)
    if err:
        return err
    db = request.app.state.db
    try:
        deleted = db.delete_reference(kind, ref_id)
    except ValueError as e:
        # In use — hard refusal. 409 Conflict is the honest status.
        return _err(409, str(e))
    if deleted is None:
        return _err(404, f"{kind} {ref_id} not found")
    return {"deleted": True, "id": ref_id, "name": deleted.get("name")}


# ── Bulk device reassignment ──────────────────────────────────────────────

@router.post("/devices/assign")
def bulk_assign(request: Request, body: dict = Body(...)):
    """
    Reassign one FK attribute across many devices.

    Body: {"device_ids": [1,2,3], "field": "site_id", "value": 7}
    field in {site_id, platform_id, role_id}; value null clears (not site_id).
    """
    db = request.app.state.db
    body = body or {}
    device_ids = body.get("device_ids") or []
    field = body.get("field")
    value = body.get("value")

    if not isinstance(device_ids, list) or not device_ids:
        return _err(400, "device_ids must be a non-empty list")
    if not field:
        return _err(400, "field is required (site_id, platform_id, or role_id)")
    try:
        result = db.bulk_assign_device_attr(device_ids, field, value)
    except ValueError as e:
        return _err(400, str(e))
    logger.info(
        "Bulk-assigned %s=%s to %d device(s)",
        result["field"], result["value"], result["updated"],
    )
    return result