"""Which devices are declared, and where each one said it can be found.

Persisted, unlike sensor readings, and for the opposite reason. A reading describes
the house *now*, so one recovered from disk would be a lie; an address is a fact
about the network that stays true across a restart of this process. A device
announces when it boots, which may have been in March — losing that on a hub restart
would mean no polling at all until somebody power-cycled the house.

The declaration stays in YAML and only the volatile part is stored here. Which
devices exist is an operator's decision; where they are is the network's.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pihome_hub.devices.errors import DeviceConfigError, UndeclaredDeviceError
from pihome_hub.devices.models import (
    Device,
    DeviceAnnouncement,
    DeviceStatus,
    PollTarget,
)
from pihome_hub.storage import DatabaseUnavailableError, connect, writing

#: Longest failure message kept against a device. It can come from whatever
#: answered on that address, so it is a value to bound rather than to trust.
MAX_ERROR_LENGTH: Final = 500


@dataclass(frozen=True, slots=True)
class _Outcome:
    """One poll's worth of columns, already in the shapes SQLite stores.

    Grouped rather than passed as six keyword arguments, so that the two callers
    below read as the two outcomes they are and the statement writing them has one
    thing to unpack.
    """

    reachable: int
    last_seen_at: str | None
    unreachable_since: str | None
    last_error: str | None
    state: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(moment: datetime) -> str:
    """Store every instant in UTC, so string comparison and ordering mean something."""
    return moment.astimezone(UTC).isoformat()


def _moment(raw: str | None) -> datetime | None:
    return None if raw is None else datetime.fromisoformat(raw)


class DeviceRegistry:
    """The declared devices, and what is known about each one."""

    def __init__(
        self,
        path: Path,
        devices: Iterable[Device],
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._path = path
        self._clock = clock
        # Announcements arrive on the request thread and poll results on the event
        # loop's. SQLite would serialise the writes on its own, but read-then-write
        # in announce() has to be one step, and BEGIN IMMEDIATE only covers the two
        # connections — not two threads sharing this object's view of them.
        self._lock = threading.Lock()
        self._declared: dict[str, Device] = {}

        for device in devices:
            if device.id in self._declared:
                msg = f"duplicate device id {device.id!r}"
                raise DeviceConfigError(msg)
            self._declared[device.id] = device

    @property
    def configured(self) -> Mapping[str, Device]:
        return dict(self._declared)

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """A connection whose statement errors arrive as :class:`StorageError`.

        ``connect`` wraps what goes wrong while *opening*; a statement against a
        database that opened fine — one whose schema was never created, one on a
        filesystem that has since gone read-only — raises ``sqlite3.Error`` as
        itself. Left alone that escapes a route as an unhandled 500, past the
        handler registered for exactly this kind of failure.
        """
        try:
            with connect(self._path) as connection:
                yield connection
        except sqlite3.Error as exc:
            msg = f"could not read the device registry at {self._path}: {exc}"
            raise DatabaseUnavailableError(msg) from exc

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """The same, for a write, and holding this object's lock while it runs."""
        try:
            with self._lock, writing(self._path) as connection:
                yield connection
        except sqlite3.Error as exc:
            msg = f"could not write the device registry at {self._path}: {exc}"
            raise DatabaseUnavailableError(msg) from exc

    def _require(self, device_id: str) -> Device:
        try:
            return self._declared[device_id]
        except KeyError:
            raise UndeclaredDeviceError(device_id) from None

    def forget_undeclared(self) -> int:
        """Drop rows for ids no longer in configuration. Returns how many went.

        Called at startup. A device removed from the YAML file is a device an
        operator has decided this hub does not talk to, and leaving its announced
        key in the database afterwards keeps a credential nobody is using.

        The row goes; the bytes are not scrubbed. SQLite frees a page without
        overwriting it, and turning on ``secure_delete`` to change that would cost
        SD-card writes on every delete in the file to defend against somebody who
        can already read the live keys beside it. A device taken out of service
        should have its key rotated on the device.
        """
        with self._writing() as connection:
            if not self._declared:
                cursor = connection.execute("DELETE FROM devices")
                return cursor.rowcount
            placeholders = ",".join("?" * len(self._declared))
            cursor = connection.execute(
                f"DELETE FROM devices WHERE id NOT IN ({placeholders})",  # noqa: S608
                tuple(self._declared),
            )
            return cursor.rowcount

    def announce(self, device_id: str, announcement: DeviceAnnouncement) -> DeviceStatus:
        """Record where a device is and what it wants to be asked with.

        Everything the poller wrote is cleared. An announcement means the device has
        just booted or just moved, so the last reading, the last error and the run of
        failures all describe a situation that no longer obtains — and if the address
        changed, they describe a different endpoint entirely.
        """
        device = self._require(device_id)
        now = self._clock()

        with self._writing() as connection:
            connection.execute(
                """
                INSERT INTO devices (id, address, api_key, firmware, announced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    address           = excluded.address,
                    api_key           = excluded.api_key,
                    firmware          = excluded.firmware,
                    announced_at      = excluded.announced_at,
                    reachable         = NULL,
                    last_polled_at    = NULL,
                    last_seen_at      = NULL,
                    unreachable_since = NULL,
                    last_error        = NULL,
                    state             = NULL
                """,
                (
                    device_id,
                    announcement.address,
                    announcement.api_key.get_secret_value(),
                    announcement.firmware,
                    _timestamp(now),
                ),
            )
            row = _find(connection, device_id)

        assert row is not None  # noqa: S101 - just written, in the same transaction
        return _to_status(device, row)

    def status(self, device_id: str) -> DeviceStatus:
        device = self._require(device_id)
        with self._reading() as connection:
            row = _find(connection, device_id)
        return _to_status(device, row)

    def statuses(self) -> list[DeviceStatus]:
        """Every declared device in configuration order, announced or not.

        Configuration order rather than insertion order, and including the ones
        never heard from: a device that was declared and has never announced is the
        single most interesting row in the list, and a query over the table alone
        would leave it out.
        """
        with self._reading() as connection:
            rows = {row["id"]: row for row in connection.execute("SELECT * FROM devices")}
        return [
            _to_status(device, rows.get(device_id)) for device_id, device in self._declared.items()
        ]

    def targets(self) -> list[PollTarget]:
        """One target per device that has announced. The others have no address."""
        with self._reading() as connection:
            rows = list(connection.execute("SELECT id, address, api_key FROM devices"))

        targets = []
        for row in rows:
            device = self._declared.get(row["id"])
            if device is None:
                continue
            targets.append(
                PollTarget(
                    id=device.id,
                    address=row["address"],
                    path=device.status_path,
                    api_key=row["api_key"],
                )
            )
        return targets

    def record_success(self, target: PollTarget, state: dict[str, Any], *, at: datetime) -> None:
        """Store what a device answered, and clear whatever it failed with before."""
        self._record(
            target,
            at,
            _Outcome(
                reachable=1,
                last_seen_at=_timestamp(at),
                unreachable_since=None,
                last_error=None,
                state=json.dumps(state),
            ),
        )

    def record_failure(self, target: PollTarget, error: str, *, at: datetime) -> None:
        """Mark a device unreachable, keeping the reading that is now going stale.

        ``state`` is left alone deliberately. It is the last thing the device said
        and it is still that; ``last_seen_at`` says how old it is, and blanking it
        would leave a client with nothing to show rather than with something dated.
        """
        self._record(
            target,
            at,
            _Outcome(
                reachable=0,
                last_seen_at=None,
                unreachable_since=_timestamp(at),
                last_error=error[:MAX_ERROR_LENGTH],
                state=None,
            ),
        )

    def _record(self, target: PollTarget, at: datetime, outcome: _Outcome) -> None:
        """Write one poll result, if the device is still where it was polled.

        The address is in the WHERE clause because a poll is not instantaneous: a
        device can announce a new one while a request to the old one is in flight,
        and the answer that comes back describes an endpoint this hub has already
        been told to stop using. Without the guard, a slow failure against the old
        address lands on top of a fresh announcement and the device reads as broken
        until the next cycle.

        ``unreachable_since`` is only taken when there is not one already, so it
        marks the start of a run of failures rather than the most recent one.
        """
        with self._writing() as connection:
            connection.execute(
                """
                UPDATE devices SET
                    reachable         = ?,
                    last_polled_at    = ?,
                    last_seen_at      = COALESCE(?, last_seen_at),
                    unreachable_since = CASE
                        WHEN ? IS NULL THEN NULL
                        ELSE COALESCE(unreachable_since, ?)
                    END,
                    last_error        = ?,
                    state             = COALESCE(?, state)
                WHERE id = ? AND address = ?
                """,
                (
                    outcome.reachable,
                    _timestamp(at),
                    outcome.last_seen_at,
                    outcome.unreachable_since,
                    outcome.unreachable_since,
                    outcome.last_error,
                    outcome.state,
                    target.id,
                    target.address,
                ),
            )


def _find(connection: sqlite3.Connection, device_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
        "SELECT * FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    return row


def _to_status(device: Device, row: sqlite3.Row | None) -> DeviceStatus:
    """Join what was declared with what was stored, if anything was."""
    if row is None:
        return DeviceStatus(id=device.id, label=device.label, kind=device.kind)

    raw_state: str | None = row["state"]
    return DeviceStatus(
        id=device.id,
        label=device.label,
        kind=device.kind,
        address=row["address"],
        firmware=row["firmware"],
        announced_at=_moment(row["announced_at"]),
        reachable=None if row["reachable"] is None else bool(row["reachable"]),
        last_polled_at=_moment(row["last_polled_at"]),
        last_seen_at=_moment(row["last_seen_at"]),
        unreachable_since=_moment(row["unreachable_since"]),
        last_error=row["last_error"],
        state=None if raw_state is None else json.loads(raw_state),
    )
