"""
Netlapse Vault Bridge — Connects the credential vault to the DCIM and executor.

Provides:
  - Headless unlock via NETLAPSE_VAULT_PASSWORD env var (daemon mode)
  - Device credential resolution: credential_id → SSHCredential → executor tuple
  - Bulk credential assignment for devices
  - CLI helpers for vault management

This is the glue between the SC2 credential vault and Netlapse's
collection pipeline. The vault stores encrypted credentials; the bridge
resolves them into the format the executor expects.

Usage (daemon):
    export NETLAPSE_VAULT_PASSWORD="master_password"
    python -m netlapse

Usage (programmatic):
    from netlapse.vault.bridge import get_vault, resolve_credentials

    vault = get_vault()  # auto-unlocks from env var
    creds = resolve_credentials(vault, device_row)
    # creds is (username, password) tuple for the executor
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, Union, List, Dict, Any

from .vault import CredentialVault, VaultError, VaultNotInitialized
from .encryption import VaultLocked, InvalidPassword
from .models import SSHCredential, CredentialType

logger = logging.getLogger(__name__)

# Environment variable for headless unlock
ENV_VAULT_PASSWORD = "NETLAPSE_VAULT_PASSWORD"

# Module-level singleton
_vault_instance: Optional[CredentialVault] = None


# ═══════════════════════════════════════════════════════════════════════════
# Vault lifecycle
# ═══════════════════════════════════════════════════════════════════════════

def get_vault(
    db_path: Optional[Path] = None,
    auto_unlock: bool = True,
) -> CredentialVault:
    """
    Get or create the vault singleton.

    If NETLAPSE_VAULT_PASSWORD is set and the vault is initialized,
    automatically unlocks for headless/daemon operation.

    Args:
        db_path: Override vault database path.
        auto_unlock: If True, attempt unlock from env var.

    Returns:
        CredentialVault instance (may or may not be unlocked).
    """
    global _vault_instance

    if _vault_instance is None:
        _vault_instance = CredentialVault(db_path)

    if auto_unlock and not _vault_instance.is_unlocked:
        password = os.environ.get(ENV_VAULT_PASSWORD)
        if password and _vault_instance.is_initialized:
            try:
                _vault_instance.unlock(password)
                logger.info("Vault unlocked from %s env var", ENV_VAULT_PASSWORD)
            except InvalidPassword:
                logger.error(
                    "Vault unlock failed: invalid password in %s",
                    ENV_VAULT_PASSWORD,
                )
            except VaultNotInitialized:
                logger.warning("Vault not initialized — run: netlapse vault init")

    return _vault_instance


def reset_vault():
    """Clear the vault singleton. Used in tests."""
    global _vault_instance
    if _vault_instance and _vault_instance.is_unlocked:
        _vault_instance.lock()
    _vault_instance = None


# ═══════════════════════════════════════════════════════════════════════════
# Credential resolution
# ═══════════════════════════════════════════════════════════════════════════

def resolve_credentials(
    vault: CredentialVault,
    device: Dict[str, Any],
) -> Tuple[str, str]:
    """
    Resolve a device's credentials into the tuple the executor expects.

    Lookup order:
      1. device['credential_id'] → vault lookup by ID
      2. Fall back to default SSH credential in vault

    Args:
        vault: Unlocked CredentialVault instance.
        device: Row dict from v_device_detail.

    Returns:
        (username, password) tuple for build_ssh_config().

    Raises:
        VaultLocked: If vault not unlocked.
        ValueError: If no credential found for device.
    """
    if not vault.is_unlocked:
        raise VaultLocked("Vault must be unlocked to resolve credentials")

    device_name = device.get("name", "unknown")
    credential_id = device.get("credential_id")

    ssh_cred: Optional[SSHCredential] = None

    # Try device-specific credential
    if credential_id:
        ssh_cred = vault.get_ssh_credential(credential_id=credential_id)
        if ssh_cred:
            logger.debug(
                "Resolved credential_id=%d for %s",
                credential_id, device_name,
            )

    # Fall back to default
    if not ssh_cred:
        ssh_cred = vault.get_ssh_credential()  # gets default
        if ssh_cred:
            logger.debug(
                "Using default SSH credential for %s (no credential_id)",
                device_name,
            )

    if not ssh_cred:
        raise ValueError(
            f"No SSH credential found for device {device_name} "
            f"(credential_id={credential_id})"
        )

    # Convert to executor tuple
    if ssh_cred.key_content:
        return (ssh_cred.username, ssh_cred.password or "", ssh_cred.key_content)
    return (ssh_cred.username, ssh_cred.password or "")


def resolve_shared_credentials(
    vault: CredentialVault,
    credential_name: Optional[str] = None,
    credential_id: Optional[int] = None,
) -> Tuple[str, str]:
    """
    Resolve a named or default credential for batch collection.

    The executor's collect_devices() takes shared credentials for the
    whole batch. This resolves a single credential by name, ID, or
    default for that use case.

    Args:
        vault: Unlocked CredentialVault instance.
        credential_name: Credential name to look up.
        credential_id: Credential ID to look up.

    Returns:
        (username, password) tuple.

    Raises:
        VaultLocked: If vault not unlocked.
        ValueError: If credential not found.
    """
    if not vault.is_unlocked:
        raise VaultLocked("Vault must be unlocked to resolve credentials")

    ssh_cred = vault.get_ssh_credential(
        name=credential_name,
        credential_id=credential_id,
    )

    if not ssh_cred:
        ref = credential_name or credential_id or "default"
        raise ValueError(f"SSH credential not found: {ref}")

    if ssh_cred.key_content:
        return (ssh_cred.username, ssh_cred.password or "", ssh_cred.key_content)
    return (ssh_cred.username, ssh_cred.password or "")


def resolve_credential_chain(
    vault: CredentialVault,
    primary_name: Optional[str] = None,
    primary_id: Optional[int] = None,
    fallback_names: Optional[List[str]] = None,
) -> List[Tuple[str, Tuple]]:
    """
    Build an ordered credential chain for auth-failure fallback.

    Returns a list of (label, cred_tuple) pairs, primary first, then each
    fallback credential named in config.yaml `credentials.fallback_order`.
    The executor's collect_device() tries them in order, advancing only on
    authentication failure.

    Duplicate credentials are removed by value, so a fallback that resolves
    to the same (username, password[, key]) as the primary is not tried
    twice — that both saves a wasted attempt and avoids contributing an
    extra failed login toward an AAA lockout threshold.

    Args:
        vault: Unlocked CredentialVault instance.
        primary_name: Name of the primary credential (None → vault default).
        primary_id: ID of the primary credential (alternative to name).
        fallback_names: Ordered list of fallback credential names. Names that
                        don't resolve are logged and skipped, not fatal.

    Returns:
        Ordered, de-duplicated list of (label, cred_tuple). Always contains
        at least the primary credential.

    Raises:
        VaultLocked: If vault not unlocked.
        ValueError: If the primary credential cannot be resolved.
    """
    if not vault.is_unlocked:
        raise VaultLocked("Vault must be unlocked to resolve credentials")

    chain: List[Tuple[str, Tuple]] = []
    seen: set = set()

    def _add(label: str, cred: Tuple) -> None:
        if cred in seen:
            logger.debug("Credential %r duplicates an earlier entry — skipping", label)
            return
        seen.add(cred)
        chain.append((label, cred))

    # Primary first (named, by id, or vault default)
    primary = resolve_shared_credentials(
        vault, credential_name=primary_name, credential_id=primary_id
    )
    _add(primary_name or "default", primary)

    # Then ordered fallbacks
    for name in (fallback_names or []):
        try:
            cred = resolve_shared_credentials(vault, credential_name=name)
        except ValueError:
            logger.warning(
                "Fallback credential %r not found in vault — skipping", name
            )
            continue
        _add(name, cred)

    if len(chain) > 1:
        logger.info(
            "Credential chain: %s", " → ".join(label for label, _ in chain)
        )
    return chain


def resolve_collection_chain(
    vault: CredentialVault,
    device: Dict[str, Any],
    fallback_names: Optional[List[str]] = None,
) -> List[Tuple[str, Tuple]]:
    """
    Build the credential chain for COLLECTING from a specific device.

    Unlike resolve_credential_chain — which always starts from the vault
    default — this honors the device's assigned credential_id: that
    credential is tried FIRST, then the global fallbacks. This makes the
    per-device credential override in the UI actually affect collection,
    matching what the Test Authentication button already does via
    resolve_credentials(). Without this, a device assigned a specific
    credential is still collected with the vault default first, so the
    assignment has no effect on real collection.

    If the device has no credential_id, or its assigned credential can't be
    resolved (e.g. a stale id), this falls back to the vault default chain so
    collection still proceeds.

    Args:
        vault: Unlocked CredentialVault instance.
        device: Row dict from v_device_detail (carries credential_id).
        fallback_names: Ordered fallback credential names from config.

    Returns:
        Ordered (label, cred_tuple) chain for collect_device().
    """
    if not vault.is_unlocked:
        raise VaultLocked("Vault must be unlocked to resolve credentials")

    credential_id = device.get("credential_id")
    if credential_id:
        # Resolve the credential's name so the chain label in logs is
        # meaningful — resolve_credential_chain labels an id-only primary as
        # "default", which is misleading when it's actually a device override.
        primary_name = None
        try:
            for info in vault.list_credentials(credential_type=CredentialType.SSH):
                if info.id == credential_id:
                    primary_name = info.name
                    break
        except Exception:
            pass
        try:
            if primary_name:
                return resolve_credential_chain(
                    vault,
                    primary_name=primary_name,
                    fallback_names=fallback_names,
                )
            return resolve_credential_chain(
                vault,
                primary_id=credential_id,
                fallback_names=fallback_names,
            )
        except Exception as e:
            logger.warning(
                "Device %s: assigned credential_id=%s did not resolve (%s); "
                "falling back to the default credential chain",
                device.get("name", "?"), credential_id, e,
            )

    return resolve_credential_chain(vault, fallback_names=fallback_names)


# ═══════════════════════════════════════════════════════════════════════════
# DCIM credential assignment
# ═══════════════════════════════════════════════════════════════════════════

def assign_credential_to_device(
    db,
    vault: CredentialVault,
    device_name: str,
    credential_name: str,
    site_slug: Optional[str] = None,
) -> bool:
    """
    Assign a vault credential to a DCIM device.

    Sets the device's credential_id to the vault credential's ID.
    Validates that the credential exists and is accessible.

    Args:
        db: NetlapseDB instance.
        vault: Unlocked CredentialVault instance.
        device_name: Device name in DCIM.
        credential_name: Credential name in vault.
        site_slug: Optional site to disambiguate device name.

    Returns:
        True if assignment succeeded.

    Raises:
        ValueError: If device or credential not found.
    """
    if not vault.is_unlocked:
        raise VaultLocked("Vault must be unlocked")

    # Verify credential exists
    cred_info_list = vault.list_credentials(credential_type=CredentialType.SSH)
    cred_match = None
    for info in cred_info_list:
        if info.name == credential_name:
            cred_match = info
            break

    if not cred_match:
        raise ValueError(f"Credential '{credential_name}' not found in vault")

    # Find device
    if site_slug:
        device = db.get_device_by_name_and_site(device_name, site_slug)
    else:
        device = db.get_device_by_name(device_name)

    if not device:
        raise ValueError(f"Device '{device_name}' not found in DCIM")

    # Update device
    # credential_id references the vault DB (separate file), so
    # we temporarily disable FK checks for this cross-DB reference.
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    db.conn.execute("PRAGMA foreign_keys = OFF")
    db.conn.execute(
        "UPDATE dcim_device SET credential_id = ?, updated_at = ? WHERE id = ?",
        (cred_match.id, now_iso, device["id"]),
    )
    db.conn.commit()
    db.conn.execute("PRAGMA foreign_keys = ON")

    logger.info(
        "Assigned credential '%s' (id=%d) to device %s",
        credential_name, cred_match.id, device_name,
    )
    return True


def assign_credential_to_all(
    db,
    vault: CredentialVault,
    credential_name: str,
    site_filter: Optional[str] = None,
    role_filter: Optional[str] = None,
) -> int:
    """
    Assign a vault credential to all matching devices.

    Args:
        db: NetlapseDB instance.
        vault: Unlocked CredentialVault instance.
        credential_name: Credential name in vault.
        site_filter: Optional site slug filter.
        role_filter: Optional role slug filter.

    Returns:
        Number of devices updated.
    """
    if not vault.is_unlocked:
        raise VaultLocked("Vault must be unlocked")

    # Verify credential exists
    cred_info_list = vault.list_credentials(credential_type=CredentialType.SSH)
    cred_match = None
    for info in cred_info_list:
        if info.name == credential_name:
            cred_match = info
            break

    if not cred_match:
        raise ValueError(f"Credential '{credential_name}' not found in vault")

    # Build update query
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    query = "UPDATE dcim_device SET credential_id = ?, updated_at = ? WHERE 1=1"
    params: list = [cred_match.id, now_iso]

    if site_filter:
        site_ids = []
        for slug in site_filter.split(","):
            row = db.conn.execute(
                "SELECT id FROM dcim_site WHERE slug = ?", (slug.strip(),)
            ).fetchone()
            if row:
                site_ids.append(row[0])
        if site_ids:
            placeholders = ",".join("?" * len(site_ids))
            query += f" AND site_id IN ({placeholders})"
            params.extend(site_ids)

    if role_filter:
        role_ids = []
        for slug in role_filter.split(","):
            row = db.conn.execute(
                "SELECT id FROM dcim_device_role WHERE slug = ?", (slug.strip(),)
            ).fetchone()
            if row:
                role_ids.append(row[0])
        if role_ids:
            placeholders = ",".join("?" * len(role_ids))
            query += f" AND role_id IN ({placeholders})"
            params.extend(role_ids)

    # credential_id references the vault DB (separate file), so
    # we temporarily disable FK checks for this cross-DB reference.
    db.conn.execute("PRAGMA foreign_keys = OFF")
    cursor = db.conn.execute(query, params)
    db.conn.commit()
    db.conn.execute("PRAGMA foreign_keys = ON")
    count = cursor.rowcount

    logger.info(
        "Assigned credential '%s' (id=%d) to %d devices",
        credential_name, cred_match.id, count,
    )
    return count