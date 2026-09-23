"""What this hub is willing to be told about a device.

The address is the value under test here more than any other. It is the one thing
an outside party chooses that this process then makes an authenticated request to,
so each case below is a way of pointing it somewhere it should not go.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from pihome_hub.devices import (
    Device,
    DeviceAnnouncement,
    DeviceKind,
    DeviceStatus,
    normalise_address,
)


class TestAnAddressIsReducedToOneSpelling:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("http://10.0.0.5", "http://10.0.0.5"),
            ("http://10.0.0.5/", "http://10.0.0.5"),
            ("http://10.0.0.5:80", "http://10.0.0.5"),
            ("http://10.0.0.5:80/", "http://10.0.0.5"),
            ("  http://10.0.0.5  ", "http://10.0.0.5"),
            ("http://192.168.1.50:8080", "http://192.168.1.50:8080"),
            ("http://127.0.0.1:5002", "http://127.0.0.1:5002"),
            ("http://[fd00::1]", "http://[fd00::1]"),
            ("http://[fd00::1]:8080", "http://[fd00::1]:8080"),
            ("http://[fe80::1]", "http://[fe80::1]"),
        ],
    )
    def test_equivalent_forms_normalise_together(self, written: str, expected: str) -> None:
        assert normalise_address(written) == expected

    def test_the_same_device_written_two_ways_is_one_address(self) -> None:
        """Which is what stops a reannounce looking like a move."""
        assert normalise_address("http://10.0.0.5:80/") == normalise_address("http://10.0.0.5")


class TestAnAddressThisHubWillNotCall:
    @pytest.mark.parametrize(
        ("written", "because"),
        [
            ("https://10.0.0.5", "no certificate anybody could check"),
            ("ftp://10.0.0.5", "not http"),
            ("file:///etc/passwd", "not http"),
            ("http://example.invalid", "a name is resolved by somebody else"),
            ("http://localhost", "a name, even this one"),
            ("http://8.8.8.8", "not on a private network"),
            ("http://[2001:4860::1]", "not on a private network"),
            ("http://10.0.0.5/v1/power", "the path belongs to the kind"),
            ("http://10.0.0.5/?x=1", "no query"),
            ("http://10.0.0.5#frag", "no fragment"),
            ("http://user:pw@10.0.0.5", "credentials do not travel in a URL"),
            ("http://10.0.0.5:not-a-port", "not a URL at all"),
            ("", "empty"),
            ("   ", "empty once stripped"),
        ],
    )
    def test_it_is_refused(self, written: str, because: str) -> None:
        with pytest.raises(ValueError, match=r".+"):
            normalise_address(written)

    def test_a_public_address_says_so(self) -> None:
        """The message is what an operator reads when their firmware is wrong."""
        with pytest.raises(ValueError, match="private network"):
            normalise_address("http://8.8.8.8")

    def test_a_hostname_explains_itself(self) -> None:
        with pytest.raises(ValueError, match="rather than a hostname"):
            normalise_address("http://pc.local")


class TestADeclaredDevice:
    def test_the_kind_decides_where_status_is_read(self) -> None:
        device = Device(id="workshop-pc", label="Workshop PC", kind=DeviceKind.PC_POWER)
        assert device.status_path == "/v1/power"

    @pytest.mark.parametrize("bad", ["Workshop-PC", "workshop_pc", "-pc", "pc-", "pc--power", ""])
    def test_an_id_that_is_not_a_slug_is_refused(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            Device(id=bad, label="PC", kind=DeviceKind.PC_POWER)

    def test_an_unknown_kind_is_refused(self) -> None:
        """A kind this build cannot talk to is configuration to fix, not to ignore."""
        with pytest.raises(ValidationError):
            Device.model_validate({"id": "pc", "label": "PC", "kind": "toaster"})

    def test_an_unexpected_field_is_refused(self) -> None:
        """A misspelled key in YAML is a setting silently not applied."""
        with pytest.raises(ValidationError):
            Device.model_validate(
                {"id": "pc", "label": "PC", "kind": "pc-power", "poll_seconds": 5}
            )


class TestAnAnnouncement:
    def test_it_carries_an_address_a_key_and_nothing_required_beyond_them(self) -> None:
        announcement = DeviceAnnouncement(address="http://10.0.0.5/", api_key=SecretStr("k" * 32))
        assert announcement.address == "http://10.0.0.5"
        assert announcement.firmware is None

    def test_the_key_is_a_secret(self) -> None:
        """So it stays out of a traceback, a log line and a repr()."""
        announcement = DeviceAnnouncement(address="http://10.0.0.5", api_key=SecretStr("k" * 32))
        assert "k" * 32 not in repr(announcement)
        assert announcement.api_key.get_secret_value() == "k" * 32

    @pytest.mark.parametrize("length", [0, 7, 513])
    def test_a_key_that_cannot_be_one_is_refused(self, length: int) -> None:
        with pytest.raises(ValidationError):
            DeviceAnnouncement(address="http://10.0.0.5", api_key=SecretStr("k" * length))

    def test_a_firmware_string_with_a_newline_in_it_is_refused(self) -> None:
        """The value is chosen by the device and ends up in a log line."""
        with pytest.raises(ValidationError):
            DeviceAnnouncement(
                address="http://10.0.0.5",
                api_key=SecretStr("k" * 32),
                firmware="0.1.0\nlevel=INFO fake",
            )

    def test_an_unexpected_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            DeviceAnnouncement.model_validate(
                {"address": "http://10.0.0.5", "api_key": "k" * 32, "kind": "pc-power"}
            )


class TestWhatIsServedHoldsNoCredential:
    def test_status_has_no_field_that_could_carry_the_key(self) -> None:
        """The registry holds the announced key; this is the shape that leaves.

        Asserted on the model rather than on one response, because a field added
        here would be served by every route at once.
        """
        assert "api_key" not in DeviceStatus.model_fields
        assert not any("key" in name for name in DeviceStatus.model_fields)
