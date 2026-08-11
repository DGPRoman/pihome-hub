"""Guards that keep ``deploy/`` agreeing with the code it installs and starts.

The suite can neither start a unit nor run the installer, so what is checked here is
the small set of places where those two files restate a fact that lives in Python —
the exit code that must not be retried, the console script, the example config the
installer copies — plus two sandbox options that would break relays without breaking
anything a test would otherwise notice. Each is easy to change on one side only, and
the failure would surface on a Raspberry Pi rather than in CI.
"""

from __future__ import annotations

import re
import tomllib
from configparser import ConfigParser
from pathlib import Path

import pytest

from pihome_hub.__main__ import EXIT_CONFIGURATION_ERROR
from pihome_hub.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO_ROOT / "deploy" / "pihome-hub.service"
INSTALLER_PATH = REPO_ROOT / "deploy" / "install.sh"

#: Options that would tighten the sandbox past what gpiozero can work with, mapped to
#: the values that leave the hardware reachable if one is ever spelled out.
_SAFE_SANDBOX_VALUES = {
    # Hides /dev/gpiochip0.
    "PrivateDevices": {"no", "false", "off", "0"},
    # Hides /proc/device-tree, which gpiozero reads to identify the board.
    "ProcSubset": {"all"},
}


class _UnitParser(ConfigParser):
    """``ConfigParser`` in systemd's dialect."""

    def optionxform(self, optionstr: str) -> str:
        """Preserve case — systemd keys are ``CamelCase`` and case-sensitive."""
        return optionstr


@pytest.fixture(scope="module")
def service() -> dict[str, str]:
    """The unit's ``[Service]`` section."""
    # systemd lets a key repeat, and does not %-interpolate values the way
    # configparser assumes, so both of those defaults are turned off.
    parser = _UnitParser(strict=False, interpolation=None)
    parser.read_string(UNIT_PATH.read_text(encoding="utf-8"))
    return dict(parser["Service"])


@pytest.fixture(scope="module")
def script() -> str:
    """The installer, as text."""
    return INSTALLER_PATH.read_text(encoding="utf-8")


class TestUnitMatchesTheCode:
    def test_a_broken_configuration_is_not_retried(self, service: dict[str, str]) -> None:
        assert str(EXIT_CONFIGURATION_ERROR) in service["RestartPreventExitStatus"].split()

    def test_it_starts_a_console_script_the_package_installs(self, service: dict[str, str]) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        assert Path(service["ExecStart"]).name in pyproject["project"]["scripts"]


class TestSandboxLeavesTheHardwareReachable:
    @pytest.mark.parametrize("option", sorted(_SAFE_SANDBOX_VALUES))
    def test_option_hiding_the_hardware_is_not_enabled(
        self, service: dict[str, str], option: str
    ) -> None:
        value = service.get(option)

        assert value is None or value.casefold() in _SAFE_SANDBOX_VALUES[option]

    def test_a_gpio_character_device_is_allowed_back(self, service: dict[str, str]) -> None:
        assert "gpiochip" in service["DeviceAllow"]


class TestInstallerMatchesTheCode:
    def test_every_example_config_it_copies_exists(self, script: str) -> None:
        referenced = re.findall(r'\$REPO_ROOT/(config/[^"\s]+)', script)

        assert referenced, "the installer no longer copies any example config"
        for relative in referenced:
            assert (REPO_ROOT / relative).is_file()

    def test_its_port_fallback_is_the_configured_default(self, script: str) -> None:
        # Only reached for a hub.env written before the installer existed, which is
        # exactly why nothing would notice the two drifting apart.
        fallback = re.search(r"port:-(\d+)", script)

        assert fallback is not None
        assert int(fallback.group(1)) == Settings.model_fields["port"].default
