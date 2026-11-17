"""
Netlapse SSH — Paramiko-based SSH client with NetEmulate support.

Path: netlapse/ssh/__init__.py

Core SSH infrastructure for Netlapse device collection. The client
connects to network devices (or NetEmulate mock devices), detects
prompts, disables pagination, and executes CLI commands.

The emulation module is a standalone shim that any Netlapse component
can import independently — scheduler, executor, CLI tools — without
pulling in SSH internals.

Usage:
    from netlapse.ssh import SSHClient, SSHClientConfig

    config = SSHClientConfig(
        host="192.168.1.1",
        username="admin",
        password="secret",
    )

    with SSHClient(config) as client:
        client.find_prompt()
        output = client.execute_command("show version")

Emulation:
    from netlapse.ssh.emulation import enable_emulation
    enable_emulation("/path/to/ip_lookup.json")
    # All subsequent SSHClient connections redirect to mock devices

Ported from: sc2/scng/discovery/ssh/
"""

from .client import (
    SSHClient,
    SSHClientConfig,
    LegacySSHSupport,
    filter_ansi_sequences,
    PAGINATION_DISABLE_SHOTGUN,
)
from .emulation import (
    enable_emulation,
    disable_emulation,
    is_enabled as emulation_is_enabled,
    lookup as emulation_lookup,
)
from .executor import (
    collect_device,
    collect_devices,
    build_ssh_config,
    DeviceResult,
    CommandResult,
    ErrorCategory,
)

__all__ = [
    # Client
    'SSHClient',
    'SSHClientConfig',
    'LegacySSHSupport',
    'filter_ansi_sequences',
    'PAGINATION_DISABLE_SHOTGUN',
    # Executor
    'collect_device',
    'collect_devices',
    'build_ssh_config',
    'DeviceResult',
    'CommandResult',
    'ErrorCategory',
    # Emulation (convenience re-exports)
    'enable_emulation',
    'disable_emulation',
    'emulation_is_enabled',
    'emulation_lookup',
]
