"""Which relays a person has turned automation off for.

Persisted, unlike a hold. A hold is the hub's own timing and ends with the process
that scheduled it; this is a person's instruction — "leave this light alone" — and
the service restarting is not them changing their mind. Nor does it run out: it
lasts until somebody turns automation back on.

Stored as the exception. A row means a relay's automation is off and no row means it
is on, so a relay is automatic from the moment it is configured, with nothing
written for it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pihome_hub.storage import DatabaseUnavailableError, connect, writing


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RelayAutomationStore:
    """The relays whose automation is off, on disk."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._path = path
        self._clock = clock

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """A connection whose statement errors arrive as :class:`StorageError`.

        ``connect`` wraps what goes wrong while *opening*; a statement against a
        database that opened fine — one whose schema was never created, one on a
        filesystem that has since gone read-only — raises ``sqlite3.Error`` as
        itself, and would escape a route as an unhandled 500.
        """
        try:
            with connect(self._path) as connection:
                yield connection
        except sqlite3.Error as exc:
            msg = f"could not read relay automation at {self._path}: {exc}"
            raise DatabaseUnavailableError(msg) from exc

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """The same, for a write."""
        try:
            with writing(self._path) as connection:
                yield connection
        except sqlite3.Error as exc:
            msg = f"could not write relay automation at {self._path}: {exc}"
            raise DatabaseUnavailableError(msg) from exc

    def turned_off(self) -> frozenset[str]:
        """Every relay id a person has turned automation off for."""
        with self._reading() as connection:
            rows = connection.execute("SELECT relay_id FROM automation_off").fetchall()
        return frozenset(str(row["relay_id"]) for row in rows)

    def set_automatic(self, relay_id: str, *, automatic: bool) -> None:
        """Record the choice for one relay. Idempotent in both directions.

        Turning automation off a second time keeps the moment it was first turned
        off: the repeat is the same instruction again, not a new one.
        """
        with self._writing() as connection:
            if automatic:
                connection.execute("DELETE FROM automation_off WHERE relay_id = ?", (relay_id,))
            else:
                connection.execute(
                    "INSERT INTO automation_off (relay_id, turned_off_at) VALUES (?, ?)"
                    " ON CONFLICT(relay_id) DO NOTHING",
                    (relay_id, self._clock().astimezone(UTC).isoformat()),
                )
