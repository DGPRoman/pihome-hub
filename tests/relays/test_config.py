"""Loading relay configuration from YAML."""

from __future__ import annotations

from pathlib import Path

import pytest

from pihome_hub.relays import RelayConfigError, load_relays

_VALID_YAML = """
relays:
  - id: porch-light
    pin: 17
    label: "Porch light"
    active_low: true
    initial_state: preserve
  - id: gate-light
    pin: 27
    label: "Gate light"
"""


class TestLoadRelays:
    def test_parses_a_valid_file(self, tmp_path: Path) -> None:
        config_file = tmp_path / "relays.yaml"
        config_file.write_text(_VALID_YAML, encoding="utf-8")

        relays = load_relays(config_file)

        assert [relay.id for relay in relays] == ["porch-light", "gate-light"]
        assert relays[1].active_low is True  # falls back to the model default

    def test_missing_file_raises_relay_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(RelayConfigError, match="could not read"):
            load_relays(tmp_path / "does-not-exist.yaml")

    def test_malformed_yaml_raises_relay_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "relays.yaml"
        config_file.write_text("relays: [this is not: valid: yaml", encoding="utf-8")

        with pytest.raises(RelayConfigError, match="invalid YAML"):
            load_relays(config_file)

    def test_missing_relays_key_raises_relay_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "relays.yaml"
        config_file.write_text("something_else: []", encoding="utf-8")

        with pytest.raises(RelayConfigError, match="top-level 'relays' list"):
            load_relays(config_file)

    def test_schema_violation_raises_relay_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "relays.yaml"
        config_file.write_text(
            "relays:\n  - id: porch-light\n    pin: 999\n    label: x\n", encoding="utf-8"
        )

        with pytest.raises(RelayConfigError, match="invalid relay configuration"):
            load_relays(config_file)

    def test_a_misspelled_key_is_refused_rather_than_ignored(self, tmp_path: Path) -> None:
        """Ignoring it would run the relay at the default polarity, config looking fine."""
        config_file = tmp_path / "relays.yaml"
        config_file.write_text(
            "relays:\n  - id: porch-light\n    pin: 17\n    label: x\n    active-low: false\n",
            encoding="utf-8",
        )

        with pytest.raises(RelayConfigError, match="active-low"):
            load_relays(config_file)
