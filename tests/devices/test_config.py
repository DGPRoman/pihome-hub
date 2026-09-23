"""Reading config/devices.yaml, and refusing the ways it can be wrong."""

from __future__ import annotations

from pathlib import Path

import pytest

from pihome_hub.devices import DeviceConfigError, DeviceKind, load_devices


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestAFileThatIsNotThere:
    def test_no_file_means_no_devices(self, tmp_path: Path) -> None:
        """A deployment that only switches relays is an ordinary one."""
        assert load_devices(tmp_path / "devices.yaml") == []

    def test_an_empty_file_means_no_devices(self, tmp_path: Path) -> None:
        assert load_devices(write(tmp_path / "devices.yaml", "")) == []


class TestAFileThatIsThere:
    def test_devices_are_read_in_the_order_they_are_written(self, tmp_path: Path) -> None:
        path = write(
            tmp_path / "devices.yaml",
            """
            devices:
              - id: workshop-pc
                label: "Workshop PC"
                kind: pc-power
              - id: study-pc
                label: "Study PC"
                kind: pc-power
            """,
        )
        devices = load_devices(path)

        assert [device.id for device in devices] == ["workshop-pc", "study-pc"]
        assert devices[0].kind is DeviceKind.PC_POWER
        assert devices[0].label == "Workshop PC"

    def test_the_example_file_is_one_this_build_can_read(self) -> None:
        """Shipping an example that does not load is worse than shipping none."""
        example = Path(__file__).resolve().parents[2] / "config" / "devices.example.yaml"
        devices = load_devices(example)

        assert devices, "the example declares nothing — it is no longer an example"


class TestAFileThatIsWrong:
    def test_invalid_yaml_names_the_file(self, tmp_path: Path) -> None:
        path = write(tmp_path / "devices.yaml", "devices: [")
        with pytest.raises(DeviceConfigError, match=str(path)):
            load_devices(path)

    def test_a_document_without_a_devices_list_is_refused(self, tmp_path: Path) -> None:
        path = write(tmp_path / "devices.yaml", "sensors: []")
        with pytest.raises(DeviceConfigError, match="top-level 'devices' list"):
            load_devices(path)

    def test_a_kind_this_build_cannot_talk_to_is_refused(self, tmp_path: Path) -> None:
        path = write(
            tmp_path / "devices.yaml",
            "devices:\n  - {id: kettle, label: Kettle, kind: kettle}\n",
        )
        with pytest.raises(DeviceConfigError, match="invalid device configuration"):
            load_devices(path)

    def test_a_directory_where_a_file_should_be_is_reported_not_raised_raw(
        self, tmp_path: Path
    ) -> None:
        """OSError escaping as itself would exit with the wrong code at startup."""
        directory = tmp_path / "devices.yaml"
        directory.mkdir()
        with pytest.raises(DeviceConfigError, match="could not read"):
            load_devices(directory)
