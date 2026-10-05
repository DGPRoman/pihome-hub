"""Account administration over HTTP: who may, and how far it reaches.

The property most worth pinning is the one a reader would least expect from the rest
of ``/v1``: a valid relay key opens every relay route and none of these. Everything
else follows the role rules the relay routes already follow, with one boundary of its
own — an admin account is not a target here, whoever is asking.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pihome_hub.accounts import Role, UserStore
from pihome_hub.config import Settings
from pihome_hub.security import CSRF_HEADER, SESSION_COOKIE
from tests.conftest import RELAY_HEADERS

PASSWORD = "correct-horse-battery"

#: One request per route, each aimed at an account the route may act on.
ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/v1/users", None),
    ("PATCH", "/v1/users/viewer-user", {"disabled": True}),
    ("DELETE", "/v1/users/viewer-user", None),
]


@pytest.fixture
def accounts(settings: Settings, app: FastAPI) -> UserStore:
    """One account per role, and a second admin to aim at. The app prepares the schema."""
    store = UserStore(settings.database_path)
    store.create("admin-user", PASSWORD, Role.ADMIN)
    store.create("second-admin", PASSWORD, Role.ADMIN)
    store.create("operator-user", PASSWORD, Role.OPERATOR)
    store.create("viewer-user", PASSWORD, Role.VIEWER)
    return store


@pytest.fixture
def tokens(client: TestClient, accounts: UserStore) -> dict[Role, str]:
    """A session token per role, held here rather than in the client's cookie jar.

    Several sessions are live at once in these tests — an admin changing an account
    while that account is logged in — and one jar holds one cookie. Each request
    says which session it is.
    """
    found: dict[Role, str] = {}
    for role in (Role.ADMIN, Role.OPERATOR, Role.VIEWER):
        response = client.post(
            "/v1/session", json={"username": f"{role.value}-user", "password": PASSWORD}
        )
        assert response.status_code == HTTPStatus.CREATED, response.text
        found[role] = response.cookies[SESSION_COOKIE]
    client.cookies.clear()
    return found


def as_session(token: str, *, csrf: bool = True) -> dict[str, str]:
    headers = {"Cookie": f"{SESSION_COOKIE}={token}"}
    if csrf:
        headers[CSRF_HEADER] = "1"
    return headers


def call(
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
    headers: dict[str, str],
) -> int:
    if body is None:
        return client.request(method, path, headers=headers).status_code
    return client.request(method, path, json=body, headers=headers).status_code


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
class TestWhoMay:
    def test_an_admin_may(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        status = call(client, method, path, body, as_session(tokens[Role.ADMIN]))

        assert status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}

    def test_an_operator_may_not(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        status = call(client, method, path, body, as_session(tokens[Role.OPERATOR]))

        assert status == HTTPStatus.FORBIDDEN

    def test_a_viewer_may_not(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        status = call(client, method, path, body, as_session(tokens[Role.VIEWER]))

        assert status == HTTPStatus.FORBIDDEN

    def test_nobody_at_all_is_unauthorised(
        self, client: TestClient, method: str, path: str, body: dict[str, object] | None
    ) -> None:
        assert call(client, method, path, body, {}) == HTTPStatus.UNAUTHORIZED

    def test_the_relay_key_opens_nothing_here(
        self,
        client: TestClient,
        accounts: UserStore,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        """The key that opens every relay route. It lives in firmware and scripts,
        and none of them has any business deciding who may log in."""
        assert call(client, method, path, body, RELAY_HEADERS) == HTTPStatus.UNAUTHORIZED
        assert accounts.get("viewer-user").disabled is False

    def test_the_relay_key_does_not_lift_a_session_below_admin(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        """Together with an operator's cookie, so the key cannot be a fallback for a
        role that fell short — which is what it is on the relay routes."""
        headers = as_session(tokens[Role.OPERATOR]) | RELAY_HEADERS

        assert call(client, method, path, body, headers) == HTTPStatus.FORBIDDEN


class TestCrossSiteRequests:
    def test_a_change_without_the_header_is_refused(
        self, client: TestClient, tokens: dict[Role, str], accounts: UserStore
    ) -> None:
        response = client.patch(
            "/v1/users/viewer-user",
            json={"disabled": True},
            headers=as_session(tokens[Role.ADMIN], csrf=False),
        )

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert CSRF_HEADER in response.json()["detail"]
        assert accounts.get("viewer-user").disabled is False

    def test_a_deletion_without_the_header_is_refused(
        self, client: TestClient, tokens: dict[Role, str], accounts: UserStore
    ) -> None:
        response = client.delete(
            "/v1/users/viewer-user", headers=as_session(tokens[Role.ADMIN], csrf=False)
        )

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert accounts.get("viewer-user").role is Role.VIEWER

    def test_reading_the_list_needs_no_header(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        response = client.get("/v1/users", headers=as_session(tokens[Role.ADMIN], csrf=False))

        assert response.status_code == HTTPStatus.OK


class TestListing:
    def test_every_account_is_listed_with_nothing_secret(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        response = client.get("/v1/users", headers=as_session(tokens[Role.ADMIN]))

        users = response.json()["users"]
        assert [user["username"] for user in users] == [
            "admin-user",
            "operator-user",
            "second-admin",
            "viewer-user",
        ]
        assert {key for user in users for key in user} == {
            "username",
            "role",
            "disabled",
            "created_at",
            "invitation_expires_at",
        }
        assert "scrypt" not in response.text


class TestChanging:
    def test_a_demotion_takes_effect_on_the_next_request(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        operator = as_session(tokens[Role.OPERATOR])
        assert (
            client.put("/v1/relays/porch-light", json={"on": True}, headers=operator).status_code
            == HTTPStatus.OK
        )

        response = client.patch(
            "/v1/users/operator-user",
            json={"role": "viewer"},
            headers=as_session(tokens[Role.ADMIN]),
        )

        assert response.status_code == HTTPStatus.OK
        assert response.json()["role"] == "viewer"
        assert (
            client.put("/v1/relays/porch-light", json={"on": False}, headers=operator).status_code
            == HTTPStatus.FORBIDDEN
        )

    def test_disabling_ends_access_on_the_next_request(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        viewer = as_session(tokens[Role.VIEWER])
        assert client.get("/v1/session", headers=viewer).status_code == HTTPStatus.OK

        client.patch(
            "/v1/users/viewer-user",
            json={"disabled": True},
            headers=as_session(tokens[Role.ADMIN]),
        )

        assert client.get("/v1/session", headers=viewer).status_code == HTTPStatus.UNAUTHORIZED

    def test_both_fields_at_once(
        self, client: TestClient, tokens: dict[Role, str], accounts: UserStore
    ) -> None:
        client.patch(
            "/v1/users/operator-user",
            json={"role": "viewer", "disabled": True},
            headers=as_session(tokens[Role.ADMIN]),
        )

        changed = accounts.get("operator-user")
        assert (changed.role, changed.disabled) == (Role.VIEWER, True)

    def test_the_name_is_matched_as_the_login_matches_it(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        """Ignoring case, like every other lookup, and answered with the stored name."""
        response = client.patch(
            "/v1/users/VIEWER-USER",
            json={"role": "operator"},
            headers=as_session(tokens[Role.ADMIN]),
        )

        assert response.status_code == HTTPStatus.OK
        assert response.json()["username"] == "viewer-user"

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"role": "admin"}, id="admin-is-console-only"),
            pytest.param({}, id="nothing-named"),
            pytest.param({"role": "VIEWER"}, id="case-is-not-guessed"),
            pytest.param({"disabled": "yes"}, id="not-a-boolean"),
            pytest.param({"password": "something-long-enough"}, id="unknown-field"),
        ],
    )
    def test_a_body_that_asks_for_something_else_is_refused(
        self,
        client: TestClient,
        tokens: dict[Role, str],
        accounts: UserStore,
        body: dict[str, object],
    ) -> None:
        response = client.patch(
            "/v1/users/viewer-user", json=body, headers=as_session(tokens[Role.ADMIN])
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert accounts.get("viewer-user").role is Role.VIEWER

    @pytest.mark.parametrize("target", ["second-admin", "admin-user"])
    def test_an_admin_account_is_not_a_target(
        self, client: TestClient, tokens: dict[Role, str], accounts: UserStore, target: str
    ) -> None:
        """Another admin, and the caller's own account. Neither is changed from here,
        so a session taken over in a browser cannot lock the real admin out."""
        response = client.patch(
            f"/v1/users/{target}",
            json={"disabled": True},
            headers=as_session(tokens[Role.ADMIN]),
        )

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert "pihome-hub-admin" in response.json()["detail"]
        assert accounts.get(target).disabled is False

    def test_an_account_that_does_not_exist_is_not_found(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        response = client.patch(
            "/v1/users/nobody", json={"disabled": True}, headers=as_session(tokens[Role.ADMIN])
        )

        assert response.status_code == HTTPStatus.NOT_FOUND


class TestDeleting:
    def test_the_account_and_its_session_go(
        self, client: TestClient, tokens: dict[Role, str], accounts: UserStore
    ) -> None:
        viewer = as_session(tokens[Role.VIEWER])

        response = client.delete("/v1/users/viewer-user", headers=as_session(tokens[Role.ADMIN]))

        assert response.status_code == HTTPStatus.NO_CONTENT
        assert "viewer-user" not in {user.username for user in accounts.list_users()}
        assert client.get("/v1/session", headers=viewer).status_code == HTTPStatus.UNAUTHORIZED

    def test_an_admin_account_is_not_deleted(
        self, client: TestClient, tokens: dict[Role, str], accounts: UserStore
    ) -> None:
        response = client.delete("/v1/users/second-admin", headers=as_session(tokens[Role.ADMIN]))

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert accounts.get("second-admin").role is Role.ADMIN

    def test_an_account_that_does_not_exist_is_not_found(
        self, client: TestClient, tokens: dict[Role, str]
    ) -> None:
        response = client.delete("/v1/users/nobody", headers=as_session(tokens[Role.ADMIN]))

        assert response.status_code == HTTPStatus.NOT_FOUND
