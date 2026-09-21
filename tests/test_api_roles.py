"""Who may do what, over HTTP.

Sessions authenticated a caller and nothing authorised one: role data sat in the
account store while no route compared it to anything. A viewer could switch a
mains circuit, because the question was never asked.

Two kinds of caller reach these routes and they are answered differently, which is
what most of this module is about. A session is a person and has a role. An API key
is one shared secret provisioned into firmware and scripts, with no account behind
it — it carries no role and is admitted as it always has been. The consequence is
stated in the last class here rather than left for somebody to discover.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pihome_hub.accounts import Role, UserStore
from pihome_hub.config import Settings
from pihome_hub.relays import MockRelayBackend
from pihome_hub.security import CSRF_HEADER
from tests.conftest import RELAY_HEADERS

#: Logs the shared client in as one role and hands it back.
LoginAs = Callable[[Role], TestClient]

PASSWORD = "correct-horse-battery"

#: Every route that changes the house. Named by what a refusal has to cover: the
#: two shapes — one relay and the whole group — have separate handlers, so a gate
#: applied to one of them says nothing about the other.
WRITE_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("PUT", "/v1/relays/porch-light", {"on": True}),
    ("POST", "/v1/relays/porch-light/toggle", None),
    ("PUT", "/v1/relays", {"on": True}),
    ("POST", "/v1/relays/toggle", None),
]

#: Every route that only reads it.
READ_ROUTES = [
    "/v1/relays",
    "/v1/relays/porch-light",
    "/v1/sensors",
    "/v1/automation/rules",
]


@pytest.fixture
def accounts(settings: Settings, app: FastAPI) -> UserStore:
    """One account per role. The app fixture is what prepares the schema."""
    store = UserStore(settings.database_path)
    store.create("admin-user", PASSWORD, Role.ADMIN)
    store.create("operator-user", PASSWORD, Role.OPERATOR)
    store.create("viewer-user", PASSWORD, Role.VIEWER)
    return store


@pytest.fixture
def as_role(client: TestClient, accounts: UserStore) -> Iterator[LoginAs]:
    """Log the client in as a named role, and out again between uses."""

    def login(role: Role) -> TestClient:
        client.cookies.clear()
        response = client.post(
            "/v1/session", json={"username": f"{role.value}-user", "password": PASSWORD}
        )
        assert response.status_code == HTTPStatus.CREATED, response.text
        return client

    yield login
    client.cookies.clear()


#: What a browser has to send with a cookie-authenticated write. Its presence is
#: the check; the value is never read.
CSRF = {CSRF_HEADER: "1"}


def call(client: TestClient, method: str, path: str, body: dict[str, object] | None) -> int:
    if body is None:
        return client.request(method, path, headers=CSRF).status_code
    return client.request(method, path, json=body, headers=CSRF).status_code


@pytest.mark.parametrize(("method", "path", "body"), WRITE_ROUTES)
class TestWritingTheHouse:
    def test_an_operator_may(
        self, as_role: LoginAs, method: str, path: str, body: dict[str, object] | None
    ) -> None:
        client = as_role(Role.OPERATOR)

        assert call(client, method, path, body) == HTTPStatus.OK

    def test_an_admin_may(
        self, as_role: LoginAs, method: str, path: str, body: dict[str, object] | None
    ) -> None:
        """Admin is above operator, not beside it. A gate that compared roles for
        equality rather than by rank would refuse the account that may do most."""
        client = as_role(Role.ADMIN)

        assert call(client, method, path, body) == HTTPStatus.OK

    def test_a_viewer_may_not(
        self, as_role: LoginAs, method: str, path: str, body: dict[str, object] | None
    ) -> None:
        client = as_role(Role.VIEWER)

        assert call(client, method, path, body) == HTTPStatus.FORBIDDEN

    def test_nobody_at_all_is_unauthorised_rather_than_forbidden(
        self, client: TestClient, method: str, path: str, body: dict[str, object] | None
    ) -> None:
        """401 and 403 answer different questions, and a client acts on the
        difference: one shows a login form, the other says to go and find an admin.
        Collapsing them shows a login form to somebody already logged in."""
        assert call(client, method, path, body) == HTTPStatus.UNAUTHORIZED


@pytest.mark.parametrize("path", READ_ROUTES)
class TestReadingTheHouse:
    @pytest.mark.parametrize("role", list(Role))
    def test_every_role_may_read(self, as_role: LoginAs, path: str, role: Role) -> None:
        """Viewer is the floor, so nothing readable is refused to an account."""
        client = as_role(role)

        assert client.get(path).status_code == HTTPStatus.OK

    def test_a_reader_with_no_session_and_no_key_is_refused(
        self, client: TestClient, path: str
    ) -> None:
        assert client.get(path).status_code == HTTPStatus.UNAUTHORIZED


class TestTheRefusalIsUsable:
    def test_a_forbidden_response_says_so_without_naming_the_role(self, as_role: LoginAs) -> None:
        """Enough to act on, and not a description of the account's own privileges
        echoed back to whoever is holding the cookie."""
        client = as_role(Role.VIEWER)

        response = client.put("/v1/relays/porch-light", json={"on": True}, headers=CSRF)

        assert response.status_code == HTTPStatus.FORBIDDEN
        detail = response.json()["detail"]
        assert "not allowed" in detail
        assert "viewer" not in detail.lower()
        assert "operator" not in detail.lower()

    def test_the_two_refusals_do_not_share_a_message(
        self, client: TestClient, as_role: LoginAs
    ) -> None:
        anonymous = client.put("/v1/relays/porch-light", json={"on": True}).json()["detail"]
        viewer = as_role(Role.VIEWER).put("/v1/relays/porch-light", json={"on": True}, headers=CSRF)

        assert viewer.json()["detail"] != anonymous

    def test_a_viewer_is_not_offered_a_way_to_authenticate_again(self, as_role: LoginAs) -> None:
        """WWW-Authenticate on a 403 invites a client to retry with credentials it
        already sent. Nothing it can send will help."""
        client = as_role(Role.VIEWER)

        response = client.put("/v1/relays/porch-light", json={"on": True}, headers=CSRF)

        assert "WWW-Authenticate" not in response.headers

    def test_a_refused_write_does_not_reach_the_relay(
        self, as_role: LoginAs, relay_backend: MockRelayBackend
    ) -> None:
        """The status is not the point on its own — the circuit is."""
        client = as_role(Role.VIEWER)
        before = relay_backend.is_on(17)

        client.put("/v1/relays/porch-light", json={"on": not before}, headers=CSRF)

        assert relay_backend.is_on(17) is before


class TestTheKeyIsNotAPerson:
    """What the API key still gets, and why that is not an oversight.

    One shared secret provisioned into firmware and scripts, with no account behind
    it and nobody to hold one. It carries no role and cannot be given one without
    inventing a user nothing ever logs in to.

    So the gate admits it, exactly as before. That is a real limit and this class
    exists to state it rather than let a passing suite imply otherwise: while the
    web client reaches the hub through a proxy that attaches the key, a viewer's
    browser is authorised by the key and not by their role.
    """

    @pytest.mark.parametrize(("method", "path", "body"), WRITE_ROUTES)
    def test_the_relay_key_still_writes(
        self, client: TestClient, method: str, path: str, body: dict[str, object] | None
    ) -> None:
        response = (
            client.request(method, path, json=body, headers=RELAY_HEADERS)
            if body
            else client.request(method, path, headers=RELAY_HEADERS)
        )

        assert response.status_code == HTTPStatus.OK

    def test_a_viewers_session_does_not_take_away_what_the_key_grants(
        self, as_role: LoginAs
    ) -> None:
        """Both credentials at once. The key wins, because it is checked when the
        session is not sufficient — a caller holding it could send it alone."""
        client = as_role(Role.VIEWER)

        refused = client.put("/v1/relays/porch-light", json={"on": True}, headers=CSRF)
        assert refused.status_code == HTTPStatus.FORBIDDEN

        client.cookies.clear()
        allowed = client.put("/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS)
        assert allowed.status_code == HTTPStatus.OK

    def test_a_wrong_key_with_no_session_is_still_unauthorised(self, client: TestClient) -> None:
        response = client.get("/v1/relays", headers={"X-API-Key": "wrong" * 10})

        assert response.status_code == HTTPStatus.UNAUTHORIZED


class TestABadBodyFromSomebodyLoggedIn:
    def test_it_is_a_validation_error_rather_than_a_refusal(self, as_role: LoginAs) -> None:
        """FastAPI parses a body before solving dependencies, so an unparseable one
        is guarded outside the dependency system — and that guard only knew about
        keys. A logged-in caller who got a body wrong was told they were anonymous.
        """
        client = as_role(Role.OPERATOR)

        response = client.put(
            "/v1/relays/porch-light",
            content=b"{",
            headers={"Content-Type": "application/json", **CSRF},
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_an_anonymous_caller_is_still_told_nothing(self, client: TestClient) -> None:
        """The guard exists so 422 does not map the API for somebody with no
        credentials at all. That has to keep working."""
        response = client.put(
            "/v1/relays/porch-light",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED


class TestACookieWriteMustSayItMeantIt:
    """Cross-site request forgery, on the routes that now accept a cookie.

    SECURITY.md has said since sessions landed that the exposure was nil only
    because no cookie-authenticated route changed anything, and that a defence
    belonged in the same change that first let a session switch a relay. This is
    that change.

    ``SameSite=Strict`` already keeps the cookie off any cross-site request. This
    is a second lock on the same door, because that one is a defence the *browser*
    provides and a client that does not implement SameSite does not get it. The
    header's presence is the whole check: a page on another origin cannot set one
    without a CORS preflight, and this service answers no CORS headers, so the
    preflight fails and the request is never sent.
    """

    @pytest.mark.parametrize(("method", "path", "body"), WRITE_ROUTES)
    def test_a_write_without_the_header_is_refused(
        self, as_role: LoginAs, method: str, path: str, body: dict[str, object] | None
    ) -> None:
        client = as_role(Role.OPERATOR)

        response = client.request(method, path, json=body) if body else client.request(method, path)

        assert response.status_code == HTTPStatus.FORBIDDEN
        assert CSRF_HEADER in response.json()["detail"], "the refusal should say what is missing"

    def test_the_refused_write_does_not_reach_the_relay(
        self, as_role: LoginAs, relay_backend: MockRelayBackend
    ) -> None:
        client = as_role(Role.OPERATOR)
        before = relay_backend.is_on(17)

        client.put("/v1/relays/porch-light", json={"on": not before})

        assert relay_backend.is_on(17) is before

    @pytest.mark.parametrize("path", READ_ROUTES)
    def test_reading_needs_no_header(self, as_role: LoginAs, path: str) -> None:
        """Safe methods change nothing, so there is nothing to forge."""
        client = as_role(Role.VIEWER)

        assert client.get(path).status_code == HTTPStatus.OK

    def test_the_value_is_never_read(self, as_role: LoginAs) -> None:
        """Presence is the check. A value would imply a secret this does not have,
        and inventing one would mean a token store that can fall out of step with
        the session it belongs to."""
        client = as_role(Role.OPERATOR)

        for value in ("1", "", "anything at all"):
            response = client.put(
                "/v1/relays/porch-light", json={"on": True}, headers={CSRF_HEADER: value}
            )
            assert response.status_code == HTTPStatus.OK, f"rejected the value {value!r}"

    def test_a_key_authenticated_write_needs_no_header(self, client: TestClient) -> None:
        """A key is not an ambient credential. A browser will not attach it to a
        request another page made, so there is nothing for a forged request to
        borrow — and requiring it would break every script and every device."""
        response = client.put("/v1/relays/porch-light", json={"on": True}, headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.OK
