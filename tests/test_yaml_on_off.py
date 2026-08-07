"""YAML 1.1 resolves bare on/off to booleans, and this config is full of on/off fields.

Without accommodation, `state: on` reaches pydantic as `True` and fails validation
with a message about a type the author never typed. Every affected field is checked
here in both spellings, because the trap is silent, repeatable, and lands on whoever
is wiring up their house for the first time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pihome_hub.automation import load_automation
from pihome_hub.relays import load_relays
from pihome_hub.sensors import load_sensors


def test_yaml_really_does_this() -> None:
    """The premise, asserted rather than assumed."""
    parsed = yaml.safe_load("a: on\nb: off\nc: 'on'\n")
    assert parsed == {"a": True, "b": False, "c": "on"}


class TestRelayConfig:
    @pytest.mark.parametrize("spelling", ["on", '"on"', "'on'"])
    def test_initial_state_on(self, tmp_path: Path, spelling: str) -> None:
        path = tmp_path / "relays.yaml"
        path.write_text(
            f'relays:\n  - id: porch-light\n    pin: 17\n    label: "P"\n'
            f"    initial_state: {spelling}\n",
            encoding="utf-8",
        )
        assert load_relays(path)[0].initial_state == "on"

    @pytest.mark.parametrize("spelling", ["off", '"off"'])
    def test_shutdown_state_off(self, tmp_path: Path, spelling: str) -> None:
        path = tmp_path / "relays.yaml"
        path.write_text(
            f'relays:\n  - id: porch-light\n    pin: 17\n    label: "P"\n'
            f"    shutdown_state: {spelling}\n",
            encoding="utf-8",
        )
        assert load_relays(path)[0].shutdown_state == "off"

    def test_unquoted_words_are_unaffected(self, tmp_path: Path) -> None:
        path = tmp_path / "relays.yaml"
        path.write_text(
            'relays:\n  - id: porch-light\n    pin: 17\n    label: "P"\n'
            "    initial_state: preserve\n    shutdown_state: leave\n",
            encoding="utf-8",
        )
        relay = load_relays(path)[0]
        assert (relay.initial_state, relay.shutdown_state) == ("preserve", "leave")


class TestAutomationConfig:
    @pytest.mark.parametrize(("spelling", "expected"), [("on", "on"), ("off", "off")])
    def test_action_state(self, tmp_path: Path, spelling: str, expected: str) -> None:
        path = tmp_path / "automation.yaml"
        path.write_text(
            "rules:\n"
            "  - id: porch-motion-light\n"
            "    when:\n"
            "      device: porch-motion\n"
            "      motion: true\n"
            "    then:\n"
            "      relay: porch-light\n"
            f"      state: {spelling}\n",
            encoding="utf-8",
        )
        assert load_automation(path).rules[0].then.state == expected


#: The suite runs from a temporary directory, so example files need an absolute path.
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class TestShippedExamples:
    """The example files are the first thing anyone copies; they must load."""

    def test_relays_example_loads(self) -> None:
        relays = load_relays(CONFIG_DIR / "relays.example.yaml")
        assert [relay.id for relay in relays] == ["porch-light", "gate-light"]

    def test_automation_example_loads(self) -> None:
        config = load_automation(CONFIG_DIR / "automation.example.yaml")
        assert [rule.id for rule in config.rules] == [
            "porch-motion-light",
            "gate-motion-light",
        ]
        assert config.location is not None
        assert all(rule.then.state == "on" for rule in config.rules)

    def test_sensors_example_loads(self) -> None:
        devices = load_sensors(CONFIG_DIR / "sensors.example.yaml")
        assert [device.id for device in devices] == ["porch-motion", "gate-camera"]

    def test_the_examples_are_mutually_consistent(self) -> None:
        """Every id an automation rule names must exist in the other two files."""
        relays = {relay.id for relay in load_relays(CONFIG_DIR / "relays.example.yaml")}
        devices = {device.id for device in load_sensors(CONFIG_DIR / "sensors.example.yaml")}

        for rule in load_automation(CONFIG_DIR / "automation.example.yaml").rules:
            assert rule.then.relay in relays, f"{rule.id} targets an undefined relay"
            assert rule.when.device in devices, f"{rule.id} triggers on an undefined device"
