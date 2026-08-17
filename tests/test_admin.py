"""The ``pihome-hub-admin`` command, driven the way an operator drives it."""

from __future__ import annotations

import getpass
import io
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from pihome_hub.accounts import Role, SessionStore, UserStore
from pihome_hub.admin import main

PASSWORD = "correct-horse-battery"
OTHER_PASSWORD = "battery-staple-horse"

#: Signature of the ``admin`` fixture: argv, plus whatever is on stdin.
Admin = Callable[..., int]


class _Terminal(io.StringIO):
    """Stdin that claims to be a terminal, so the interactive paths are reachable."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "hub.db"


@pytest.fixture
def admin(database: Path, monkeypatch: pytest.MonkeyPatch) -> Admin:
    def run(*argv: str, stdin: str = "") -> int:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        return main(["--database", str(database), *argv])

    return run


@pytest.fixture
def store(database: Path) -> UserStore:
    return UserStore(database)


class TestCreate:
    def test_it_creates_an_account_that_can_then_log_in(
        self, admin: Admin, store: UserStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n") == 0

        assert "created 'roman' as admin" in capsys.readouterr().out
        assert store.authenticate("roman", PASSWORD) is not None

    def test_it_creates_the_database_if_there_is_none(self, admin: Admin, database: Path) -> None:
        """The first account on a fresh installation, before the service has ever run."""
        assert not database.exists()

        assert admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n") == 0
        assert database.exists()

    def test_a_weak_password_is_reported_and_nothing_is_created(
        self, admin: Admin, store: UserStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert admin("create", "roman", "--role", "admin", stdin="short\n") == 1

        captured = capsys.readouterr()
        assert "at least 12 characters" in captured.err
        assert store.count() == 0

    def test_a_taken_name_is_reported(
        self, admin: Admin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")

        assert admin("create", "roman", "--role", "viewer", stdin=f"{OTHER_PASSWORD}\n") == 1
        assert "already exists" in capsys.readouterr().err

    def test_a_role_that_does_not_exist_is_a_usage_error(self, admin: Admin) -> None:
        """Exit 2, argparse's — distinct from 1, which means the request was refused."""
        with pytest.raises(SystemExit) as raised:
            admin("create", "roman", "--role", "superuser", stdin=f"{PASSWORD}\n")

        assert raised.value.code == 2

    def test_the_password_is_never_echoed(
        self, admin: Admin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")

        captured = capsys.readouterr()
        assert PASSWORD not in captured.out
        assert PASSWORD not in captured.err

    def test_it_is_not_available_as_an_argument(self) -> None:
        """A command line is visible in ps and lands in shell history."""
        with pytest.raises(SystemExit):
            main(["create", "roman", "--role", "admin", "--password", PASSWORD])


class TestList:
    def test_an_empty_database_says_how_to_start(
        self, admin: Admin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert admin("list") == 0

        out = capsys.readouterr().out
        assert "no accounts yet" in out
        assert "create <username> --role admin" in out

    def test_it_shows_the_role_and_the_state(
        self, admin: Admin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")
        admin("create", "anna", "--role", "operator", stdin=f"{PASSWORD}\n")
        admin("disable", "anna")
        capsys.readouterr()

        admin("list")

        lines = capsys.readouterr().out.splitlines()
        assert lines[0].split() == ["USERNAME", "ROLE", "STATE", "CREATED"]
        assert lines[1].split()[:3] == ["anna", "operator", "disabled"]
        assert lines[2].split()[:3] == ["roman", "admin", "enabled"]


class TestPasswd:
    def test_the_new_password_replaces_the_old_one(self, admin: Admin, store: UserStore) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")

        assert admin("passwd", "roman", stdin=f"{OTHER_PASSWORD}\n") == 0
        assert store.authenticate("roman", PASSWORD) is None
        assert store.authenticate("roman", OTHER_PASSWORD) is not None

    def test_an_unknown_account_is_named(
        self, admin: Admin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert admin("passwd", "nobody", stdin=f"{PASSWORD}\n") == 1
        assert "no account named 'nobody'" in capsys.readouterr().err

    def test_an_unknown_account_is_named_even_when_the_password_is_also_bad(
        self, admin: Admin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Found by running the real command. The store hashes before it reads, so
        the weak-password message arrived first and blamed the wrong thing."""
        assert admin("passwd", "nobody", stdin="\n") == 1

        error = capsys.readouterr().err
        assert "no account named 'nobody'" in error
        assert "characters" not in error, "it should not complain about the password"

    def test_an_unacceptable_name_is_reported_before_the_password_is_asked_for(
        self, database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "stdin", _Terminal())

        def refuse(*args: object, **kwargs: object) -> str:
            raise AssertionError("a password was asked for after all")

        monkeypatch.setattr(getpass, "getpass", refuse)

        exit_code = main(["--database", str(database), "create", "no spaces", "--role", "admin"])

        assert exit_code == 1
        assert "letters and digits" in capsys.readouterr().err

    def test_two_different_answers_at_a_terminal_are_refused(
        self, database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "stdin", _Terminal())
        answers = iter([PASSWORD, OTHER_PASSWORD])
        monkeypatch.setattr(getpass, "getpass", lambda *args, **kwargs: next(answers))

        exit_code = main(["--database", str(database), "create", "roman", "--role", "admin"])

        assert exit_code == 1
        assert "did not match" in capsys.readouterr().err

    def test_two_matching_answers_at_a_terminal_are_accepted(
        self, database: Path, monkeypatch: pytest.MonkeyPatch, store: UserStore
    ) -> None:
        monkeypatch.setattr(sys, "stdin", _Terminal())
        monkeypatch.setattr(getpass, "getpass", lambda *args, **kwargs: PASSWORD)

        assert main(["--database", str(database), "create", "roman", "--role", "admin"]) == 0
        assert store.authenticate("roman", PASSWORD) is not None


class TestChangingAPasswordEndsTheSessions:
    """A new password that left the old logins running would not lock anybody out,
    which is most of the reason to change one."""

    def test_open_sessions_are_closed_and_counted(
        self, admin: Admin, store: UserStore, database: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")
        sessions = SessionStore(database)
        roman = store.get("roman")
        tokens = [sessions.create(roman)[0], sessions.create(roman)[0]]
        capsys.readouterr()

        assert admin("passwd", "roman", stdin=f"{OTHER_PASSWORD}\n") == 0

        assert "2 open session(s) closed" in capsys.readouterr().out
        assert all(sessions.resolve(token) is None for token in tokens)

    def test_another_account_keeps_its_own(
        self, admin: Admin, store: UserStore, database: Path
    ) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")
        admin("create", "anna", "--role", "operator", stdin=f"{PASSWORD}\n")
        sessions = SessionStore(database)
        hers, _ = sessions.create(store.get("anna"))

        admin("passwd", "roman", stdin=f"{OTHER_PASSWORD}\n")

        assert sessions.resolve(hers) is not None

    def test_it_says_nothing_about_sessions_when_there_were_none(
        self, admin: Admin, capsys: pytest.CaptureFixture[str]
    ) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")
        capsys.readouterr()

        admin("passwd", "roman", stdin=f"{OTHER_PASSWORD}\n")

        assert "session" not in capsys.readouterr().out


class TestRoleAndState:
    @pytest.fixture(autouse=True)
    def _two_accounts(self, admin: Admin) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")
        admin("create", "anna", "--role", "viewer", stdin=f"{PASSWORD}\n")

    def test_a_role_can_be_changed(self, admin: Admin, store: UserStore) -> None:
        assert admin("role", "anna", "operator") == 0
        assert store.get("anna").role is Role.OPERATOR

    def test_disabling_and_enabling_round_trips(self, admin: Admin, store: UserStore) -> None:
        admin("disable", "anna")
        assert store.get("anna").disabled

        admin("enable", "anna")
        assert not store.get("anna").disabled

    def test_the_last_admin_is_protected_and_told_why(
        self, admin: Admin, store: UserStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert admin("role", "roman", "viewer") == 1

        assert "only enabled admin" in capsys.readouterr().err
        assert store.get("roman").role is Role.ADMIN


class TestDelete:
    @pytest.fixture(autouse=True)
    def _two_accounts(self, admin: Admin) -> None:
        admin("create", "roman", "--role", "admin", stdin=f"{PASSWORD}\n")
        admin("create", "anna", "--role", "viewer", stdin=f"{PASSWORD}\n")

    def test_it_removes_the_account(self, admin: Admin, store: UserStore) -> None:
        assert admin("delete", "anna", "--yes") == 0
        assert [user.username for user in store.list_users()] == ["roman"]

    def test_no_terminal_means_no_confirmation_to_ask_for(
        self, admin: Admin, store: UserStore
    ) -> None:
        """A script that reached this line already said what it meant."""
        assert admin("delete", "anna") == 0
        assert store.count() == 1

    def test_a_terminal_is_asked_first(
        self, database: Path, monkeypatch: pytest.MonkeyPatch, store: UserStore
    ) -> None:
        monkeypatch.setattr(sys, "stdin", _Terminal())
        monkeypatch.setattr("builtins.input", lambda *args: "n")

        assert main(["--database", str(database), "delete", "anna"]) == 0
        assert store.count() == 2, "answering no should have kept the account"

    def test_yes_at_the_prompt_goes_through(
        self, database: Path, monkeypatch: pytest.MonkeyPatch, store: UserStore
    ) -> None:
        monkeypatch.setattr(sys, "stdin", _Terminal())
        monkeypatch.setattr("builtins.input", lambda *args: "y")

        assert main(["--database", str(database), "delete", "anna"]) == 0
        assert store.count() == 1

    def test_the_last_admin_survives_even_with_yes(
        self, admin: Admin, store: UserStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert admin("delete", "roman", "--yes") == 1

        assert "only enabled admin" in capsys.readouterr().err
        assert store.get("roman").role is Role.ADMIN


class TestRunningAsTheWrongUser:
    @pytest.mark.skipif(
        os.geteuid() == 0, reason="needs a state directory that does not belong to root"
    )
    def test_root_is_refused_and_told_which_account_to_use(
        self, database: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A database created by root is one the service account cannot write.

        systemd does not repair that, so the symptom arrives at the next start as
        "attempt to write a readonly database" — several steps from its cause.
        """
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        assert main(["--database", str(database), "list"]) == 1

        error = capsys.readouterr().err
        assert "sudo -u" in error
        assert not database.exists(), "it must refuse before creating anything"

    def test_an_ordinary_user_is_not_refused(self, admin: Admin) -> None:
        assert admin("list") == 0
