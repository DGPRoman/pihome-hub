"""Logging in over HTTP: the cookie, the refusals, and what the response says."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.cookies import SimpleCookie

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from pihome_hub.accounts import Role, SessionStore, UserStore
from pihome_hub.app import create_app
from pihome_hub.config import Settings
from pihome_hub.relays import RelayService
from pihome_hub.security import SESSION_COOKIE
from pihome_hub.storage import prepare_database
from tests.conftest import RELAY_HEADERS, build_settings

PASSWORD = "correct-horse-battery"
OTHER_PASSWORD = "battery-staple-horse"
LOGIN = "/v1/session"


@pytest.fixture
def users(settings: Settings, app: FastAPI) -> UserStore:
    """The app fixture prepares the schema, so this depends on it having run."""
    store = UserStore(settings.database_path)
    store.create("roman", PASSWORD, Role.ADMIN)
    store.create("anna", PASSWORD, Role.VIEWER)
    return store


@pytest.fixture
def sessions(settings: Settings, app: FastAPI) -> SessionStore:
    return SessionStore(settings.database_path)


@contextmanager
def _hub_with(relays: RelayService, **overrides: object) -> Iterator[TestClient]:
    """A hub on its own settings, with one admin already created.

    Its own app rather than the shared fixture, because these change settings that
    are read when the application is built.
    """
    settings = build_settings(**overrides)
    prepare_database(settings.database_path)
    UserStore(settings.database_path).create("roman", PASSWORD, Role.ADMIN)

    with TestClient(create_app(settings, relay_service=relays)) as client:
        yield client


def _set_cookie_attributes(response: Response) -> dict[str, str]:
    """The Set-Cookie header, parsed. httpx keeps only the value, not the flags."""
    header = response.headers.get("set-cookie")
    assert header is not None, "no Set-Cookie header at all"
    jar = SimpleCookie()
    jar.load(header)
    return dict(jar[SESSION_COOKIE])


class TestLogIn:
    def test_the_right_password_opens_a_session(self, client: TestClient, users: UserStore) -> None:
        response = client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        assert response.status_code == HTTPStatus.CREATED
        body = response.json()
        assert body["username"] == "roman"
        assert body["role"] == "admin"
        assert body["expires_at"]

    def test_it_sets_a_cookie_the_browser_will_keep_to_itself(
        self, client: TestClient, users: UserStore
    ) -> None:
        response = client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        attributes = _set_cookie_attributes(response)
        assert attributes["httponly"], "readable from JavaScript would survive one XSS bug"
        assert attributes["samesite"].lower() == "strict"
        assert attributes["path"] == "/"
        assert attributes["max-age"] == str(build_settings().session_lifetime_seconds)

    def test_the_cookie_is_not_marked_secure_over_plain_http(
        self, client: TestClient, users: UserStore
    ) -> None:
        """Marked Secure it would never be sent back, and login would silently do nothing."""
        response = client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        assert not _set_cookie_attributes(response)["secure"]

    def test_it_is_marked_secure_when_configured(self, relay_service: RelayService) -> None:
        with _hub_with(relay_service, session_cookie_secure=True) as client:
            response = client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        assert _set_cookie_attributes(response)["secure"]

    def test_the_token_appears_nowhere_but_the_cookie(
        self, client: TestClient, users: UserStore
    ) -> None:
        """Not the body and not another header.

        Checking only the body was not enough: a header carrying it passes that, and a
        reverse proxy logs response headers as readily as it logs a URL.
        """
        response = client.post(LOGIN, json={"username": "roman", "password": PASSWORD})
        token = client.cookies[SESSION_COOKIE]

        assert token
        assert token not in response.text
        elsewhere = {
            name: value
            for name, value in response.headers.items()
            if name.lower() != "set-cookie" and token in value
        }
        assert not elsewhere

    def test_the_name_is_matched_ignoring_case(self, client: TestClient, users: UserStore) -> None:
        response = client.post(LOGIN, json={"username": "ROMAN", "password": PASSWORD})

        assert response.status_code == HTTPStatus.CREATED

    @pytest.mark.parametrize(
        ("username", "password", "case"),
        [
            ("roman", OTHER_PASSWORD, "wrong password"),
            ("nobody", PASSWORD, "no such account"),
            ("", PASSWORD, "no username at all"),
            ("roman", "", "no password at all"),
        ],
    )
    def test_every_kind_of_wrong_gets_the_same_answer(
        self, client: TestClient, users: UserStore, username: str, password: str, case: str
    ) -> None:
        """Which one it was is not the caller's to learn, so neither is the wording."""
        response = client.post(LOGIN, json={"username": username, "password": password})

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json() == {"detail": "Invalid username or password"}
        assert SESSION_COOKIE not in client.cookies

    def test_a_disabled_account_cannot_log_in(self, client: TestClient, users: UserStore) -> None:
        users.set_disabled("anna", True)

        response = client.post(LOGIN, json={"username": "anna", "password": PASSWORD})

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_an_unknown_field_is_refused(self, client: TestClient, users: UserStore) -> None:
        response = client.post(
            LOGIN, json={"username": "roman", "password": PASSWORD, "remember": True}
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY

    def test_a_malformed_body_is_explained_rather_than_asking_for_a_key(
        self, client: TestClient, users: UserStore
    ) -> None:
        """The one path under /v1 exempt from the handler that answers 401 before 422.

        Without the exemption the way in would be reachable only by callers who
        already hold an API key, which is nobody who needs to log in.
        """
        response = client.post(
            LOGIN, content=b"{not json", headers={"content-type": "application/json"}
        )

        assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert "API key" not in response.text

    def test_a_malformed_body_elsewhere_still_asks_for_a_key(self, client: TestClient) -> None:
        """The exemption is one path, not a hole under /v1."""
        response = client.put(
            "/v1/relays", content=b"{not json", headers={"content-type": "application/json"}
        )

        assert response.status_code == HTTPStatus.UNAUTHORIZED


class TestReadSession:
    def test_it_reports_the_account_behind_the_cookie(
        self, client: TestClient, users: UserStore
    ) -> None:
        client.post(LOGIN, json={"username": "anna", "password": PASSWORD})

        response = client.get(LOGIN)

        assert response.status_code == HTTPStatus.OK
        assert response.json()["username"] == "anna"
        assert response.json()["role"] == "viewer"

    def test_without_a_cookie_it_refuses(self, client: TestClient, users: UserStore) -> None:
        response = client.get(LOGIN)

        assert response.status_code == HTTPStatus.UNAUTHORIZED
        assert response.json() == {"detail": "Not authenticated"}

    def test_a_cookie_that_was_never_issued_refuses(
        self, client: TestClient, users: UserStore
    ) -> None:
        client.cookies.set(SESSION_COOKIE, "not-a-token-anybody-ever-had")

        assert client.get(LOGIN).status_code == HTTPStatus.UNAUTHORIZED

    def test_no_www_authenticate_header_is_offered(
        self, client: TestClient, users: UserStore
    ) -> None:
        """There is no registered scheme for cookies, and inventing one would only
        prompt some clients to show a basic-auth dialog that cannot work here."""
        response = client.get(LOGIN)

        assert "www-authenticate" not in response.headers

    def test_an_api_key_does_not_stand_in_for_a_session(self, client: TestClient) -> None:
        """The key says a device is trusted; it does not say which person is asking."""
        response = client.get(LOGIN, headers=RELAY_HEADERS)

        assert response.status_code == HTTPStatus.UNAUTHORIZED

    def test_disabling_the_account_takes_effect_at_once(
        self, client: TestClient, users: UserStore
    ) -> None:
        client.post(LOGIN, json={"username": "anna", "password": PASSWORD})
        assert client.get(LOGIN).status_code == HTTPStatus.OK

        users.set_disabled("anna", True)

        assert client.get(LOGIN).status_code == HTTPStatus.UNAUTHORIZED

    def test_a_role_change_takes_effect_at_once(self, client: TestClient, users: UserStore) -> None:
        client.post(LOGIN, json={"username": "anna", "password": PASSWORD})

        users.set_role("anna", Role.OPERATOR)

        assert client.get(LOGIN).json()["role"] == "operator"


class TestLogOut:
    def test_it_ends_the_session(self, client: TestClient, users: UserStore) -> None:
        client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        assert client.delete(LOGIN).status_code == HTTPStatus.NO_CONTENT
        assert client.get(LOGIN).status_code == HTTPStatus.UNAUTHORIZED

    def test_the_token_stops_working_even_if_the_cookie_comes_back(
        self, client: TestClient, users: UserStore, sessions: SessionStore
    ) -> None:
        """The row is deleted, not merely un-set on this client."""
        client.post(LOGIN, json={"username": "roman", "password": PASSWORD})
        token = client.cookies[SESSION_COOKIE]

        client.delete(LOGIN)
        client.cookies.set(SESSION_COOKIE, token)

        assert client.get(LOGIN).status_code == HTTPStatus.UNAUTHORIZED
        assert sessions.resolve(token) is None

    def test_it_tells_the_browser_to_drop_the_cookie(
        self, client: TestClient, users: UserStore
    ) -> None:
        client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        response = client.delete(LOGIN)

        attributes = _set_cookie_attributes(response)
        # Every attribute that identified the cookie has to come back, or the browser
        # treats this as a different cookie and keeps the original.
        assert attributes["path"] == "/"
        assert attributes["samesite"].lower() == "strict"
        assert attributes["httponly"]
        assert SESSION_COOKIE not in client.cookies

    def test_it_ends_only_that_session(
        self, client: TestClient, users: UserStore, sessions: SessionStore
    ) -> None:
        elsewhere, _ = sessions.create(users.get("roman"))
        client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        client.delete(LOGIN)

        assert sessions.resolve(elsewhere) is not None


class TestGuessingPasswords:
    def test_attempts_are_counted_and_then_refused(self, relay_service: RelayService) -> None:
        with _hub_with(relay_service, auth_max_failures=3) as client:
            for _ in range(3):
                wrong = client.post(LOGIN, json={"username": "roman", "password": "wrong-guess-1"})
                assert wrong.status_code == HTTPStatus.UNAUTHORIZED

            blocked = client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        assert blocked.status_code == HTTPStatus.TOO_MANY_REQUESTS
        assert blocked.json() == {"detail": "Too many failed authentication attempts"}

    def test_a_successful_login_clears_the_count(self, relay_service: RelayService) -> None:
        with _hub_with(relay_service, auth_max_failures=3) as client:
            for _ in range(2):
                client.post(LOGIN, json={"username": "roman", "password": "wrong-guess-1"})

            assert (
                client.post(LOGIN, json={"username": "roman", "password": PASSWORD}).status_code
                == HTTPStatus.CREATED
            )
            for _ in range(2):
                client.post(LOGIN, json={"username": "roman", "password": "wrong-guess-1"})

            # Still under the limit, because the success above reset the count.
            again = client.post(LOGIN, json={"username": "roman", "password": PASSWORD})

        assert again.status_code == HTTPStatus.CREATED

    def test_guessing_passwords_does_not_lock_out_the_relay_key(
        self, relay_service: RelayService
    ) -> None:
        """Its own bucket, so a browser at the login form cannot shut out the firmware."""
        with _hub_with(relay_service, auth_max_failures=2) as client:
            for _ in range(5):
                client.post(LOGIN, json={"username": "roman", "password": "wrong-guess-1"})

            still_works = client.get("/v1/relays", headers=RELAY_HEADERS)

        assert still_works.status_code == HTTPStatus.OK
