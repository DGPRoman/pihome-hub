"""RelayAutomationStore: which relays a person turned automation off for, on disk."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pihome_hub.automation import RelayAutomationStore
from pihome_hub.storage import StorageError, connect, prepare_database


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "hub.db"
    prepare_database(path)
    return path


class TestRelayAutomationStore:
    def test_a_fresh_database_has_every_relay_automatic(self, database: Path) -> None:
        assert RelayAutomationStore(database).turned_off() == frozenset()

    def test_turning_automation_off_is_recorded(self, database: Path) -> None:
        store = RelayAutomationStore(database)

        store.set_automatic("porch-light", automatic=False)

        assert store.turned_off() == frozenset({"porch-light"})

    def test_turning_it_back_on_removes_the_record(self, database: Path) -> None:
        store = RelayAutomationStore(database)
        store.set_automatic("porch-light", automatic=False)

        store.set_automatic("porch-light", automatic=True)

        assert store.turned_off() == frozenset()

    def test_turning_on_a_relay_that_was_never_off_is_harmless(self, database: Path) -> None:
        store = RelayAutomationStore(database)

        store.set_automatic("porch-light", automatic=True)

        assert store.turned_off() == frozenset()

    def test_the_choice_outlives_the_object_that_made_it(self, database: Path) -> None:
        RelayAutomationStore(database).set_automatic("gate-light", automatic=False)

        assert RelayAutomationStore(database).turned_off() == frozenset({"gate-light"})

    def test_turning_it_off_twice_keeps_the_first_time(self, database: Path) -> None:
        """The repeat is the same instruction again, not a new one."""
        first = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)
        later = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)
        RelayAutomationStore(database, clock=lambda: first).set_automatic(
            "porch-light", automatic=False
        )
        RelayAutomationStore(database, clock=lambda: later).set_automatic(
            "porch-light", automatic=False
        )

        with connect(database) as connection:
            rows = connection.execute(
                "SELECT relay_id, turned_off_at FROM automation_off"
            ).fetchall()
        assert [tuple(row) for row in rows] == [("porch-light", first.isoformat())]

    def test_an_unprepared_database_is_a_storage_error(self, tmp_path: Path) -> None:
        """Not a raw sqlite3 error, which would escape a route as an unhandled 500."""
        store = RelayAutomationStore(tmp_path / "hub.db")

        with pytest.raises(StorageError):
            store.turned_off()
        with pytest.raises(StorageError):
            store.set_automatic("porch-light", automatic=False)
