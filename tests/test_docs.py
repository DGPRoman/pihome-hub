"""Guards against documentation drifting away from the code it describes.

Documentation is only worth having if what it states is still true — a quoted error
message, a path, a constant, the name of a test it points at as proof. Prose cannot be
type-checked, so everything in it that is machine-checkable is checked here, and every
document under ``docs/`` is swept without having to be listed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pihome_hub.__main__ import EXIT_CONFIGURATION_ERROR
from pihome_hub.config import MIN_API_KEY_LENGTH, Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
TROUBLESHOOTING = DOCS / "troubleshooting.md"

#: Every document under docs/, so a new one is covered without being listed here.
_DOC_NAMES = sorted(path.name for path in DOCS.glob("*.md"))


@pytest.fixture(scope="module")
def guide() -> str:
    return TROUBLESHOOTING.read_text()


@pytest.fixture(scope="module")
def readme() -> str:
    return (REPO_ROOT / "README.md").read_text()


class TestEveryDocumentIsReachable:
    @pytest.mark.parametrize("name", _DOC_NAMES)
    def test_the_readme_links_to_it(self, name: str, readme: str) -> None:
        """An unlinked document is one nobody will find when they need it."""
        assert f"docs/{name}" in readme


class TestEveryPathTheDocsPointAtExists:
    @pytest.mark.parametrize("name", _DOC_NAMES)
    def test_no_relative_link_or_backticked_path_is_stale(self, name: str) -> None:
        text = (DOCS / name).read_text()

        # Markdown links are relative to docs/; backticked paths are written from the
        # repository root. Absolute ones live on the Pi (/etc, /dev) and are skipped.
        links = {(DOCS / target) for target in re.findall(r"\]\((\.\.?/[^)#]+)", text)}
        quoted = {
            (REPO_ROOT / match)
            for match in re.findall(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*/[A-Za-z0-9_./-]+)`", text)
            if not match.startswith("/")
        }
        assert links | quoted, f"{name}: path extraction found nothing — this test is out of date"

        missing = sorted(str(path) for path in links | quoted if not path.exists())
        assert not missing, f"{name} points at paths that do not exist: {missing}"


class TestTheArchitectureNotesCiteRealTests:
    def test_every_test_class_it_names_exists(self) -> None:
        """Naming a guard that has been renamed away is worse than naming none."""
        text = (DOCS / "architecture.md").read_text()
        cited = set(re.findall(r"`(Test[A-Za-z0-9_]+)`", text))
        assert cited, "no test class is cited — this test is out of date"

        defined = {
            name
            for source in (REPO_ROOT / "tests").rglob("*.py")
            for name in re.findall(r"^class (Test[A-Za-z0-9_]+)", source.read_text(), re.MULTILINE)
        }
        assert cited <= defined, f"cited but not defined: {sorted(cited - defined)}"


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
