"""Joining an account by invitation, over HTTP, from both ends.

An admin makes the account and issues the token; a person presents it at the login
route and gets a session. The tests at the end are the ones a reader would least
think to write: what must not happen to the token on the way — that a refused
attempt does not spend it, and that it reaches no log.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pihome_hub.accounts import INVITATION_LIFETIME, InvitationStore, Role, UserStore
from pihome_hub.config import Settings
from pihome_hub.security import CSRF_HEADER, SESSION_COOKIE
from tests.conftest import RELAY_HEADERS

PASSWORD = "correct-horse-battery"

#: What the web client sends with every write, and what a redemption must carry.
CSRF = {CSRF_HEADER: "1"}


@pytest.fixture
def accounts(settings: Settings, app: FastAPI) -> UserStore:
    store = UserStore(settings.database_path)
    store.create("admin-user", PASSWORD, Role.ADMIN)
    store.create("operator-user", PASSWORD, Role.OPERATOR)
    return store


def _log_in(client: TestClient, username: str) -> str:
    response = client.post("/v1/session", json={"username": username, "password": PASSWORD})
    assert response.status_code == HTTPStatus.CREATED, response.text
    token = response.cookies[SESSION_COOKIE]
    client.cookies.clear()
    return token


@pytest.fixture
def admin(client: TestClient, accounts: UserStore) -> dict[str, str]:
    """Headers for an admin's session, with the CSRF header a browser would send."""
    return {"Cookie": f"{SESSION_COOKIE}={_log_in(client, 'admin-user')}"} | CSRF


@pytest.fixture
def olya(client: TestClient, admin: dict[str, str]) -> str:
    """An operator account made for an invitation. Returns its name."""
    response = client.post(
        "/v1/users", json={"username": "olya", "role": "operator"}, headers=admin
    )
    assert response.status_code == HTTPStatus.CREATED, response.text
    return "olya"


def _invite(client: TestClient, admin: dict[str, str], username: str) -> str:
    response = client.post(f"/v1/users/{username}/invitation", headers=admin)
    assert response.status_code == HTTPStatus.CREATED, response.text
    token: str = response.json()["token"]
    return token


def _join(client: TestClient, token: str, *, csrf: bool = True) -> int:
    client.cookies.clear()
    response = client.post("/v1/session", json={"invitation": token}, headers=CSRF if csrf else {})
    return response.status_code


class TestCreatingAnAccount:
    def test_an_admin_creates_one_with_no_invitation_yet(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/users", json={"username": "olya", "role": "viewer"}, headers=admin
        )

        assert response.status_code == HTTPStatus.CREATED
        body = response.json()
        assert (body["username"], body["role"], body["invitation_expires_at"]) == (
            "olya",
            "viewer",
            None,
        )

    @pytest.mark.parametrize("guess", ["", "!", PASSWORD])
    def test_no_password_opens_it_and_none_is_a_500(
        self, client: TestClient, olya: str, guess: str
    ) -> None:
        """The empty string and ``!`` are the sentinels a careless version would
        have stored, and a 500 on either would mark the account from outside."""
        response = client.post("/v1/session", json={"username": olya, "password": guess})

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json() == {"detail": "Invalid username or password"}

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param(
                {"username": "olya", "role": "admin"},
                HTTPStatus.UNPROCESSABLE_ENTITY,
                id="admin-is-console-only",
            ),
            pytest.param(
                {"username": "olya kovalenko", "role": "viewer"},
                HTTPStatus.UNPROCESSABLE_ENTITY,
                id="not-a-username",
            ),
            pytest.param(
                {"username": "Operator-User", "role": "viewer"},
                HTTPStatus.CONFLICT,
                id="taken-ignoring-case",
            ),
            pytest.param(
                {"username": "olya", "role": "viewer", "password": "something-long-enough"},
                HTTPStatus.UNPROCESSABLE_ENTITY,
                id="no-password-field",
            ),
        ],
    )
    def test_what_it_refuses(
        self,
        client: TestClient,
        admin: dict[str, str],
        accounts: UserStore,
        body: dict[str, object],
        expected: HTTPStatus,
    ) -> None:
        response = client.post("/v1/users", json=body, headers=admin)

        assert response.status_code == expected
        assert {user.username for user in accounts.list_users()} == {
            "admin-user",
            "operator-user",
        }

    def test_the_relay_key_cannot_make_one(self, client: TestClient, accounts: UserStore) -> None:
        response = client.post(
            "/v1/users", json={"username": "olya", "role": "viewer"}, headers=RELAY_HEADERS
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert {user.username for user in accounts.list_users()} == {"admin-user", "operator-user"}


class TestInviting:
    def test_the_token_comes_back_once_with_fifteen_minutes_on_it(
        self, client: TestClient, admin: dict[str, str], olya: str
    ) -> None:
        before = datetime.now(UTC)
        response = client.post(f"/v1/users/{olya}/invitation", headers=admin)

        assert response.status_code == HTTPStatus.CREATED
        expires_at = datetime.fromisoformat(response.json()["expires_at"])
        assert (
            before + INVITATION_LIFETIME
            <= expires_at
            <= before + INVITATION_LIFETIME + (timedelta(seconds=5))
        )

    def test_the_list_says_when_it_runs_out_and_nothing_more(
        self, client: TestClient, admin: dict[str, str], olya: str
    ) -> None:
        token = _invite(client, admin, olya)

        response = client.get("/v1/users", headers=admin)

        listed = {user["username"]: user for user in response.json()["users"]}
        assert listed[olya]["invitation_expires_at"] is not None
        assert listed["operator-user"]["invitation_expires_at"] is None
        assert token not in response.text

    def test_an_account_with_a_password_can_be_invited_too(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        """A new phone for somebody who has always logged in with a password."""
        token = _invite(client, admin, "operator-user")

        assert _join(client, token) == HTTPStatus.CREATED

    def test_an_admin_account_is_not_invited(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        response = client.post("/v1/users/admin-user/invitation", headers=admin)

        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_a_disabled_account_is_not_invited(
        self, client: TestClient, admin: dict[str, str], olya: str
    ) -> None:
        client.patch(f"/v1/users/{olya}", json={"disabled": True}, headers=admin)

        response = client.post(f"/v1/users/{olya}/invitation", headers=admin)

        assert response.status_code == HTTPStatus.CONFLICT

    def test_an_account_that_does_not_exist(
        self, client: TestClient, admin: dict[str, str]
    ) -> None:
        response = client.post("/v1/users/nobody/invitation", headers=admin)

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_the_relay_key_cannot_issue_one(self, client: TestClient, olya: str) -> None:
        response = client.post(f"/v1/users/{olya}/invitation", headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_an_operator_cannot_issue_one(self, client: TestClient, olya: str) -> None:
        operator = {"Cookie": f"{SESSION_COOKIE}={_log_in(client, 'operator-user')}"} | CSRF

        response = client.post(f"/v1/users/{olya}/invitation", headers=operator)

        assert response.status_code == HTTPStatus.FORBIDDEN


class TestJoining:
    def test_the_token_opens_a_session_for_its_account(
        self, client: TestClient, admin: dict[str, str], olya: str
    ) -> None:
        token = _invite(client, admin, olya)
        client.cookies.clear()

        response = client.post("/v1/session", json={"invitation": token}, headers=CSRF)

        assert response.status_code == HTTPStatus.CREATED
        assert set(response.json()) == {"username", "role", "expires_at"}
        assert response.json()["username"] == olya
        assert client.get("/v1/session").json()["username"] == olya

    def test_only_once(self, client: TestClient, admin: dict[str, str], olya: str) -> None:
        token = _invite(client, admin, olya)
        _join(client, token)

        client.cookies.clear()
        response = client.post("/v1/session", json={"invitation": token}, headers=CSRF)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert "new one" in response.json()["detail"]

    def test_not_after_fifteen_minutes(
        self,
        client: TestClient,
        app: FastAPI,
        settings: Settings,
        admin: dict[str, str],
        olya: str,
    ) -> None:
        token = _invite(client, admin, olya)
        app.state.invitations = InvitationStore(
            settings.database_path, clock=lambda: datetime.now(UTC) + INVITATION_LIFETIME
        )

        assert _join(client, token) == HTTPStatus.UNAUTHORIZED

    def test_not_once_revoked(self, client: TestClient, admin: dict[str, str], olya: str) -> None:
        token = _invite(client, admin, olya)

        first = client.delete(f"/v1/users/{olya}/invitation", headers=admin)
        again = client.delete(f"/v1/users/{olya}/invitation", headers=admin)

        assert (first.status_code, again.status_code) == (
            HTTPStatus.NO_CONTENT,
            HTTPStatus.NO_CONTENT,
        )
        assert _join(client, token) == HTTPStatus.UNAUTHORIZED

    def test_not_once_replaced(self, client: TestClient, admin: dict[str, str], olya: str) -> None:
        first = _invite(client, admin, olya)
        second = _invite(client, admin, olya)

        assert _join(client, first) == HTTPStatus.UNAUTHORIZED
        assert _join(client, second) == HTTPStatus.CREATED

    def test_a_password_and_an_invitation_together_are_neither(self, client: TestClient) -> None:
        response = client.post(
            "/v1/session",
            json={"username": "admin-user", "password": PASSWORD, "invitation": "x"},
            headers=CSRF,
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_a_failure_counts_where_a_wrong_password_does(
        self, client: TestClient, app: FastAPI
    ) -> None:
        """One bucket for every way of guessing at the way in."""
        _join(client, "a-token-nobody-was-given")

        assert app.state.auth_limiter.failure_count("login:testclient") == 1


class TestWhatMustNotHappenToTheToken:
    def test_a_redemption_without_the_header_is_refused_and_does_not_spend_it(
        self, client: TestClient, admin: dict[str, str], olya: str
    ) -> None:
        """Another page cannot set the header, so this is the forged attempt. Had
        it spent the token, any page could burn somebody's invitation."""
        token = _invite(client, admin, olya)

        assert _join(client, token, csrf=False) == HTTPStatus.FORBIDDEN
        assert _join(client, token) == HTTPStatus.CREATED

    def test_it_reaches_no_log(
        self,
        client: TestClient,
        admin: dict[str, str],
        olya: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Issued, refused, and accepted — every path a token takes through the hub.

        The fields as well as the message, because that is where this hub writes the
        detail of a request, and a token passed as ``extra`` would never show up in
        the text alone.
        """
        caplog.set_level(logging.DEBUG)

        token = _invite(client, admin, olya)
        _join(client, token, csrf=False)
        _join(client, token)
        _join(client, token)

        assert caplog.records, "nothing was logged — this test is not looking"
        assert all(token not in repr(vars(record)) for record in caplog.records)
