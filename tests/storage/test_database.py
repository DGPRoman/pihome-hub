"""Opening the database, and the pragmas the rest of the service assumes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pihome_hub.storage import (
    LATEST_VERSION,
    DatabaseUnavailableError,
    connect,
    prepare_database,
)


class TestConnect:
    def test_creates_the_file_and_the_directory_above_it(self, tmp_path: Path) -> None:
        """systemd makes the state directory, but a development run has no systemd."""
        target = tmp_path / "state" / "hub.db"

        with connect(target):
            pass

        assert target.exists()

    def test_reports_a_directory_it_cannot_create_as_a_storage_error(self, tmp_path: Path) -> None:
        read_only = tmp_path / "locked"
        read_only.mkdir(mode=0o500)

        with (
            pytest.raises(DatabaseUnavailableError, match="could not open the database"),
            connect(read_only / "sub" / "hub.db"),
        ):
            pass

    def test_reports_an_existing_database_it_cannot_write_as_a_storage_error(
        self, tmp_path: Path
    ) -> None:
        """The realistic failure, and the one that used to escape as a traceback.

        ``sqlite3.connect`` opens nothing, so a read-only directory raises on the
        first statement rather than on the call — which is why the pragmas have to
        run inside the same block that translates the error.
        """
        directory = tmp_path / "state"
        directory.mkdir()
        target = directory / "hub.db"
        with connect(target):
            pass
        directory.chmod(0o500)

        try:
            with (
                pytest.raises(DatabaseUnavailableError, match="could not open the database"),
                connect(target),
            ):
                pass
        finally:
            # Restored so pytest can clean the temporary directory up.
            directory.chmod(0o700)

    def test_foreign_keys_are_enforced(self, tmp_path: Path) -> None:
        """Off by default in SQLite, and per connection — so worth pinning."""
        with connect(tmp_path / "hub.db") as connection:
            connection.executescript(
                "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
                "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id));"
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO child (parent_id) VALUES (404)")

    def test_rows_are_addressable_by_column_name(self, tmp_path: Path) -> None:
        with connect(tmp_path / "hub.db") as connection:
            row = connection.execute("SELECT 1 AS answer").fetchone()
            assert row["answer"] == 1

    def test_it_runs_in_write_ahead_log_mode(self, tmp_path: Path) -> None:
        with connect(tmp_path / "hub.db") as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_the_connection_is_closed_afterwards(self, tmp_path: Path) -> None:
        with connect(tmp_path / "hub.db") as connection:
            pass

        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


class TestPrepareDatabase:
    def test_a_fresh_file_ends_up_at_the_current_schema(self, tmp_path: Path) -> None:
        assert prepare_database(tmp_path / "hub.db") == LATEST_VERSION

    def test_the_file_is_readable_only_by_its_owner(self, tmp_path: Path) -> None:
        """It holds password hashes, and a development run has no UMask= to lean on."""
        target = tmp_path / "hub.db"
        prepare_database(target)

        assert target.stat().st_mode & 0o777 == 0o600

    def test_running_it_twice_changes_nothing(self, tmp_path: Path) -> None:
        """Every start calls this, so it has to be safe on an existing database."""
        target = tmp_path / "hub.db"
        prepare_database(target)

        with connect(target) as connection:
            connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at)"
                " VALUES ('roman', 'x', 'admin', '2026-01-01T00:00:00Z')"
            )
            connection.commit()

        prepare_database(target)

        with connect(target) as connection:
            assert connection.execute("SELECT count(*) FROM users").fetchone()[0] == 1
