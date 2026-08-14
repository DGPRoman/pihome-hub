"""Exceptions raised by the storage layer."""

from __future__ import annotations


class StorageError(Exception):
    """Base class for every error this package raises."""


class DatabaseUnavailableError(StorageError):
    """Raised when the database cannot be opened or created.

    Almost always a permissions or path problem rather than a corrupt file: the
    service runs under a sandbox with exactly one writable directory.
    """


class SchemaTooNewError(StorageError):
    """Raised when the database was written by a later version of this service.

    Downgrading is not supported and guessing is worse: an older build that met a
    newer schema would read columns that mean something else now.
    """
