"""
Oxidized-Compatible REST API Router

Drop-in replacement for Oxidized's REST API surface.
LibreNMS and other tools that integrate with Oxidized can point
at Netlapse without changing their configuration.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Form, Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oxidized-compat"])


def _resolve_node(db, node_full: str):

    if "/" in node_full:
        group, _, node = node_full.partition("/")
        device = db.get_device_by_name_and_site(node, group)
        if device:
            return device
        # Fall through: maybe the name legitimately contains a slash, or the
        # group didn't match — try the whole string as a bare name.
    return db.get_device_by_name(node_full) or db.get_device_by_ip(node_full)


# ---------------------------------------------------------------------------
# GET /nodes — List all nodes
# ---------------------------------------------------------------------------
@router.get("/nodes")
def list_nodes(request: Request, group: Optional[str] = Query(None)):
    """List all nodes, optionally filtered by group (= site slug)."""
    db = request.app.state.db
    return db.list_nodes_oxidized(group=group)


# ---------------------------------------------------------------------------
# GET /node/show/:node — Single node info (full_name, group, model, …)
# LibreNMS fetches this first (showconfig.inc.php:207) to learn a node's
# full_name and group before requesting versions/config. Returns the same
# per-node shape as /nodes.
# ---------------------------------------------------------------------------
@router.get("/node/show/{node}")
def show_node(request: Request, node: str):
    db = request.app.state.db
    device = _resolve_node(db, node)
    if not device:
        raise HTTPException(status_code=404, detail=f"node '{node}' not found")
    status = "never"
    if device.get("last_collection_status") == "success":
        status = "success"
    elif device.get("last_collection_status"):
        status = "failure"
    last_time = device.get("last_collected_at") or ""
    return {
        "name": device["name"],
        "full_name": device["name"],
        "ip": device.get("primary_ip4") or "",
        "model": device.get("platform_profile") or "unknown",
        "group": device.get("site_slug") or "",
        "status": status,
        "time": last_time,
        # LibreNMS reads $node_info['last']['status'] and ['last']['end']
        # (showconfig.inc.php:271,276), so `last` must be an object.
        "last": {"status": status, "end": last_time, "start": last_time},
    }


# ---------------------------------------------------------------------------
# GET /node/fetch/:node — Fetch current config
# ---------------------------------------------------------------------------
@router.get("/node/fetch/{node}")
def fetch_node_config(request: Request, node: str):
    """Fetch the most recent config for a node (plain text)."""
    db = request.app.state.db
    storage = request.app.state.storage

    device = db.get_device_by_name(node)
    if not device:
        # Try by IP (some integrations use IP instead of hostname)
        device = db.get_device_by_ip(node)
    if not device:
        raise HTTPException(status_code=404, detail=f"node '{node}' not found")

    raw = storage.fetch_latest_raw(
        device["name"], device["site_slug"], "config"
    )
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail=f"no config stored for '{node}'",
        )
    return PlainTextResponse(raw)


@router.get("/node/fetch/{group}/{node}")
def fetch_node_config_with_group(
    request: Request, group: str, node: str
):
    """Fetch config for a node scoped to a group (site slug)."""
    db = request.app.state.db
    storage = request.app.state.storage

    device = db.get_device_by_name_and_site(node, group)
    if not device:
        raise HTTPException(
            status_code=404,
            detail=f"node '{node}' in group '{group}' not found",
        )

    raw = storage.fetch_latest_raw(device["name"], group, "config")
    if raw is None:
        raise HTTPException(
            status_code=404,
            detail=f"no config stored for '{node}'",
        )
    return PlainTextResponse(raw)


# ---------------------------------------------------------------------------
# GET/PUT /node/next/:node — Trigger immediate collection
# ---------------------------------------------------------------------------
@router.get("/node/next/{node}")
def node_next_get(request: Request, node: str):
    return _trigger_node_collection(request, node)


@router.put("/node/next/{node}")
def node_next_put(request: Request, node: str):
    return _trigger_node_collection(request, node)


def _trigger_node_collection(request: Request, node: str):
    """Signal immediate collection for a node."""
    db = request.app.state.db
    device = db.get_device_by_name(node) or db.get_device_by_ip(node)
    if not device:
        raise HTTPException(status_code=404, detail=f"node '{node}' not found")

    scheduler = request.app.state.scheduler
    if scheduler is not None:
        try:
            scheduler.enqueue_device(device["id"], "config", trigger="oxidized")
        except (LookupError, ValueError) as e:
            logger.warning("Could not trigger collection for %s: %s", device["name"], e)
            raise HTTPException(status_code=422, detail=str(e))

    logger.info("Triggered immediate collection for %s", device["name"])
    return {"node": device["name"], "status": "queued"}


# ---------------------------------------------------------------------------
# GET /reload — Reload node list from source
# ---------------------------------------------------------------------------
@router.get("/reload")
def reload_nodes(request: Request):
    """Reload the node list. Returns current count."""
    db = request.app.state.db
    devices = db.list_devices(collection_enabled=True)
    return {"status": "reloaded", "nodes": len(devices)}


# ---------------------------------------------------------------------------
# GET /node/version — List versions for a node
# ---------------------------------------------------------------------------
@router.get("/node/version")
def node_versions(request: Request, node_full: str = Query(...)):
    """List stored versions (git commits or timestamps) for a node."""
    db = request.app.state.db
    storage = request.app.state.storage

    device = _resolve_node(db, node_full)
    if not device:
        raise HTTPException(status_code=404, detail=f"node '{node_full}' not found")

    try:
        versions = storage.list_versions(device["name"], device["site_slug"])
        return [
            {
                "oid": v.version_id,
                "date": v.collected_at.isoformat() if v.collected_at else "",
                # LibreNMS' showconfig reads $version['author']['name'], so
                # author must be an object, not a bare string.
                "author": {"name": v.author, "email": ""},
                "message": v.message,
            }
            for v in versions
        ]
    except NotImplementedError:
        return []


# ---------------------------------------------------------------------------
# GET/POST /node/version/view — View a config version, or diff two versions
#
# LibreNMS (showconfig.inc.php) calls this as GET with query params and always
# format=text, assigning the response body straight to a string for display:
#   view a version:  ?node=<n>&oid=<oid>&date=<d>&num=<k>&format=text
#   diff two:        ?node=<n>&oid=<newer>&...&oid2=<older>&format=text
# The presence of `oid2` is what signals a diff request (not a `diff` param),
# and the diff is returned as a plain-text unified diff. We also accept POST
# form and the `diff`/`node_full` aliases for programmatic/other-client use.
# ---------------------------------------------------------------------------
@router.get("/node/version/view")
def node_version_view_get(
    request: Request,
    node: str = Query(None),
    node_full: str = Query(None),
    oid: str = Query(None),
    oid2: str = Query(None),
    diff: str = Query(None),
    num: int = Query(1),
):
    return _do_version_view(request, node or node_full, oid, oid2, diff, num)


@router.post("/node/version/view")
def node_version_view_post(
    request: Request,
    node: str = Form(None),
    node_full: str = Form(None),
    oid: str = Form(None),
    oid2: str = Form(None),
    diff: str = Form(None),
    num: int = Form(1),
):
    return _do_version_view(request, node or node_full, oid, oid2, diff, num)


# ---------------------------------------------------------------------------
# GET /node/version/diffs — Diff two versions (LibreNMS's actual diff route)
#
# LibreNMS "Show diff" calls a SEPARATE endpoint, not view-with-oid2:
#   GET /node/version/diffs?node=<n>&group=<g>&oid=<newer>&oid2=<older>&format=text
# It renders the response body as a unified diff. We delegate to the same diff
# logic as the view handler.
# ---------------------------------------------------------------------------
@router.get("/node/version/diffs")
def node_version_diffs(
    request: Request,
    node: str = Query(None),
    node_full: str = Query(None),
    group: str = Query(None),
    oid: str = Query(None),
    oid2: str = Query(None),
    num: int = Query(1),
):
    # Force the diff path by passing a non-None diff flag; oid2 is the older
    # version, oid the newer.
    return _do_version_view(request, node or node_full, oid, oid2, "1", num)


def _do_version_view(request: Request, node_id: str, oid, oid2, diff, num):
    """View a specific config version, or diff two versions, as plain text."""
    if not node_id:
        raise HTTPException(status_code=422, detail="node (or node_full) required")

    db = request.app.state.db
    storage = request.app.state.storage

    device = _resolve_node(db, node_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"node '{node_id}' not found")

    name, site = device["name"], device["site_slug"]

    # Diff request: LibreNMS signals it by sending oid2 (the older version);
    # oid is the newer one. Some other clients send a `diff` flag instead, in
    # which case we fall back to "this version vs its predecessor".
    want_diff = oid2 is not None or diff is not None
    if want_diff:
        try:
            to_version = oid or None        # newer
            from_version = oid2 or None     # older
            if to_version and from_version is None:
                # No explicit older version — pick the one just before `oid`.
                versions = storage.list_versions(name, site)
                ids = [v.version_id for v in versions]  # newest-first
                if to_version in ids:
                    idx = ids.index(to_version)
                    if idx + 1 < len(ids):
                        from_version = ids[idx + 1]
            result = storage.diff(
                name, site, "config",
                from_version=from_version, to_version=to_version,
            )
            # LibreNMS renders the body as a diff (sets $language='diff'); it
            # wants the raw unified-diff text, not a JSON envelope.
            return PlainTextResponse(result.raw_diff if result else "")
        except NotImplementedError:
            return PlainTextResponse("")

    # No diff → return the requested version's config as plain text.
    # LibreNMS calls this with format=text and assigns the body straight to
    # $text for the <pre> highlighter (showconfig.inc.php:259,360). It does
    # NOT json_decode this path — the earlier line-301 crash was the version
    # list's author field, not this response.
    if oid:
        try:
            snapshot = storage.fetch_version(name, site, oid, "config")
            if snapshot:
                return PlainTextResponse(snapshot.raw_text)
        except NotImplementedError:
            pass

    return PlainTextResponse("")


# ---------------------------------------------------------------------------
# POST /nodes/conf_search — Search across all configs
# ---------------------------------------------------------------------------
@router.post("/nodes/conf_search")
def conf_search(
    request: Request, search_in_conf_textbox: str = Form(...)
):
    """Search for a regex pattern across all stored configurations."""
    storage = request.app.state.storage
    results = storage.search_configs(search_in_conf_textbox)

    # Flatten to Oxidized-compatible format (list of match strings)
    compat = []
    for r in results:
        matches = r.get("matches", [])
        flat = [
            m["text"] if isinstance(m, dict) else m
            for m in matches
        ]
        compat.append({
            "device": r["device"],
            "site": r.get("site", ""),
            "matches": flat,
        })
    return compat