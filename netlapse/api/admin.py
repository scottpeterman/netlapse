"""
Netlapse Admin API — Settings, Credential Management, Template Lab.

Provides the API surface for the three admin views:

  /api/v1/admin/system       → config, data directory, service info
  /api/v1/admin/vault/*      → credential CRUD via the vault
  /api/v1/admin/templates/*  → template browsing, editing, and parse testing

All credential operations require an unlocked vault. Template operations
require an initialized parse engine. Both conditions are checked per-request
and return clear error responses when unmet.

Threading model:
  All endpoints are plain `def` (not async def) so FastAPI runs them
  in a threadpool — preventing event-loop blocking from synchronous
  SQLite and encryption operations. The shared NetlapseDB instance
  serializes all access through a _ThreadSafeConnection proxy (RLock),
  so no per-endpoint connection management is needed.
"""

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _vault_or_error(request: Request):
    """Return (vault, None) or (None, JSONResponse)."""
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        return None, JSONResponse(
            status_code=503,
            content={"error": "Vault not initialized"},
        )
    if not vault.is_unlocked:
        return None, JSONResponse(
            status_code=423,
            content={"error": "Vault is locked"},
        )
    return vault, None


def _parser_or_error(request: Request):
    """Return (parser, None) or (None, JSONResponse)."""
    parser = getattr(request.app.state, "parser", None)
    if parser is None:
        return None, JSONResponse(
            status_code=503,
            content={
                "error": "Parse engine not available",
                "detail": "tfsm_templates.db not found — place at "
                          "~/.netlapse/tfsm_templates.db to enable",
            },
        )
    return parser, None


def _template_db_path(request: Request) -> Optional[str]:
    """Resolve template DB path from parser or config."""
    parser = getattr(request.app.state, "parser", None)
    if parser:
        return parser.db_path
    config = getattr(request.app.state, "config", {})
    parser_cfg = config.get("parser", {})
    if parser_cfg.get("db_path"):
        return str(Path(parser_cfg["db_path"]).expanduser())
    default = Path("~/.netlapse/tfsm_templates.db").expanduser()
    return str(default) if default.exists() else None



# ═══════════════════════════════════════════════════════════════════════════
# System / Settings
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/system")
def system_info(request: Request):
    """
    System overview — config, data directory listing, component status.
    Single call for the Settings view to populate everything.
    """
    from netlapse import __version__

    config = getattr(request.app.state, "config", {})
    vault = getattr(request.app.state, "vault", None)
    parser = getattr(request.app.state, "parser", None)
    scheduler = getattr(request.app.state, "scheduler", None)
    storage = getattr(request.app.state, "storage", None)

    # ── Config (redacted — no vault password) ──
    safe_config = {}
    for key, val in config.items():
        if key in ("vault_password",):
            continue
        safe_config[key] = val

    # ── Data directory listing ──
    data_dir = Path("~/.netlapse").expanduser()
    dir_listing = []
    if data_dir.exists():
        for entry in sorted(data_dir.iterdir()):
            try:
                stat = entry.stat()
                dir_listing.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(),
                    "size": stat.st_size if entry.is_file() else None,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime
                    ).isoformat(),
                })
            except OSError:
                # Skip entries we can't stat (broken symlinks, etc.)
                continue

    # ── Data subdirectory (storage tree) ──
    storage_path = None
    storage_sites = []
    if storage:
        storage_path = str(storage.base_path)
        base = Path(storage.base_path)
        if base.exists():
            for site_dir in sorted(base.iterdir()):
                if site_dir.is_dir() and not site_dir.name.startswith("."):
                    device_count = sum(
                        1 for d in site_dir.iterdir()
                        if d.is_dir() and not d.name.startswith(".")
                    )
                    storage_sites.append({
                        "name": site_dir.name,
                        "device_count": device_count,
                    })

    # ── Component status ──
    components = {
        "vault": {
            "available": vault is not None,
            "unlocked": vault.is_unlocked if vault else False,
            "path": str(vault.db_path) if vault else None,
        },
        "parser": {
            "available": parser is not None,
            "template_count": parser.template_count if parser else 0,
            "db_path": parser.db_path if parser else None,
            "min_score": parser.min_score if parser else None,
        },
        "scheduler": {
            "running": scheduler is not None,
            "poll_interval": (
                scheduler._poll_interval if scheduler else None
            ),
            "worker_count": (
                scheduler._max_workers if scheduler else None
            ),
        },
        "storage": {
            "backend": type(storage).__name__ if storage else None,
            "path": storage_path,
            "sites": storage_sites,
        },
    }

    # ── Config file path ──
    config_path = str(Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser())

    return {
        "version": __version__,
        "config_path": config_path,
        "config": safe_config,
        "data_dir": str(data_dir),
        "data_dir_listing": dir_listing,
        "components": components,
    }


@router.get("/config")
def get_config(request: Request):
    """Return the raw config.yaml content as text for the editor."""
    config_path = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()

    if not config_path.exists():
        # Pre-fill the editor with a fully-documented starter config so the
        # operator sees every option (and its default) and "Save" writes a
        # real, commented config.yaml — not an empty stub.
        from netlapse.config_template import default_config_yaml
        return {
            "exists": False,
            "path": str(config_path),
            "content": default_config_yaml(),
        }

    content = config_path.read_text()
    return {"exists": True, "path": str(config_path), "content": content}


class ConfigUpdate(BaseModel):
    content: str


@router.put("/config")
def update_config(request: Request, body: ConfigUpdate):
    """
    Write config.yaml. Validates YAML syntax before writing.
    Does NOT hot-reload — requires a restart.
    """
    config_path = Path(
        os.environ.get("NETLAPSE_CONFIG", "~/.netlapse/config.yaml")
    ).expanduser()

    # Validate YAML
    try:
        parsed = yaml.safe_load(body.content)
        if not isinstance(parsed, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "Config must be a YAML mapping"},
            )
    except yaml.YAMLError as e:
        return JSONResponse(
            status_code=400,
            content={"error": f"Invalid YAML: {e}"},
        )

    # Backup existing
    if config_path.exists():
        backup = config_path.with_suffix(".yaml.bak")
        backup.write_text(config_path.read_text())

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(body.content)

    return {
        "saved": True,
        "path": str(config_path),
        "note": "Restart required for changes to take effect",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Vault / Credentials
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/vault/status")
def vault_status(request: Request):
    """Vault lock state and metadata."""
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        return {"initialized": False, "unlocked": False, "path": None}
    return {
        "initialized": vault.is_initialized,
        "unlocked": vault.is_unlocked,
        "path": str(vault.db_path),
    }


@router.get("/vault/credentials")
def list_vault_credentials(request: Request):
    """
    Full credential list from the vault — not the DCIM mirror.
    Returns CredentialInfo metadata (no secrets).
    """
    vault, err = _vault_or_error(request)
    if err:
        return err

    creds = vault.list_credentials()
    return [
        {
            "id": c.id,
            "name": c.name,
            "type": c.type_display,
            "credential_type": c.credential_type.value,
            "username": c.display_username,
            "auth": c.auth_summary,
            "is_default": c.is_default,
            "priority": c.priority,
            "description": c.description,
            "created_at": (
                c.created_at.isoformat() if c.created_at else None
            ),
            "last_used_at": (
                c.last_used_at.isoformat() if c.last_used_at else None
            ),
            "last_test_success": c.last_test_success,
            "last_test_at": (
                c.last_test_at.isoformat() if c.last_test_at else None
            ),
        }
        for c in creds
    ]


class AddSSHCredential(BaseModel):
    name: str
    username: str
    password: Optional[str] = None
    key_content: Optional[str] = None
    key_passphrase: Optional[str] = None
    port: int = 22
    description: Optional[str] = None
    is_default: bool = False


@router.post("/vault/credentials")
def add_credential(request: Request, body: AddSSHCredential):
    """Add an SSH credential to the vault."""
    vault, err = _vault_or_error(request)
    if err:
        return err

    if not body.password and not body.key_content:
        return JSONResponse(
            status_code=400,
            content={"error": "Provide either password or key_content"},
        )

    try:
        logger.info("Adding credential '%s'", body.name)
        cred_id = vault.add_ssh_credential(
            name=body.name,
            username=body.username,
            password=body.password,
            key_content=body.key_content,
            key_passphrase=body.key_passphrase,
            port=body.port,
            description=body.description,
            is_default=body.is_default,
        )
        logger.info("Credential '%s' added (id=%d)", body.name, cred_id)
        return {"id": cred_id, "name": body.name}

    except Exception as e:
        logger.exception("Failed to add credential '%s'", body.name)
        return JSONResponse(
            status_code=400,
            content={"error": str(e)},
        )


@router.delete("/vault/credentials/{credential_id}")
def remove_credential(request: Request, credential_id: int):
    """Remove a credential from the vault."""
    vault, err = _vault_or_error(request)
    if err:
        return err

    try:
        logger.info("Removing credential id=%d", credential_id)
        removed = vault.remove_credential(credential_id=credential_id)
        if removed:
            return {"deleted": True, "id": credential_id}
        else:
            return JSONResponse(
                status_code=404,
                content={"error": "Credential not found"},
            )
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e)},
        )


@router.put("/vault/credentials/{credential_id}/default")
def set_default_credential(request: Request, credential_id: int):
    """Set a credential as the default."""
    vault, err = _vault_or_error(request)
    if err:
        return err

    try:
        vault.set_default(credential_id)
        return {"id": credential_id, "is_default": True}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e)},
        )


class BulkAssign(BaseModel):
    credential_name: str
    site_filter: Optional[str] = None
    role_filter: Optional[str] = None


@router.post("/vault/credentials/assign")
def bulk_assign_credential(request: Request, body: BulkAssign):
    """Assign a credential to devices (bulk)."""
    vault, err = _vault_or_error(request)
    if err:
        return err

    from netlapse.vault.bridge import assign_credential_to_all

    # Shared db instance is thread-safe via _ThreadSafeConnection proxy
    db = request.app.state.db
    try:
        count = assign_credential_to_all(
            db=db,
            vault=vault,
            credential_name=body.credential_name,
            site_filter=body.site_filter,
            role_filter=body.role_filter,
        )
        return {"assigned": count, "credential": body.credential_name}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"error": str(e)},
        )


# ═══════════════════════════════════════════════════════════════════════════
# Template Lab
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/templates")
def list_templates(
    request: Request,
    platform: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    """
    List templates in the database.
    Filter by platform prefix or search query.
    """
    db_path = _template_db_path(request)
    if not db_path:
        return JSONResponse(
            status_code=503,
            content={"error": "Template database not found"},
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = "SELECT id, cli_command FROM templates WHERE 1=1"
        params = []

        if platform:
            query += " AND cli_command LIKE ?"
            params.append(f"{platform}%")

        if q:
            query += " AND cli_command LIKE ?"
            params.append(f"%{q}%")

        # Count
        count_q = query.replace(
            "SELECT id, cli_command", "SELECT COUNT(*)"
        )
        total = conn.execute(count_q, params).fetchone()[0]

        query += " ORDER BY cli_command LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()

        return {
            "total": total,
            "templates": [
                {"id": r["id"], "cli_command": r["cli_command"]}
                for r in rows
            ],
        }
    finally:
        conn.close()


@router.get("/templates/platforms")
def list_template_platforms(request: Request):
    """List distinct platform prefixes from the template database."""
    db_path = _template_db_path(request)
    if not db_path:
        return JSONResponse(
            status_code=503,
            content={"error": "Template database not found"},
        )

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT cli_command FROM templates "
            "ORDER BY cli_command"
        ).fetchall()

        # Extract platform prefixes — templates are named like
        # "cisco_ios_show_ip_arp" or "arista_eos_show_interfaces"
        platforms = {}
        for (name,) in rows:
            parts = name.split("_")
            # Try vendor_os (2 parts) then vendor (1 part)
            if len(parts) >= 2:
                prefix = f"{parts[0]}_{parts[1]}"
                platforms[prefix] = platforms.get(prefix, 0) + 1

        return [
            {"platform": p, "count": c}
            for p, c in sorted(platforms.items())
        ]
    finally:
        conn.close()


@router.get("/templates/{template_id}")
def get_template(request: Request, template_id: int):
    """Get a single template with its TextFSM content."""
    db_path = _template_db_path(request)
    if not db_path:
        return JSONResponse(
            status_code=503,
            content={"error": "Template database not found"},
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()
        if not row:
            return JSONResponse(
                status_code=404,
                content={"error": "Template not found"},
            )
        return dict(row)
    finally:
        conn.close()


class TemplateUpdate(BaseModel):
    cli_command: Optional[str] = None
    textfsm_content: Optional[str] = None


@router.put("/templates/{template_id}")
def update_template(
    request: Request, template_id: int, body: TemplateUpdate,
):
    """Update a template's name or content."""
    db_path = _template_db_path(request)
    if not db_path:
        return JSONResponse(
            status_code=503,
            content={"error": "Template database not found"},
        )

    conn = sqlite3.connect(db_path)
    try:
        fields = []
        params = []
        if body.cli_command is not None:
            fields.append("cli_command = ?")
            params.append(body.cli_command)
        if body.textfsm_content is not None:
            fields.append("textfsm_content = ?")
            params.append(body.textfsm_content)

        if not fields:
            return JSONResponse(
                status_code=400,
                content={"error": "No fields to update"},
            )

        params.append(template_id)
        conn.execute(
            f"UPDATE templates SET {', '.join(fields)} WHERE id = ?",
            params,
        )
        conn.commit()
        return {"updated": True, "id": template_id}
    finally:
        conn.close()


class TemplateCreate(BaseModel):
    cli_command: str
    textfsm_content: str


@router.post("/templates")
def create_template(request: Request, body: TemplateCreate):
    """Add a new template to the database."""
    db_path = _template_db_path(request)
    if not db_path:
        return JSONResponse(
            status_code=503,
            content={"error": "Template database not found"},
        )

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO templates (cli_command, textfsm_content) "
            "VALUES (?, ?)",
            (body.cli_command, body.textfsm_content),
        )
        conn.commit()
        return {"id": cursor.lastrowid, "cli_command": body.cli_command}
    except sqlite3.IntegrityError as e:
        return JSONResponse(
            status_code=409,
            content={"error": f"Duplicate template: {e}"},
        )
    finally:
        conn.close()


@router.delete("/templates/{template_id}")
def delete_template(request: Request, template_id: int):
    """Delete a template from the database."""
    db_path = _template_db_path(request)
    if not db_path:
        return JSONResponse(
            status_code=503,
            content={"error": "Template database not found"},
        )

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM templates WHERE id = ?", (template_id,)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return JSONResponse(
                status_code=404,
                content={"error": "Template not found"},
            )
        return {"deleted": True, "id": template_id}
    finally:
        conn.close()


class ParseTestRequest(BaseModel):
    raw_output: str
    platform: str
    command: Optional[str] = ""


@router.post("/templates/test")
def test_parse(request: Request, body: ParseTestRequest):
    """
    Template Lab: test raw CLI output against the parse engine.

    Returns the best-matching template, parsed records, score,
    and the list of all candidate templates that were tried.
    """
    parser, err = _parser_or_error(request)
    if err:
        return err

    # Primary parse: platform + command filter
    result = parser.parse(
        raw_output=body.raw_output,
        platform_profile=body.platform,
        command=body.command,
    )

    # If primary fails, try vendor fallback
    fallback_result = None
    if not result.success:
        fallback_result = parser.parse_vendor_fallback(
            raw_output=body.raw_output,
            platform_profile=body.platform,
        )

    # Get the list of candidate templates that were in scope
    candidates = _get_candidate_templates(request, body.platform, body.command)

    primary = {
        "success": result.success,
        "template": result.template,
        "score": result.score,
        "record_count": result.record_count,
        "records": result.records,
        "error": result.error,
    }

    response = {
        "primary": primary,
        "candidates": candidates,
    }

    if fallback_result and fallback_result.success:
        response["fallback"] = {
            "success": True,
            "template": fallback_result.template,
            "score": fallback_result.score,
            "record_count": fallback_result.record_count,
            "records": fallback_result.records,
        }

    return response


def _get_candidate_templates(
    request: Request, platform: str, command: str,
) -> list:
    """
    List templates that match the filter criteria.
    Shows what find_best_template would have tried.
    """
    db_path = _template_db_path(request)
    if not db_path:
        return []

    # Replicate the filter logic from tfsm_fire.get_filtered_templates
    filter_string = platform
    if command:
        cmd_clean = command.replace(" ", "_").replace("|", "")
        filter_string = f"{platform}_{cmd_clean}"

    filter_terms = filter_string.replace("-", "_").split("_")

    conn = sqlite3.connect(db_path)
    try:
        query = "SELECT id, cli_command FROM templates WHERE 1=1"
        params = []
        for term in filter_terms:
            if term and len(term) > 2:
                query += " AND cli_command LIKE ?"
                params.append(f"%{term}%")
        query += " ORDER BY cli_command"

        rows = conn.execute(query, params).fetchall()
        return [{"id": r[0], "cli_command": r[1]} for r in rows]
    finally:
        conn.close()

class AdhocTemplateTest(BaseModel):
    raw_output: str
    textfsm_content: str
    command: Optional[str] = ""
    clean: bool = True


@router.post("/templates/test-template")
def test_template_adhoc(request: Request, body: AdhocTemplateTest):
    """
    Template Lab author-test: run a single ad-hoc TextFSM template against
    raw output WITHOUT saving it to the database.

    Returns compile/runtime errors (with rule line + input line when the
    template raises a State Error), plus parsed records and the full score
    breakdown. This is the loop for fixing a bad template: edit, test, see
    exactly why it fails or what it scores, repeat — no DB write, no
    pollution of the selection sweep.
    """
    parser, err = _parser_or_error(request)
    if err:
        return err

    try:
        return parser.test_template(
            textfsm_content=body.textfsm_content,
            raw_output=body.raw_output,
            command=body.command or "",
            clean=body.clean,
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": str(e)},
        )