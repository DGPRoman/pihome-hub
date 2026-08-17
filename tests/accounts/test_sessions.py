"""Sessions: what a token buys, for how long, and what quietly takes it away."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pihome_hub.accounts import Role, Session, SessionStore, User, UserStore
from pihome_hub.accounts import sessions as sessions_module
from pihome_hub.storage import connect, prepare_database

PASSWORD = "correct-horse-battery"
LIFETIME = timedelta(days=30)
START = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


class Clock:
    """A hand-wound clock, so expiry can be tested without waiting a month."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "hub.db"
    prepare_database(path)
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def users(database: Path) -> UserStore:
    return UserStore(database)


@pytest.fixture
def sessions(database: Path, clock: Clock) -> SessionStore:
    return SessionStore(database, lifetime=LIFETIME, clock=clock)


@pytest.fixture
def roman(users: UserStore) -> User:
    return users.create("roman", PASSWORD, Role.ADMIN)


class TestCreate:
    def test_the_token_resolves_to_the_account_it_was_made_for(
        self, sessions: SessionStore, roman: User
    ) -> None:
        token, session = sessions.create(roman)

        resolved = sessions.resolve(token)
        assert resolved is not None
        assert resolved.user == roman
        assert resolved == session

    def test_every_token_is_different(self, sessions: SessionStore, roman: User) -> None:
        first, _ = sessions.create(roman)
        second, _ = sessions.create(roman)

        assert first != second
        assert sessions.resolve(first) is not None
        assert sessions.resolve(second) is not None

    def test_a_token_is_long_enough_not_to_be_searched(
        self, sessions: SessionStore, roman: User
    ) -> None:
        token, _ = sessions.create(roman)

        # 32 bytes, urlsafe-base64 without padding.
        assert len(token) >= 40

    def test_it_expires_a_lifetime_after_it_was_opened(
        self, sessions: SessionStore, roman: User
    ) -> None:
        _, session = sessions.create(roman)

        assert session.created_at == START
        assert session.expires_at == START + LIFETIME

    def test_the_token_itself_is_not_stored(
        self, sessions: SessionStore, roman: User, database: Path
    ) -> None:
        """A stolen database must hold verifiers, not credentials."""
        token, _ = sessions.create(roman)

        with connect(database) as connection:
            stored = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]

        assert token not in stored
        assert stored == hashlib.sha256(token.encode("utf-8")).hexdigest()

    def test_a_session_carries_no_token(self) -> None:
        """The value is returned once and is not recoverable from anything kept."""
        assert "token" not in Session.model_fields


class TestResolve:
    def test_a_token_that_was_never_issued_resolves_to_nothing(
        self, sessions: SessionStore, roman: User
    ) -> None:
        sessions.create(roman)

        assert sessions.resolve("not-a-token-anybody-ever-had") is None

    def test_an_expired_session_resolves_to_nothing(
        self, sessions: SessionStore, clock: Clock, roman: User
    ) -> None:
        token, _ = sessions.create(roman)

        clock.advance(LIFETIME - timedelta(seconds=1))
        assert sessions.resolve(token) is not None, "it should last the whole lifetime"

        clock.advance(timedelta(seconds=1))
        assert sessions.resolve(token) is None

    def test_it_reads_the_account_again_rather_than_trusting_the_login(
        self, sessions: SessionStore, users: UserStore, roman: User
    ) -> None:
        """Which is what makes disabling somebody take effect on their next request."""
        users.create("spare", PASSWORD, Role.ADMIN)
        token, _ = sessions.create(roman)

        users.set_role("roman", Role.VIEWER)

        resolved = sessions.resolve(token)
        assert resolved is not None
        assert resolved.user.role is Role.VIEWER

    def test_disabling_an_account_stops_its_sessions_at_once(
        self, sessions: SessionStore, users: UserStore, roman: User
    ) -> None:
        users.create("spare", PASSWORD, Role.ADMIN)
        token, _ = sessions.create(roman)

        users.set_disabled("roman", True)

        assert sessions.resolve(token) is None

    def test_re_enabling_brings_the_same_session_back(
        self, sessions: SessionStore, users: UserStore, roman: User
    ) -> None:
        """Disabling withholds access; it does not end a session that has not expired."""
        users.create("spare", PASSWORD, Role.ADMIN)
        token, _ = sessions.create(roman)
        users.set_disabled("roman", True)

        users.set_disabled("roman", False)

        assert sessions.resolve(token) is not None

    def test_the_read_path_opens_no_write_transaction(
        self, sessions: SessionStore, roman: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every authenticated request goes through here, onto an SD card.

        Asserted on the transaction rather than on the file's mtime: in WAL mode a
        write goes to hub.db-wal, so the main file does not move and an mtime check
        passes with a write added.
        """
        token, _ = sessions.create(roman)

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("resolve() opened a write transaction")

        monkeypatch.setattr(sessions_module, "writing", refuse)

        assert sessions.resolve(token) is not None


class TestDestroy:
    def test_a_destroyed_session_stops_resolving(self, sessions: SessionStore, roman: User) -> None:
        token, _ = sessions.create(roman)

        assert sessions.destroy(token) is True
        assert sessions.resolve(token) is None

    def test_destroying_one_leaves_the_others(self, sessions: SessionStore, roman: User) -> None:
        first, _ = sessions.create(roman)
        second, _ = sessions.create(roman)

        sessions.destroy(first)

        assert sessions.resolve(second) is not None

    def test_destroying_a_token_that_does_not_exist_reports_that(
        self, sessions: SessionStore
    ) -> None:
        assert sessions.destroy("never-issued") is False

    def test_destroy_all_ends_every_session_for_one_account(
        self, sessions: SessionStore, users: UserStore, roman: User
    ) -> None:
        anna = users.create("anna", PASSWORD, Role.OPERATOR)
        mine = [sessions.create(roman)[0], sessions.create(roman)[0]]
        hers, _ = sessions.create(anna)

        assert sessions.destroy_all_for(roman) == 2

        assert all(sessions.resolve(token) is None for token in mine)
        assert sessions.resolve(hers) is not None, "another account's session is not mine to end"


class TestDeletingAnAccount:
    def test_it_takes_the_sessions_with_it(
        self, sessions: SessionStore, users: UserStore, roman: User
    ) -> None:
        """ON DELETE CASCADE, which only works because foreign keys are switched on."""
        users.create("spare", PASSWORD, Role.ADMIN)
        token, _ = sessions.create(roman)

        users.delete("roman")

        assert sessions.resolve(token) is None
        assert sessions.count() == 0


class TestExpiredRowsAreSweptUp:
    def test_opening_a_session_removes_the_ones_that_have_run_out(
        self, sessions: SessionStore, clock: Clock, roman: User, database: Path
    ) -> None:
        """Otherwise the table grows for as long as the hub runs."""
        for _ in range(3):
            sessions.create(roman)
        clock.advance(LIFETIME + timedelta(seconds=1))

        sessions.create(roman)

        with connect(database) as connection:
            rows = connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
        assert rows == 1, "the three expired rows should be gone, not merely ignored"

    def test_count_ignores_expired_rows_even_before_they_are_swept(
        self, sessions: SessionStore, clock: Clock, roman: User
    ) -> None:
        sessions.create(roman)
        assert sessions.count() == 1

        clock.advance(LIFETIME + timedelta(seconds=1))

        assert sessions.count() == 0

    def test_a_sweep_does_not_touch_a_session_that_is_still_live(
        self, sessions: SessionStore, clock: Clock, roman: User
    ) -> None:
        early, _ = sessions.create(roman)
        clock.advance(LIFETIME - timedelta(days=1))

        sessions.create(roman)

        assert sessions.resolve(early) is not None


class TestClocksOnAnotherOffset:
    """SQLite compares these columns as text, so an offset that varies between the
    stored value and the value compared against would order them by their digits
    rather than by the moments they name. The store converts before writing.

    One clock on one offset is self-consistent and proves nothing, so these mix them.
    """

    def test_a_session_written_on_one_offset_expires_by_another_clock(
        self, database: Path, users: UserStore
    ) -> None:
        kyiv = timezone(timedelta(hours=3))
        # Opened 12:00+03:00 — that is 09:00Z — and lasting an hour, so 10:00Z.
        opened = SessionStore(
            database,
            lifetime=timedelta(hours=1),
            clock=Clock(datetime(2026, 8, 17, 12, 0, tzinfo=kyiv)),
        )
        roman = users.create("roman", PASSWORD, Role.ADMIN)
        token, _ = opened.create(roman)

        # 11:00Z, an hour past expiry. Unconverted the stored '…T13:00:00+03:00'
        # compares above '…T11:00:00+00:00' on its digits, and the row reads as live.
        later = SessionStore(
            database,
            lifetime=timedelta(hours=1),
            clock=Clock(datetime(2026, 8, 17, 11, 0, tzinfo=UTC)),
        )

        assert later.count() == 0
        assert later.resolve(token) is None

    def test_the_sweep_removes_it_too(self, database: Path, users: UserStore) -> None:
        kyiv = timezone(timedelta(hours=3))
        roman = users.create("roman", PASSWORD, Role.ADMIN)
        SessionStore(
            database,
            lifetime=timedelta(hours=1),
            clock=Clock(datetime(2026, 8, 17, 12, 0, tzinfo=kyiv)),
        ).create(roman)

        later = SessionStore(
            database,
            lifetime=timedelta(hours=1),
            clock=Clock(datetime(2026, 8, 17, 11, 0, tzinfo=UTC)),
        )
        later.create(roman)

        with connect(database) as connection:
            rows = connection.execute("SELECT count(*) FROM sessions").fetchone()[0]
        assert rows == 1, "the expired row was written on another offset, not exempt"
