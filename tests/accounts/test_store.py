"""The accounts table, through the operations that are allowed to change it."""

from __future__ import annotations

import base64
import hashlib
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pihome_hub.accounts import (
    DuplicateUsernameError,
    InvalidUsernameError,
    LastAdminError,
    Role,
    UnknownUserError,
    User,
    UserStore,
    WeakPasswordError,
    needs_rehash,
)
from pihome_hub.accounts import store as store_module
from pihome_hub.storage import connect, prepare_database

PASSWORD = "correct-horse-battery"
OTHER_PASSWORD = "battery-staple-horse"


@pytest.fixture
def store(tmp_path: Path) -> UserStore:
    path = tmp_path / "hub.db"
    prepare_database(path)
    return UserStore(path)


class TestCreate:
    def test_it_returns_the_account_it_stored(self, store: UserStore) -> None:
        user = store.create("roman", PASSWORD, Role.ADMIN)

        assert user.username == "roman"
        assert user.role is Role.ADMIN
        assert not user.disabled
        assert user.created_at.tzinfo is not None

    def test_the_password_works_afterwards(self, store: UserStore) -> None:
        store.create("roman", PASSWORD, Role.ADMIN)

        assert store.authenticate("roman", PASSWORD) is not None

    def test_a_taken_name_is_refused(self, store: UserStore) -> None:
        store.create("roman", PASSWORD, Role.ADMIN)

        with pytest.raises(DuplicateUsernameError, match="already exists"):
            store.create("roman", OTHER_PASSWORD, Role.VIEWER)

    def test_a_taken_name_in_another_case_is_refused(self, store: UserStore) -> None:
        """'Roman' and 'roman' as two accounts is a phishing affordance."""
        store.create("roman", PASSWORD, Role.ADMIN)

        with pytest.raises(DuplicateUsernameError):
            store.create("Roman", OTHER_PASSWORD, Role.VIEWER)

    def test_a_name_this_service_will_not_accept_is_refused(self, store: UserStore) -> None:
        with pytest.raises(InvalidUsernameError):
            store.create("roman pelenychko", PASSWORD, Role.ADMIN)

    def test_a_weak_password_leaves_nothing_behind(self, store: UserStore) -> None:
        """The hash is computed before the transaction, so this must not half-write."""
        with pytest.raises(WeakPasswordError):
            store.create("roman", "short", Role.ADMIN)

        assert store.count() == 0

    def test_the_creation_time_comes_from_the_clock(self, tmp_path: Path) -> None:
        moment = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
        path = tmp_path / "hub.db"
        prepare_database(path)

        user = UserStore(path, clock=lambda: moment).create("roman", PASSWORD, Role.ADMIN)

        assert user.created_at == moment


class TestAuthenticate:
    def test_the_right_password_returns_the_account(self, store: UserStore) -> None:
        created = store.create("roman", PASSWORD, Role.OPERATOR)

        assert store.authenticate("roman", PASSWORD) == created

    def test_the_wrong_password_returns_nothing(self, store: UserStore) -> None:
        store.create("roman", PASSWORD, Role.OPERATOR)

        assert store.authenticate("roman", OTHER_PASSWORD) is None

    def test_an_account_that_does_not_exist_returns_nothing(self, store: UserStore) -> None:
        assert store.authenticate("nobody", PASSWORD) is None

    def test_the_name_is_matched_ignoring_case(self, store: UserStore) -> None:
        store.create("roman", PASSWORD, Role.OPERATOR)

        assert store.authenticate("ROMAN", PASSWORD) is not None

    def test_a_disabled_account_is_refused_even_with_the_right_password(
        self, store: UserStore
    ) -> None:
        store.create("admin", PASSWORD, Role.ADMIN)
        store.create("roman", PASSWORD, Role.OPERATOR)
        store.set_disabled("roman", True)

        assert store.authenticate("roman", PASSWORD) is None

    def test_re_enabling_restores_the_original_password(self, store: UserStore) -> None:
        """Disabling holds an account back; it does not throw its credentials away."""
        store.create("admin", PASSWORD, Role.ADMIN)
        store.create("roman", OTHER_PASSWORD, Role.OPERATOR)
        store.set_disabled("roman", True)
        store.set_disabled("roman", False)

        assert store.authenticate("roman", OTHER_PASSWORD) is not None

    def test_an_account_carries_no_password_hash(self) -> None:
        """It is read inside the store and never handed out, including to a route."""
        assert "password_hash" not in User.model_fields


class TestAuthenticateUpgradesAnOldHash:
    def test_a_hash_at_weaker_parameters_is_replaced_on_login(
        self, store: UserStore, tmp_path: Path
    ) -> None:
        """A login is the one moment the plaintext is in hand."""
        store.create("roman", PASSWORD, Role.ADMIN)
        _downgrade_hash(tmp_path / "hub.db", "roman")

        with connect(tmp_path / "hub.db") as connection:
            stored = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        assert needs_rehash(stored), "the fixture failed to weaken anything"

        assert store.authenticate("roman", PASSWORD) is not None

        with connect(tmp_path / "hub.db") as connection:
            upgraded = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        assert not needs_rehash(upgraded)
        assert store.authenticate("roman", PASSWORD) is not None


class TestListingAndFetching:
    def test_accounts_are_listed_in_a_readable_order(self, store: UserStore) -> None:
        """Alphabetical ignoring case. Names chosen so a binary sort gives another
        answer — 'Zoya' sorts before 'anna' on byte value, which no reader expects."""
        for name in ("anna", "Zoya", "roman"):
            store.create(name, PASSWORD, Role.VIEWER)

        assert [user.username for user in store.list_users()] == ["anna", "roman", "Zoya"]

    def test_an_empty_database_lists_nothing(self, store: UserStore) -> None:
        assert store.list_users() == []
        assert store.count() == 0

    def test_get_ignores_case(self, store: UserStore) -> None:
        store.create("Roman", PASSWORD, Role.ADMIN)

        assert store.get("rOmAn").username == "Roman", "the stored spelling is what was typed"

    def test_get_names_the_account_it_could_not_find(self, store: UserStore) -> None:
        with pytest.raises(UnknownUserError, match="nobody"):
            store.get("nobody")


class TestTheLastAdmin:
    """Locking the service out of its own administration is not undoable in it."""

    @pytest.fixture
    def one_admin(self, store: UserStore) -> UserStore:
        store.create("roman", PASSWORD, Role.ADMIN)
        store.create("anna", PASSWORD, Role.OPERATOR)
        return store

    def test_deleting_the_only_admin_is_refused(self, one_admin: UserStore) -> None:
        with pytest.raises(LastAdminError, match="only enabled admin"):
            one_admin.delete("roman")

        assert one_admin.get("roman").role is Role.ADMIN

    def test_disabling_the_only_admin_is_refused(self, one_admin: UserStore) -> None:
        with pytest.raises(LastAdminError):
            one_admin.set_disabled("roman", True)

        assert not one_admin.get("roman").disabled

    def test_demoting_the_only_admin_is_refused(self, one_admin: UserStore) -> None:
        with pytest.raises(LastAdminError):
            one_admin.set_role("roman", Role.OPERATOR)

        assert one_admin.get("roman").role is Role.ADMIN

    def test_a_second_disabled_admin_does_not_count(self, one_admin: UserStore) -> None:
        """An account that cannot log in cannot administer anything."""
        one_admin.create("spare", PASSWORD, Role.ADMIN)
        one_admin.set_disabled("spare", True)

        with pytest.raises(LastAdminError):
            one_admin.delete("roman")

    def test_a_second_enabled_admin_makes_all_three_allowed(self, one_admin: UserStore) -> None:
        one_admin.create("spare", PASSWORD, Role.ADMIN)

        one_admin.set_role("roman", Role.OPERATOR)
        one_admin.set_disabled("roman", True)
        one_admin.delete("roman")

        assert [user.username for user in one_admin.list_users()] == ["anna", "spare"]

    def test_removing_someone_who_is_not_an_admin_is_fine(self, one_admin: UserStore) -> None:
        one_admin.delete("anna")

        assert one_admin.count() == 1

    def test_promoting_is_never_refused(self, one_admin: UserStore) -> None:
        assert one_admin.set_role("anna", Role.ADMIN).role is Role.ADMIN

    def test_the_last_admin_can_still_change_their_own_password(self, one_admin: UserStore) -> None:
        """The guard is about losing access, and this is how access is kept."""
        one_admin.set_password("roman", OTHER_PASSWORD)

        assert one_admin.authenticate("roman", OTHER_PASSWORD) is not None


class TestSetPassword:
    def test_the_old_password_stops_working(self, store: UserStore) -> None:
        store.create("roman", PASSWORD, Role.ADMIN)
        store.set_password("roman", OTHER_PASSWORD)

        assert store.authenticate("roman", PASSWORD) is None
        assert store.authenticate("roman", OTHER_PASSWORD) is not None

    def test_a_weak_new_password_leaves_the_old_one_working(self, store: UserStore) -> None:
        store.create("roman", PASSWORD, Role.ADMIN)

        with pytest.raises(WeakPasswordError):
            store.set_password("roman", "short")

        assert store.authenticate("roman", PASSWORD) is not None

    def test_an_account_that_does_not_exist_is_named(self, store: UserStore) -> None:
        with pytest.raises(UnknownUserError, match="nobody"):
            store.set_password("nobody", PASSWORD)


class TestATransactionIsAllOrNothing:
    def test_a_failure_after_a_write_leaves_nothing_behind(
        self, store: UserStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No method raises after writing today, and the rollback is what keeps that
        cheap to change: without it, one added step would start half-applying."""
        store.create("roman", PASSWORD, Role.ADMIN)
        store.create("anna", PASSWORD, Role.OPERATOR)

        def explode(*args: object, **kwargs: object) -> User:
            raise RuntimeError("a step added after the write")

        monkeypatch.setattr(store_module, "_reload", explode)

        with pytest.raises(RuntimeError):
            store.set_role("anna", Role.VIEWER)

        assert store.get("anna").role is Role.OPERATOR


class TestTwoWritersAtOnce:
    def test_two_threads_cannot_both_remove_the_last_admin(self, store: UserStore) -> None:
        """The reason the transaction is immediate rather than deferred.

        Deferred, both threads read "there are two admins" before either takes the
        write lock, and each then deletes a different one.
        """
        store.create("first", PASSWORD, Role.ADMIN)
        store.create("second", PASSWORD, Role.ADMIN)

        start = threading.Barrier(2)
        failures: list[BaseException] = []

        def remove(username: str) -> None:
            start.wait()
            try:
                store.delete(username)
            except BaseException as exc:  # recorded rather than raised; asserted on below
                failures.append(exc)

        threads = [
            threading.Thread(target=remove, args=("first",)),
            threading.Thread(target=remove, args=("second",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        remaining = store.list_users()
        assert len(remaining) == 1, f"both deletions went through: {failures}"
        assert remaining[0].role is Role.ADMIN
        assert len(failures) == 1
        assert isinstance(failures[0], LastAdminError)


def _downgrade_hash(path: Path, username: str) -> None:
    """Replace a stored hash with one an older build would have written."""
    salt = b"0123456789abcdef"
    key = hashlib.scrypt(
        PASSWORD.encode("utf-8"), salt=salt, n=1024, r=8, p=1, dklen=32, maxmem=64 * 1024**2
    )
    weaker = "$".join(
        (
            "scrypt",
            "n=1024,r=8,p=1",
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(key).decode("ascii"),
        )
    )
    with connect(path) as connection:
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?", (weaker, username)
        )
        connection.commit()
