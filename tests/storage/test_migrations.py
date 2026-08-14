"""Schema versioning: applying, resuming, and refusing to go backwards."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from pihome_hub.storage import (
    LATEST_VERSION,
    SchemaTooNewError,
    connect,
    current_version,
    migrate,
)
from pihome_hub.storage.migrations import MIGRATIONS


class TestMigrate:
    def test_an_empty_database_starts_at_version_zero(self, tmp_path: Path) -> None:
        with connect(tmp_path / "hub.db") as connection:
            assert current_version(connection) == 0

    def test_it_applies_every_migration_and_records_the_version(self, tmp_path: Path) -> None:
        with connect(tmp_path / "hub.db") as connection:
            assert migrate(connection) == LATEST_VERSION
            assert current_version(connection) == LATEST_VERSION

    def test_a_second_run_applies_nothing(self, tmp_path: Path) -> None:
        with connect(tmp_path / "hub.db") as connection:
            migrate(connection)
            # A migration re-applied would raise "table users already exists".
            assert migrate(connection) == LATEST_VERSION

    def test_it_resumes_from_a_partially_migrated_database(self, tmp_path: Path) -> None:
        """The case that matters once there are two: version 1 must not run again."""
        with connect(tmp_path / "hub.db") as connection:
            connection.executescript(MIGRATIONS[0])
            connection.execute("PRAGMA user_version = 1")

            assert migrate(connection) == LATEST_VERSION

    def test_it_refuses_a_database_from_a_newer_build(self, tmp_path: Path) -> None:
        """Downgrading would read columns whose meaning this build does not know."""
        with connect(tmp_path / "hub.db") as connection:
            migrate(connection)
            connection.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")

            with pytest.raises(SchemaTooNewError, match="newer pihome-hub"):
                migrate(connection)


class TestTheUsersTable:
    @pytest.fixture
    def connection(self, tmp_path: Path) -> Iterator[sqlite3.Connection]:
        with connect(tmp_path / "hub.db") as connection:
            migrate(connection)
            yield connection

    def _insert(self, connection: sqlite3.Connection, username: str, role: str) -> None:
        connection.execute(
            "INSERT INTO users (username, password_hash, role, created_at)"
            " VALUES (?, 'hash', ?, '2026-01-01T00:00:00Z')",
            (username, role),
        )

    def test_it_accepts_each_role(self, connection: sqlite3.Connection) -> None:
        for index, role in enumerate(("admin", "operator", "viewer")):
            self._insert(connection, f"user{index}", role)

    def test_it_refuses_a_role_that_is_not_one_of_the_three(
        self, connection: sqlite3.Connection
    ) -> None:
        """The check constraint is the backstop for a role the API layer let through."""
        with pytest.raises(sqlite3.IntegrityError):
            self._insert(connection, "roman", "superuser")

    def test_usernames_differing_only_in_case_are_the_same_account(
        self, connection: sqlite3.Connection
    ) -> None:
        """'Roman' and 'roman' as two accounts is a phishing affordance."""
        self._insert(connection, "roman", "admin")

        with pytest.raises(sqlite3.IntegrityError):
            self._insert(connection, "Roman", "viewer")

    def test_disabled_is_a_flag_not_an_arbitrary_number(
        self, connection: sqlite3.Connection
    ) -> None:
        self._insert(connection, "roman", "admin")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE users SET disabled = 2 WHERE username = 'roman'")


class TestTheMigrationListIsAppendOnly:
    def test_the_recorded_version_matches_the_number_of_migrations(self) -> None:
        """A migration deleted rather than appended to would silently renumber the rest."""
        assert len(MIGRATIONS) == LATEST_VERSION
