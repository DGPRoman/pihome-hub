"""Invitations: a token that opens one account, once, for fifteen minutes.

For an account made without a password the token is the whole credential, so most
of this is about the ways it must stop working — used, late, replaced, revoked, or
pointed at an account it should never open.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pihome_hub.accounts import (
    INVITATION_LIFETIME,
    InvitationStore,
    Role,
    User,
    UserStore,
)
from pihome_hub.accounts import invitations as invitations_module
from pihome_hub.storage import connect, prepare_database

PASSWORD = "correct-horse-battery"
START = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


class Clock:
    """A hand-wound clock, so expiry is tested without waiting a quarter of an hour."""

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
def invitations(database: Path, clock: Clock) -> InvitationStore:
    return InvitationStore(database, clock=clock)


@pytest.fixture
def olya(users: UserStore) -> User:
    return users.create_without_password("olya", Role.OPERATOR)


class TestRedeeming:
    def test_the_token_opens_its_account(self, invitations: InvitationStore, olya: User) -> None:
        token, _ = invitations.issue(olya)

        assert invitations.redeem(token) == olya

    def test_only_once(self, invitations: InvitationStore, olya: User) -> None:
        """Forwarded to a family chat, a link that kept working would make three
        people into one account, with no way to tell them apart or remove one."""
        token, _ = invitations.issue(olya)
        invitations.redeem(token)

        assert invitations.redeem(token) is None

    def test_a_token_nobody_issued_opens_nothing(
        self, invitations: InvitationStore, olya: User
    ) -> None:
        invitations.issue(olya)

        assert invitations.redeem("not-a-token-anyone-was-given") is None

    def test_two_presentations_at_once_open_it_once(
        self, invitations: InvitationStore, olya: User
    ) -> None:
        """The reason the lookup and the delete share an immediate transaction.

        Deferred, both threads read the row before either deletes it, and both are
        told yes.
        """
        token, _ = invitations.issue(olya)
        start = threading.Barrier(2)
        opened: list[User | None] = []

        def present() -> None:
            start.wait()
            opened.append(invitations.redeem(token))

        threads = [threading.Thread(target=present) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(opened, key=lambda user: user is None) == [olya, None]

    def test_a_second_presentation_cannot_slip_in_between(
        self, invitations: InvitationStore, olya: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same race, made to happen rather than hoped for.

        The first presentation is held between finding the row and deleting it while
        a second one runs. Under the immediate transaction the second cannot begin
        until the first commits, so it waits, the hold times out, and it finds the row
        gone. Under a deferred one it reads the row, deletes it and is told yes — and
        so, when it resumes, is the first. The thread test above catches that most of
        the time; this catches it every time.
        """
        token, _ = invitations.issue(olya)
        found = threading.Event()
        second_finished = threading.Event()
        held: list[threading.Thread] = []

        def hold_after_lookup(sql: str) -> None:
            if sql.startswith("SELECT i.expires_at") and threading.current_thread() in held:
                found.set()
                second_finished.wait(timeout=1)

        for name in ("writing", "connect"):
            monkeypatch.setattr(
                invitations_module,
                name,
                _watching(getattr(invitations_module, name), hold_after_lookup),
            )

        results: dict[str, User | None] = {}
        first = threading.Thread(target=lambda: results.update(first=invitations.redeem(token)))
        second = threading.Thread(target=lambda: results.update(second=invitations.redeem(token)))
        held.append(first)

        first.start()
        assert found.wait(timeout=5), "the first presentation never reached the lookup"
        second.start()
        second.join(timeout=10)
        second_finished.set()
        first.join(timeout=10)

        assert results == {"first": olya, "second": None}


class TestExpiry:
    def test_it_works_until_just_before_the_end(
        self, invitations: InvitationStore, clock: Clock, olya: User
    ) -> None:
        token, expires_at = invitations.issue(olya)
        clock.advance(INVITATION_LIFETIME - timedelta(seconds=1))

        assert expires_at == START + INVITATION_LIFETIME
        assert invitations.redeem(token) == olya

    def test_it_stops_at_the_end(
        self, invitations: InvitationStore, clock: Clock, olya: User
    ) -> None:
        token, _ = invitations.issue(olya)
        clock.advance(INVITATION_LIFETIME)

        assert invitations.redeem(token) is None

    def test_fifteen_minutes(self) -> None:
        """Pinned, because it is a decision rather than a default somebody tunes."""
        assert timedelta(minutes=15) == INVITATION_LIFETIME


class TestReplacingAndRevoking:
    def test_a_new_invitation_replaces_the_old_one(
        self, invitations: InvitationStore, olya: User
    ) -> None:
        """The link an admin was last shown is the only one that works."""
        first, _ = invitations.issue(olya)
        second, _ = invitations.issue(olya)

        assert invitations.redeem(first) is None
        assert invitations.redeem(second) == olya

    def test_a_revoked_invitation_opens_nothing(
        self, invitations: InvitationStore, olya: User
    ) -> None:
        token, _ = invitations.issue(olya)

        assert invitations.revoke(olya) is True
        assert invitations.redeem(token) is None

    def test_revoking_when_there_is_none_says_so(
        self, invitations: InvitationStore, olya: User
    ) -> None:
        assert invitations.revoke(olya) is False

    def test_deleting_the_account_takes_the_invitation_with_it(
        self, invitations: InvitationStore, users: UserStore, olya: User, database: Path
    ) -> None:
        invitations.issue(olya)
        users.delete("olya")

        with connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM invitations").fetchone()[0] == 0


class TestAccountsItMustNotOpen:
    def test_a_disabled_account(
        self, invitations: InvitationStore, users: UserStore, olya: User
    ) -> None:
        token, _ = invitations.issue(olya)
        users.set_disabled("olya", True)

        assert invitations.redeem(token) is None

    def test_an_account_made_admin_since_it_was_issued(
        self, invitations: InvitationStore, users: UserStore, olya: User
    ) -> None:
        """Never issued for an admin over HTTP, so this is the console raising the
        account in the meantime. An admin logs in with a password."""
        token, _ = invitations.issue(olya)
        users.set_role("olya", Role.ADMIN)

        assert invitations.redeem(token) is None

    def test_a_refused_token_is_spent_all_the_same(
        self, invitations: InvitationStore, users: UserStore, olya: User
    ) -> None:
        """Re-enabling the account must not bring back a token that was presented
        while it was disabled."""
        token, _ = invitations.issue(olya)
        users.set_disabled("olya", True)
        invitations.redeem(token)
        users.set_disabled("olya", False)

        assert invitations.redeem(token) is None


class TestWhatIsStored:
    def test_only_the_hash(self, invitations: InvitationStore, olya: User, database: Path) -> None:
        token, _ = invitations.issue(olya)

        with connect(database) as connection:
            rows = [tuple(row) for row in connection.execute("SELECT * FROM invitations")]

        assert token not in repr(rows)
        assert rows[0][0] == hashlib.sha256(token.encode()).hexdigest()

    def test_tokens_are_not_repeated(self, invitations: InvitationStore, olya: User) -> None:
        assert len({invitations.issue(olya)[0] for _ in range(20)}) == 20


class TestPending:
    def test_reports_when_each_one_stops_working(
        self, invitations: InvitationStore, users: UserStore, olya: User
    ) -> None:
        anna = users.create_without_password("anna", Role.VIEWER)
        _, olya_expires = invitations.issue(olya)

        assert invitations.pending() == {olya.id: olya_expires}
        assert anna.id not in invitations.pending()

    def test_an_expired_one_is_not_pending_before_it_is_swept(
        self, invitations: InvitationStore, clock: Clock, olya: User
    ) -> None:
        invitations.issue(olya)
        clock.advance(INVITATION_LIFETIME)

        assert invitations.pending() == {}

    def test_issuing_sweeps_the_expired_ones(
        self,
        invitations: InvitationStore,
        users: UserStore,
        clock: Clock,
        olya: User,
        database: Path,
    ) -> None:
        invitations.issue(olya)
        clock.advance(INVITATION_LIFETIME)
        invitations.issue(users.create_without_password("anna", Role.VIEWER))

        with connect(database) as connection:
            assert connection.execute("SELECT count(*) FROM invitations").fetchone()[0] == 1


class _Watched:
    """A connection that reports each statement before running it."""

    def __init__(self, connection: sqlite3.Connection, before: Callable[[str], None]) -> None:
        self._connection = connection
        self._before = before

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        self._before(sql)
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def _watching(
    opener: Callable[[Path], AbstractContextManager[sqlite3.Connection]],
    before: Callable[[str], None],
) -> Callable[[Path], AbstractContextManager[_Watched]]:
    @contextmanager
    def watched(path: Path) -> Iterator[_Watched]:
        with opener(path) as connection:
            yield _Watched(connection, before)

    return watched
