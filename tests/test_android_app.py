"""The Android app, offered to phones joining by invitation, and how it gets there."""

from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pihome_hub.admin import main
from pihome_hub.android import APK_MEDIA_TYPE, AndroidApp, NotAnApkError, check_apk, install_apk
from pihome_hub.app import create_app
from pihome_hub.relays import RelayService
from pihome_hub.storage import prepare_database
from tests.conftest import build_settings

#: What a browser sends when it is navigating, as opposed to fetching a subresource.
NAVIGATION = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


def make_apk(path: Path, code: bytes = b"dex\n035\0") -> Path:
    """A file shaped like an APK: a zip with a manifest and code. Not a real app."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00")
        archive.writestr("classes.dex", code)
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def apk_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "pihome.apk"


def _client(relay_service: RelayService, **overrides: object) -> Iterator[TestClient]:
    settings = build_settings(**overrides)
    prepare_database(settings.database_path)
    with TestClient(create_app(settings, relay_service=relay_service)) as client:
        yield client


@pytest.fixture
def hub(apk_path: Path, relay_service: RelayService) -> Iterator[TestClient]:
    yield from _client(relay_service, android_app_path=apk_path)


class TestNothingIsOfferedUntilAnAppIsInstalled:
    def test_both_routes_are_a_json_404(self, hub: TestClient) -> None:
        for path in ("/app/android.json", "/app/pihome.apk"):
            response = hub.get(path)
            assert response.status_code == HTTPStatus.NOT_FOUND, path
            assert "pihome-hub-admin app install" in response.json()["detail"], path


class TestAnInstalledAppIsOffered:
    @pytest.fixture(autouse=True)
    def _installed(self, tmp_path: Path, apk_path: Path) -> None:
        install_apk(make_apk(tmp_path / "release.apk"), apk_path)

    def test_its_description_names_the_file_a_phone_will_get(
        self, hub: TestClient, apk_path: Path
    ) -> None:
        response = hub.get("/app/android.json")

        assert response.status_code == HTTPStatus.OK
        assert response.json() == {"sha256": sha256(apk_path), "size": apk_path.stat().st_size}

    def test_it_downloads_as_an_apk_with_no_credentials(
        self, hub: TestClient, apk_path: Path
    ) -> None:
        response = hub.get("/app/pihome.apk")

        assert response.status_code == HTTPStatus.OK
        assert response.headers["content-type"] == APK_MEDIA_TYPE
        assert 'filename="pihome.apk"' in response.headers["content-disposition"]
        assert response.headers["cache-control"] == "no-cache"
        assert "etag" in response.headers
        assert response.content == apk_path.read_bytes()

    def test_a_replaced_app_is_described_as_the_new_one(
        self, hub: TestClient, tmp_path: Path, apk_path: Path
    ) -> None:
        before = hub.get("/app/android.json").json()["sha256"]

        install_apk(make_apk(tmp_path / "next.apk", code=b"dex\n035\0next"), apk_path)

        after = hub.get("/app/android.json").json()["sha256"]
        assert after != before
        assert after == sha256(apk_path)
        assert hub.get("/app/pihome.apk").content == apk_path.read_bytes()


class TestTheWebClientDoesNotShadowIt:
    @pytest.fixture
    def web(
        self, tmp_path: Path, apk_path: Path, relay_service: RelayService
    ) -> Iterator[TestClient]:
        bundle = tmp_path / "dist"
        bundle.mkdir()
        (bundle / "index.html").write_text("<!doctype html><title>pihome</title>")
        yield from _client(relay_service, android_app_path=apk_path, web_root=bundle)

    def test_no_app_is_a_json_404_even_to_a_browser_navigating(self, web: TestClient) -> None:
        response = web.get("/app/pihome.apk", headers=NAVIGATION)

        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.headers["content-type"] == "application/json"

    def test_an_installed_app_is_the_app(
        self, web: TestClient, tmp_path: Path, apk_path: Path
    ) -> None:
        install_apk(make_apk(tmp_path / "release.apk"), apk_path)

        response = web.get("/app/pihome.apk", headers=NAVIGATION)

        assert response.status_code == HTTPStatus.OK
        assert response.headers["content-type"] == APK_MEDIA_TYPE


class TestOnlyAnApkIsAccepted:
    def test_a_file_that_is_not_a_zip(self, tmp_path: Path) -> None:
        text = tmp_path / "notes.apk"
        text.write_text("not an app")

        with pytest.raises(NotAnApkError, match="not even a zip"):
            check_apk(text)

    def test_a_zip_without_a_manifest_or_code(self, tmp_path: Path) -> None:
        archive = tmp_path / "photos.apk"
        with zipfile.ZipFile(archive, "w") as writing:
            writing.writestr("photo.jpg", b"\xff\xd8")

        with pytest.raises(NotAnApkError, match=r"AndroidManifest\.xml, classes\.dex"):
            check_apk(archive)

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(NotAnApkError, match="not a file"):
            check_apk(tmp_path / "nowhere.apk")

    def test_a_refused_file_leaves_the_installed_one_as_it_was(
        self, tmp_path: Path, apk_path: Path
    ) -> None:
        install_apk(make_apk(tmp_path / "release.apk"), apk_path)
        before = apk_path.read_bytes()
        wrong = tmp_path / "wrong.apk"
        wrong.write_text("oops")

        with pytest.raises(NotAnApkError):
            install_apk(wrong, apk_path)

        assert apk_path.read_bytes() == before
        assert [path.name for path in apk_path.parent.iterdir()] == ["pihome.apk"]


class TestTheChecksumIsWorkedOutOncePerFile:
    def test_an_unchanged_file_is_not_hashed_again(
        self, tmp_path: Path, apk_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_apk(make_apk(tmp_path / "release.apk"), apk_path)
        app = AndroidApp(apk_path)
        first = app.describe()
        monkeypatch.setattr(hashlib, "sha256", lambda: pytest.fail("hashed a second time"))

        assert app.describe() == first


class TestTheAdminCommand:
    def run(self, apk_path: Path, *argv: str) -> int:
        return main(["app", "--path", str(apk_path), *argv])

    def test_install_show_and_remove(
        self, tmp_path: Path, apk_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        release = make_apk(tmp_path / "release.apk")

        assert self.run(apk_path, "install", str(release)) == 0
        assert apk_path.read_bytes() == release.read_bytes()
        assert sha256(release) in capsys.readouterr().out

        assert self.run(apk_path, "show") == 0
        assert sha256(release) in capsys.readouterr().out

        assert self.run(apk_path, "remove") == 0
        assert not apk_path.exists()
        assert self.run(apk_path, "show") == 0
        assert "no app installed" in capsys.readouterr().out

    def test_a_file_that_is_not_an_apk_is_one_line_and_a_failure(
        self, tmp_path: Path, apk_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        wrong = tmp_path / "notes.txt"
        wrong.write_text("not an app")

        assert self.run(apk_path, "install", str(wrong)) == 1

        error = capsys.readouterr().err
        assert "is not an APK" in error
        assert "Traceback" not in error
        assert not apk_path.exists()

    def test_it_opens_no_database(
        self, tmp_path: Path, apk_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        database = tmp_path / "hub.db"
        monkeypatch.setenv("PIHOME_DATABASE_PATH", str(database))
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))

        assert self.run(apk_path, "install", str(make_apk(tmp_path / "release.apk"))) == 0
        assert not database.exists()

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="needs a state directory that does not belong to root"
    )
    def test_root_is_refused_and_told_which_account_to_use(
        self,
        tmp_path: Path,
        apk_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        apk_path.parent.mkdir(parents=True)
        monkeypatch.setattr(os, "geteuid", lambda: 0)

        assert self.run(apk_path, "install", str(make_apk(tmp_path / "release.apk"))) == 1

        error = capsys.readouterr().err
        assert "only root can replace" in error
        assert "sudo -u" in error
        assert not apk_path.exists()
