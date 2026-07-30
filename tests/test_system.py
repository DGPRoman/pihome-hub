"""Operational endpoints and the documentation toggle."""

from __future__ import annotations

from http import HTTPStatus

from fastapi.testclient import TestClient

from pihome_hub import __version__
from pihome_hub.app import create_app
from tests.conftest import build_settings


class TestHealth:
    def test_reports_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"status": "ok"}

    def test_needs_no_credentials(self, client: TestClient) -> None:
        assert client.get("/health").status_code == HTTPStatus.OK

    def test_leaks_no_build_detail(self, client: TestClient) -> None:
        """An unauthenticated probe should not help fingerprint the deployment."""
        body = client.get("/health").text
        assert __version__ not in body
        assert "pihome" not in body.casefold()


class TestDocumentationToggle:
    def test_schema_is_absent_by_default(self, client: TestClient) -> None:
        assert client.get("/openapi.json").status_code == HTTPStatus.NOT_FOUND
        assert client.get("/docs").status_code == HTTPStatus.NOT_FOUND

    def test_schema_is_served_when_enabled(self) -> None:
        app = create_app(build_settings(docs_enabled=True))
        with TestClient(app) as client:
            schema = client.get("/openapi.json")
            assert schema.status_code == HTTPStatus.OK
            assert schema.json()["info"]["version"] == __version__
            assert client.get("/docs").status_code == HTTPStatus.OK


class TestRouting:
    def test_unknown_path_is_a_plain_404(self, client: TestClient) -> None:
        assert client.get("/does-not-exist").status_code == HTTPStatus.NOT_FOUND

    def test_no_v1_routes_are_registered_yet(self, app_paths: set[str]) -> None:
        """Guards the phase boundary: /v1 arrives with authentication, not before."""
        assert not any(path.startswith("/v1") for path in app_paths)
