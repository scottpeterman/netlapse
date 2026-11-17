"""
Storage layer for Netlapse.

Provides pluggable backends for storing raw CLI output and
tfsm-fire parsed JSON. Two backends ship with Netlapse:

  - FileBackend: directory tree, last-N rotation, no dependencies
  - GitBackend:  git-versioned, commit trailers, structured diffs

Usage:
    from netlapse.storage import create_backend

    backend = create_backend(config)
    backend.initialize()
    backend.store(snapshot)
"""

from netlapse.storage.backend import (
    StorageBackend,
    Snapshot,
    StoredVersion,
    DiffResult,
    FleetChange,
)
from netlapse.storage.file_backend import FileBackend
from netlapse.storage.git_backend import GitBackend


def create_backend(config: dict) -> StorageBackend:
    """
    Factory: create a storage backend from config.

    Config format:
        storage:
          backend: file|git
          path: ~/.netlapse/data
          max_versions: 50           # file backend only
          git:
            commit_trailers: true    # git backend only
    """
    storage_config = config.get("storage", {})
    backend_type = storage_config.get("backend", "file")
    base_path = storage_config.get("path", "~/.netlapse/data")

    if backend_type == "git":
        return GitBackend(base_path=base_path)
    elif backend_type == "file":
        max_versions = storage_config.get("max_versions", 50)
        return FileBackend(base_path=base_path, max_versions=max_versions)
    else:
        raise ValueError(
            f"Unknown storage backend: '{backend_type}'. "
            f"Use 'file' or 'git'."
        )


__all__ = [
    "StorageBackend",
    "Snapshot",
    "StoredVersion",
    "DiffResult",
    "FleetChange",
    "FileBackend",
    "GitBackend",
    "create_backend",
]