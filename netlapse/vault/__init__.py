"""
Netlapse Credential Vault.

Encrypted credential storage for SSH device authentication,
ported from Secure Cartography's credential vault with
Netlapse-specific integration (DCIM bridge, headless unlock).

Quick Start:
    # Initialize and add credentials
    from netlapse.vault import CredentialVault

    vault = CredentialVault()
    vault.initialize("master_password")
    vault.add_ssh_credential(
        name="lab", username="admin", password="secret", is_default=True
    )

    # Headless unlock (daemon mode)
    export NETLAPSE_VAULT_PASSWORD="master_password"

    # Resolve for executor
    from netlapse.vault.bridge import get_vault, resolve_shared_credentials

    vault = get_vault()  # auto-unlocks from env
    creds = resolve_shared_credentials(vault, credential_name="lab")
"""

__version__ = "0.1.0"

# Core vault
from .vault import (
    CredentialVault,
    VaultError,
    VaultNotInitialized,
    CredentialNotFound,
    DuplicateCredential,
)

# Encryption
from .encryption import (
    VaultEncryption,
    VaultLocked,
    InvalidPassword,
    DecryptionFailed,
)

# Models
from .models import (
    CredentialType,
    SSHCredential,
    SNMPv2cCredential,
    SNMPv3Credential,
    CredentialInfo,
    CredentialSet,
)

# Bridge
from .bridge import (
    get_vault,
    reset_vault,
    resolve_credentials,
    resolve_shared_credentials,
    assign_credential_to_device,
    assign_credential_to_all,
    ENV_VAULT_PASSWORD,
)

__all__ = [
    # Core
    "CredentialVault",
    "VaultError",
    "VaultNotInitialized",
    "VaultLocked",
    "InvalidPassword",
    "DecryptionFailed",
    "CredentialNotFound",
    "DuplicateCredential",

    # Models
    "CredentialType",
    "SSHCredential",
    "SNMPv2cCredential",
    "SNMPv3Credential",
    "CredentialInfo",
    "CredentialSet",

    # Bridge
    "get_vault",
    "reset_vault",
    "resolve_credentials",
    "resolve_shared_credentials",
    "assign_credential_to_device",
    "assign_credential_to_all",
    "ENV_VAULT_PASSWORD",
]