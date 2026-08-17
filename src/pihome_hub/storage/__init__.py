"""Persistent state: the only thing this service keeps on disk."""

from __future__ import annotations

from pihome_hub.storage.database import connect, prepare_database, writing
from pihome_hub.storage.errors import (
    DatabaseUnavailableError,
    SchemaTooNewError,
    StorageError,
)
from pihome_hub.storage.migrations import LATEST_VERSION, current_version, migrate

__all__ = [
    "LATEST_VERSION",
    "DatabaseUnavailableError",
    "SchemaTooNewError",
    "StorageError",
    "connect",
    "current_version",
    "migrate",
    "prepare_database",
    "writing",
]
