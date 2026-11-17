"""
The resolver — the single source of "what commands run on this platform".

This one function is the whole point of going data-driven: before it existed,
the scheduler resolved per-platform commands inline (honoring command_map) and
the CLI collector did not (it sent the default commands to every platform), so
`netlapse collect --job` and the daemon disagreed on Junos boxes. Both paths
now call resolve_commands(), so the divergence cannot recur — there is exactly
one place the decision is made.

It operates on the *projected* job columns (default `commands` + `command_map`),
not on the catalog directly, so it works identically whether a job was synced
from files or created through the API.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Union

logger = logging.getLogger(__name__)


def resolve_commands(
    default_commands: List[str],
    command_map: Optional[Union[str, Dict[str, List[str]]]],
    platform_slug: Optional[str],
) -> List[str]:
    """
    Resolve the command list for one device's platform.

    Args:
        default_commands: the job's fallback command list (already a list).
        command_map: per-platform overrides, as either the raw JSON string
            stored in jobs.command_map or an already-decoded dict. None/empty
            means "no overrides — everyone gets the default".
        platform_slug: the device's platform slug (v_device_detail).

    Returns:
        The override list for this platform if one exists, else the default.
    """
    if not command_map:
        return default_commands

    if isinstance(command_map, str):
        try:
            command_map = json.loads(command_map)
        except (ValueError, TypeError):
            logger.warning(
                "command_map is not valid JSON; falling back to default commands"
            )
            return default_commands

    if not isinstance(command_map, dict):
        return default_commands

    return command_map.get(platform_slug or "", default_commands)
