"""Schema versioning, on SQLite's own ``user_version``.

No migration framework. The alternative is a dependency, a table, and a directory
of numbered files to express what a tuple already says — for a schema that will
hold accounts and their sessions and is not going to sprawl.

Rules for anything added here: append, never edit. A migration that has run on a
Pi is history, and rewriting it means two installations disagree about what
version 3 was.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from pihome_hub.storage.errors import SchemaTooNewError

#: Applied in order. The index of a statement is its version, so the first is 1.
MIGRATIONS: Final[tuple[str, ...]] = (
    # 1 — accounts.
    """
    CREATE TABLE users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT    NOT NULL,
        password_hash TEXT    NOT NULL,
        role          TEXT    NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
        disabled      INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
        created_at    TEXT    NOT NULL
    );
    -- Case-insensitively unique: 'Roman' and 'roman' being two accounts is a
    -- phishing affordance, not a feature.
    CREATE UNIQUE INDEX users_username_unique ON users (username COLLATE NOCASE);
    """,
)

#: The schema this build understands.
LATEST_VERSION: Final = len(MIGRATIONS)


def current_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(connection: sqlite3.Connection) -> int:
    """Bring the database up to :data:`LATEST_VERSION`. Returns the version reached.

    Raises :class:`SchemaTooNewError` if the file was written by a later build.
    Refusing is the only safe answer: this one would read columns whose meaning it
    does not know, and writing to them would corrupt what the newer build stored.
    """
    version = current_version(connection)

    if version > LATEST_VERSION:
        msg = (
            f"the database is at schema version {version}, but this build only knows "
            f"version {LATEST_VERSION}. It was written by a newer pihome-hub; "
            "upgrade rather than downgrade."
        )
        raise SchemaTooNewError(msg)

    for index in range(version, LATEST_VERSION):
        # Each migration is one transaction, so a failure halfway leaves the
        # database at the last version that fully applied rather than in between.
        with connection:
            connection.executescript(MIGRATIONS[index])
            # No parameter binding: PRAGMA does not accept one. The value is a loop
            # index over a tuple defined in this file, not anything from outside.
            connection.execute(f"PRAGMA user_version = {index + 1}")

    return LATEST_VERSION
