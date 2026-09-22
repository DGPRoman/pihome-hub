"""Serving the browser client's built bundle, and not serving anything else.

The bundle is mounted at ``/`` and answers every path the API did not, which is the
arrangement that makes a client-side route survive a refresh — and the arrangement
that could quietly swallow the API's own 404s. Most of what is below is about the
second half of that sentence.
"""

from __future__ import annotations

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pihome_hub.app import check_configuration, create_app
from pihome_hub.config import Settings
from pihome_hub.relays import RelayService
from pihome_hub.storage import prepare_database
from pihome_hub.web import WebClientError, hub_segments
from tests.conftest import RELAY_HEADERS, build_settings

#: What a browser sends when it is navigating, as opposed to fetching a subresource.
NAVIGATION = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

INDEX_BODY = "<!doctype html><title>pihome</title><script src=/assets/app.js></script>"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A directory shaped like what the web client's build writes."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_BODY)
    (root / "assets" / "app.js").write_text("console.log('pihome')\n")
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    return root


@pytest.fixture
def web_settings(bundle: Path) -> Settings:
    return build_settings(web_root=bundle)


@pytest.fixture
def web(web_settings: Settings, relay_service: RelayService) -> Iterator[TestClient]:
    prepare_database(web_settings.database_path)
    with TestClient(create_app(web_settings, relay_service=relay_service)) as client:
        yield client


class TestTheBundleIsServed:
    def test_the_root_is_the_index(self, web: TestClient) -> None:
        response = web.get("/", headers=NAVIGATION)

        assert response.status_code == HTTPStatus.OK
        assert response.text == INDEX_BODY
        assert response.headers["content-type"].startswith("text/html")

    def test_a_built_asset_is_served_as_itself(self, web: TestClient) -> None:
        response = web.get("/assets/app.js")

        assert response.status_code == HTTPStatus.OK
        assert "console.log" in response.text
        assert "javascript" in response.headers["content-type"]

    def test_a_client_side_route_survives_a_refresh(self, web: TestClient) -> None:
        """The point of the fallback: /relays exists only once the bundle is running."""
        response = web.get("/relays", headers=NAVIGATION)

        assert response.status_code == HTTPStatus.OK
        assert response.text == INDEX_BODY

    def test_a_nested_client_side_route_too(self, web: TestClient) -> None:
        assert web.get("/rules/porch-at-dusk", headers=NAVIGATION).text == INDEX_BODY

    def test_a_missing_asset_stays_missing(self, web: TestClient) -> None:
        """A subresource asks for no HTML, so it is not a route — it is a 404.

        Answering it with index.html would hand the browser a document labelled as
        JavaScript, and the error it then reports would be about syntax rather than
        about the file being absent.
        """
        response = web.get("/assets/index-C7kKkJch.js", headers={"Accept": "*/*"})

        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_a_method_the_bundle_cannot_answer_is_405(self, web: TestClient) -> None:
        assert web.post("/relays", headers=NAVIGATION).status_code == HTTPStatus.METHOD_NOT_ALLOWED

    def test_nothing_outside_the_bundle_is_reachable(self, web: TestClient, bundle: Path) -> None:
        """Percent-encoded, so the escape reaches the server rather than being folded
        away by the client. It arrives as ``/../hub.env`` and is refused as a file,
        which leaves it looking like any other client-side route."""
        (bundle.parent / "hub.env").write_text("PIHOME_RELAY_API_KEY=not-this-one\n")

        response = web.get("/%2e%2e/hub.env", headers=NAVIGATION)

        assert "not-this-one" not in response.text
        assert response.text == INDEX_BODY


class TestTheBundleDoesNotShadowTheApi:
    def test_an_unknown_versioned_path_is_still_a_json_404(self, web: TestClient) -> None:
        """The acceptance criterion. A mount at / matches everything, so without the
        reserved check every mistyped /v1 path would come back as the front page."""
        response = web.get("/v1/relayz", headers=NAVIGATION)

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.headers["content-type"].startswith("application/json")
        assert response.text != INDEX_BODY

    def test_an_unauthenticated_call_is_still_401(self, web: TestClient) -> None:
        assert web.get("/v1/relays", headers=NAVIGATION).status_code == HTTPStatus.UNAUTHORIZED

    def test_an_authenticated_call_still_answers(self, web: TestClient) -> None:
        response = web.get("/v1/relays", headers={**NAVIGATION, **RELAY_HEADERS})

        assert response.status_code == HTTPStatus.OK
        assert [relay["id"] for relay in response.json()["relays"]] == ["porch-light", "gate-light"]

    def test_health_still_answers(self, web: TestClient) -> None:
        response = web.get("/health", headers=NAVIGATION)

        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"status": "ok"}

    def test_the_reserved_segments_come_from_the_router(
        self, web_settings: Settings, relay_service: RelayService
    ) -> None:
        """Not from a list somebody has to remember to extend."""
        app = create_app(web_settings, relay_service=relay_service)

        assert {"v1", "health"} <= hub_segments(app)

    def test_the_documentation_routes_are_reserved_when_they_exist(
        self, bundle: Path, relay_service: RelayService
    ) -> None:
        """They are not routers, so they are the other shape app.routes can hold."""
        settings = build_settings(web_root=bundle, docs_enabled=True)
        app = create_app(settings, relay_service=relay_service)

        assert {"docs", "openapi.json"} <= hub_segments(app)


class TestItIsOffUnlessAskedFor:
    def test_no_web_root_serves_no_files(self, client: TestClient) -> None:
        assert client.get("/", headers=NAVIGATION).status_code == HTTPStatus.NOT_FOUND

    def test_the_default_is_unset(self) -> None:
        assert build_settings().web_root is None


class TestAMissingBundleIsReportedAtStartup:
    def test_a_path_that_is_not_a_directory_is_refused(
        self, tmp_path: Path, relay_service: RelayService
    ) -> None:
        settings = build_settings(web_root=tmp_path / "never-built")

        with pytest.raises(WebClientError, match="is not a directory"):
            create_app(settings, relay_service=relay_service)

    def test_a_directory_without_an_index_is_refused(
        self, tmp_path: Path, relay_service: RelayService
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        settings = build_settings(web_root=empty)

        with pytest.raises(WebClientError, match=r"index\.html"):
            create_app(settings, relay_service=relay_service)

    def test_the_startup_check_finds_it_before_the_factory_does(
        self, tmp_path: Path, relay_service: RelayService
    ) -> None:
        """__main__ calls check_configuration first, and turns this into exit 2 with
        one readable line rather than a traceback out of uvicorn."""
        settings = build_settings(web_root=tmp_path / "never-built")

        with pytest.raises(WebClientError, match="PIHOME_WEB_ROOT"):
            check_configuration(settings, relay_service)

    def test_a_configured_bundle_passes_the_startup_check(
        self, web_settings: Settings, relay_service: RelayService
    ) -> None:
        check_configuration(web_settings, relay_service)
