"""Opening the SQLite database, with the pragmas this service depends on.

SQLite rather than a server: the whole dataset is a handful of accounts and their
live sessions, on a Pi Zero that has no business running a database process. It is
also the only storage in the project — readings stay in memory on purpose.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pihome_hub.storage.errors import DatabaseUnavailableError
from pihome_hub.storage.migrations import migrate

#: How long a writer waits for another writer before giving up. Contention here is
#: two requests logging in at once, which resolves in microseconds; the timeout is
#: for the pathological case, so it is short enough not to hold a worker for long.
_BUSY_TIMEOUT_MS = 5_000


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open the database at ``path``, creating its directory if need be.

    Every caller gets its own connection. A single shared one would have to be
    guarded by a lock, because FastAPI runs synchronous work in a threadpool and
    a SQLite connection is not safe to use from several threads at once. Opening
    is cheap, and at this traffic the cost is not worth the shared state.
    """
    connection: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Configuring inside this block, not after it: sqlite3.connect() is lazy and
        # touches nothing, so a directory the service cannot write to raises on the
        # first statement rather than here. Left outside, that arrives as a raw
        # OperationalError and an exit code the unit retries.
        connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000)
        _configure(connection)
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        msg = f"could not open the database at {path}: {exc}"
        raise DatabaseUnavailableError(msg) from exc

    try:
        yield connection
    finally:
        connection.close()


def _configure(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    # WAL so a read never blocks behind a write. The default rollback journal takes
    # an exclusive lock for the duration of a write, which on an SD card is long
    # enough to matter.
    connection.execute("PRAGMA journal_mode = WAL")
    # Off by default in SQLite, and per connection rather than per database, so it
    # has to be set every time or a foreign key is decoration rather than a rule.
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    # NORMAL rather than FULL: a power cut can lose the last transaction, which
    # here means one session that has to be created again. FULL would fsync on
    # every commit, and this runs on a card with finite write cycles.
    connection.execute("PRAGMA synchronous = NORMAL")


def prepare_database(path: Path) -> int:
    """Open the database, bring its schema up to date, and report the version.

    Called once before the server starts, so a directory the service cannot write
    to is reported while there is still a terminal to report it on, rather than at
    the first request that needed an account.
    """
    with connect(path) as connection:
        version = migrate(connection)

    # The unit's UMask=0077 already gets this right, and the state directory it is
    # in is 0700. Set it anyway: the file holds password hashes, both of those are
    # one edit away from being widened, and a development run has neither.
    path.chmod(0o600)
    return version
