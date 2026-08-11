"""Guards that keep the systemd unit agreeing with the code it starts.

The suite cannot start a unit, so what is checked here is the small set of places
where the unit restates a fact that lives in Python — the exit code it must not
retry, the console script it runs — plus two sandbox options that would break relays
without breaking anything a test would otherwise notice. Each of these is easy to
change on one side only, and the failure would surface on a Raspberry Pi.
"""

from __future__ import annotations

import tomllib
from configparser import ConfigParser
from pathlib import Path

import pytest

from pihome_hub.__main__ import EXIT_CONFIGURATION_ERROR

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_PATH = REPO_ROOT / "deploy" / "pihome-hub.service"

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
