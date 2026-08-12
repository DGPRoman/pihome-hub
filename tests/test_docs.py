"""Guards against documentation drifting away from the code it describes.

A troubleshooting guide is only worth having if the messages and numbers in it are
still the ones the service produces. Prose cannot be type-checked, so the parts that
are machine-checkable are checked here: the paths it points at, and every constant it
quotes at the reader as a fact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pihome_hub.__main__ import EXIT_CONFIGURATION_ERROR
from pihome_hub.config import MIN_API_KEY_LENGTH, Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
TROUBLESHOOTING = REPO_ROOT / "docs" / "troubleshooting.md"


@pytest.fixture(scope="module")
def guide() -> str:
    return TROUBLESHOOTING.read_text()


@pytest.fixture(scope="module")
def readme() -> str:
    return (REPO_ROOT / "README.md").read_text()


class TestTheGuideIsReachable:
    def test_the_readme_links_to_it(self, readme: str) -> None:
        """An unlinked guide is one nobody in trouble will find."""
        assert "docs/troubleshooting.md" in readme


class TestEveryPathItPointsAtExists:
    def test_no_backticked_repository_path_is_stale(self, guide: str) -> None:
        # Only paths with a directory component, and only relative ones: an absolute
        # path in this guide is on the Pi (/etc/pihome-hub) or in /dev, not in here.
        referenced = {
            match
            for match in re.findall(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+)`", guide)
            if not match.startswith("/")
        }
        assert referenced, "path extraction found nothing — this test is out of date"

        missing = sorted(path for path in referenced if not (REPO_ROOT / path).exists())
        assert not missing, f"the guide points at paths that do not exist: {missing}"


class TestEveryNumberItQuotesIsStillTrue:
    def test_the_configuration_exit_code(self, guide: str) -> None:
        codes = re.findall(r"^\| `(\d+)` \| Configuration is wrong", guide, re.MULTILINE)
        assert codes == [str(EXIT_CONFIGURATION_ERROR)]

    def test_the_unit_still_refuses_to_restart_on_that_code(self, guide: str) -> None:
        """The guide's promise that systemd leaves a bad config alone is the unit's job."""
        unit = (REPO_ROOT / "deploy" / "pihome-hub.service").read_text()
        assert f"RestartPreventExitStatus={EXIT_CONFIGURATION_ERROR}" in unit
        assert f"`RestartPreventExitStatus={EXIT_CONFIGURATION_ERROR}`" in guide

    def test_the_minimum_key_length(self, guide: str) -> None:
        quoted = re.search(r"at least (\d+) characters", guide)
        assert quoted is not None
        assert int(quoted.group(1)) == MIN_API_KEY_LENGTH

    def test_the_port_in_every_example(self, guide: str) -> None:
        ports = {int(port) for port in re.findall(r"127\.0\.0\.1:(\d+)", guide)}
        assert ports == {Settings.model_fields["port"].default}

    def test_the_authentication_failure_window(self, guide: str) -> None:
        quoted = re.search(r"\((\d+) by default\)", guide)
        assert quoted is not None
        window = Settings.model_fields["auth_failure_window_seconds"].default
        assert int(quoted.group(1)) == window
