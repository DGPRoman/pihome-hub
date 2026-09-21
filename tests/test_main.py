"""Entry-point behaviour: an unconfigured service must fail loudly and readably."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from pihome_hub.__main__ import (
    EXIT_CONFIGURATION_ERROR,
    main,
    render_configuration_error,
)
from pihome_hub.config import Settings
from pihome_hub.relays import MockRelayBackend
from tests.conftest import VALID_KEY


def _missing_keys_error() -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        Settings()  # type: ignore[call-arg]  # deliberately omitting required fields
    return caught.value


class TestConfigurationErrorRendering:
    def test_names_the_environment_variables_the_operator_must_set(self) -> None:
        message = render_configuration_error(_missing_keys_error())
        assert "PIHOME_RELAY_API_KEY" in message
        assert "PIHOME_SENSOR_API_KEY" in message

    def test_points_at_the_example_file(self) -> None:
        message = render_configuration_error(_missing_keys_error())
        assert ".env.example" in message

    def test_does_not_include_a_traceback(self) -> None:
        message = render_configuration_error(_missing_keys_error())
        assert "Traceback" not in message
        assert "pydantic_core" not in message

    def test_does_not_echo_a_rejected_secret(self) -> None:
        """A too-short key must not be reflected back into the logs."""
        with pytest.raises(ValidationError) as caught:
            Settings(relay_api_key=SecretStr("short"), sensor_api_key=SecretStr(VALID_KEY))
        assert "short" not in render_configuration_error(caught.value)


PORCH_PIN = 17

#: One relay that starts energised and is meant to end de-energised. Both halves
#: matter: without initial_state the pin is never driven, and without
#: shutdown_state close() has nothing to prove it ran.
RELAYS_YAML = (
    "relays:\n"
    "  - id: porch-light\n"
    "    label: Porch light\n"
    f"    pin: {PORCH_PIN}\n"
    "    initial_state: on\n"
    "    shutdown_state: off\n"
)


@pytest.fixture
def started_backend(monkeypatch: pytest.MonkeyPatch) -> MockRelayBackend:
    """The backend ``main()`` will build, handed back so a test can inspect it.

    ``main()`` constructs its own relay service, which is the point — the pins it
    claims are the ones at issue — so there is no other way to see what became of
    them. Every early-exit test in this module used to assert an exit code and a
    line of stderr, and held no reference to the service at all: a pin left
    claimed was invisible to all of them.
    """
    backend = MockRelayBackend()
    monkeypatch.setattr("pihome_hub.app.create_backend", lambda name: backend)
    return backend


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, automation: str) -> None:
    """Point a runnable service at one relay and the given automation rules."""
    (tmp_path / "relays.yaml").write_text(RELAYS_YAML)
    (tmp_path / "automation.yaml").write_text(automation)

    monkeypatch.setenv("PIHOME_RELAY_API_KEY", VALID_KEY)
    monkeypatch.setenv("PIHOME_SENSOR_API_KEY", VALID_KEY[::-1])
    monkeypatch.setenv("PIHOME_RELAY_CONFIG_PATH", str(tmp_path / "relays.yaml"))
    monkeypatch.setenv("PIHOME_AUTOMATION_CONFIG_PATH", str(tmp_path / "automation.yaml"))

    # Reaching the server means the configuration was accepted. Failing here turns a
    # regression into a failed test rather than a suite that hangs on a bound port.
    monkeypatch.setattr(
        "pihome_hub.__main__.uvicorn.run",
        lambda *args, **kwargs: pytest.fail("the server started on a rejected configuration"),
    )


@dataclass
class ServerRun:
    """What ``main()`` handed uvicorn, and the state of the house while it ran."""

    called: bool = False
    app: object | None = None
    claimed_while_serving: frozenset[int] = field(default_factory=frozenset)
    on_while_serving: bool | None = None


def _configure_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: MockRelayBackend
) -> ServerRun:
    """Configure a service that is *meant* to start, and record the moment it does.

    The counterpart to ``_configure``. Every test in this module was built on that
    helper, which fails outright if the server is reached — so the success path, and
    the ``finally`` that releases the pins after it, could not be executed by any
    test. Deleting that ``finally`` left the whole suite passing.
    """
    (tmp_path / "relays.yaml").write_text(RELAYS_YAML)
    (tmp_path / "automation.yaml").write_text("rules: []\n")

    monkeypatch.setenv("PIHOME_RELAY_API_KEY", VALID_KEY)
    monkeypatch.setenv("PIHOME_SENSOR_API_KEY", VALID_KEY[::-1])
    monkeypatch.setenv("PIHOME_RELAY_CONFIG_PATH", str(tmp_path / "relays.yaml"))
    monkeypatch.setenv("PIHOME_AUTOMATION_CONFIG_PATH", str(tmp_path / "automation.yaml"))
    monkeypatch.setenv("PIHOME_DATABASE_PATH", str(tmp_path / "hub.sqlite3"))

    run = ServerRun()

    def record(app: object, **kwargs: object) -> None:
        run.called = True
        run.app = app
        # Sampled here rather than afterwards: this is the only instant at which the
        # service is supposed to be holding the pins, so it is the only way to tell
        # "released on the way out" from "never claimed".
        run.claimed_while_serving = backend.claimed
        run.on_while_serving = backend.is_on(PORCH_PIN)

    monkeypatch.setattr("pihome_hub.__main__.uvicorn.run", record)
    return run


class TestMain:
    def test_exits_with_a_configuration_code_when_unconfigured(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        assert "PIHOME_RELAY_API_KEY" in capsys.readouterr().err

    def test_a_rule_naming_something_absent_is_rejected_before_the_server_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Validating rules inside the lifespan exits 3, which the unit retries forever."""
        _configure(
            monkeypatch,
            tmp_path,
            "rules:\n"
            "  - id: porch-motion-light\n"
            "    when: {device: ghost, motion: true}\n"
            "    then: {relay: porch-light, state: on}\n",
        )

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        error = capsys.readouterr().err
        assert "porch-motion-light" in error
        assert "ghost" in error
        assert "Traceback" not in error

    def test_a_malformed_automation_file_is_rejected_before_the_server_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _configure(monkeypatch, tmp_path, "rules: [{id: broken}]\n")

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        assert "Traceback" not in capsys.readouterr().err


class TestThePinsAreAlwaysReleased:
    """Whatever main() claims, main() gives back.

    The pins are claimed by ``build_relay_service`` and each relay is driven to its
    ``initial_state``. Only the ``try/finally`` around ``uvicorn.run`` released them,
    so every path that exited before the server started left the circuit energised
    and ``shutdown_state`` unapplied. A configuration error does not heal itself, so
    a restarting unit repeated that forever.
    """

    def test_the_success_path_claims_the_pin_and_gives_it_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, started_backend: MockRelayBackend
    ) -> None:
        run = _configure_to_start(monkeypatch, tmp_path, started_backend)

        main()

        assert run.called, "the server was never reached"
        assert run.claimed_while_serving == {PORCH_PIN}, "the pin was not held while serving"
        assert run.on_while_serving is True, "initial_state was not applied"
        assert started_backend.claimed == frozenset(), "the pin was still claimed on the way out"
        assert started_backend.is_on(PORCH_PIN) is False, "shutdown_state was not applied"

    def test_an_unresolvable_rule_still_releases_the_pin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        started_backend: MockRelayBackend,
    ) -> None:
        """check_configuration raises after the pins are claimed."""
        _configure(
            monkeypatch,
            tmp_path,
            "rules:\n"
            "  - id: porch-motion-light\n"
            "    when: {device: ghost, motion: true}\n"
            "    then: {relay: porch-light, state: on}\n",
        )

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        assert "Traceback" not in capsys.readouterr().err
        assert started_backend.claimed == frozenset()
        assert started_backend.is_on(PORCH_PIN) is False, "shutdown_state was not applied"

    def test_an_unknown_timezone_still_releases_the_pin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        started_backend: MockRelayBackend,
    ) -> None:
        """SunClock raised a bare ValueError, which went past every except in main().

        It escaped as a traceback with exit 1 — a code the unit retries — leaving the
        pin claimed and the circuit closed on every attempt.
        """
        _configure(
            monkeypatch,
            tmp_path,
            "location: {latitude: 50.45, longitude: 30.52, timezone: Europe/Nowhere}\n"
            "rules:\n"
            "  - id: porch-motion-light\n"
            "    when: {device: porch-motion, motion: true}\n"
            "    only_after_dark: true\n"
            "    then: {relay: porch-light, state: on}\n",
        )

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        error = capsys.readouterr().err
        assert "Europe/Nowhere" in error
        assert "IANA" in error
        assert "Traceback" not in error
        assert started_backend.claimed == frozenset()
        assert started_backend.is_on(PORCH_PIN) is False

    def test_a_database_that_cannot_be_prepared_still_releases_the_pin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        started_backend: MockRelayBackend,
    ) -> None:
        """prepare_database runs after the pins are claimed, in its own try."""
        _configure(monkeypatch, tmp_path, "rules: []\n")
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("", encoding="utf-8")
        monkeypatch.setenv("PIHOME_DATABASE_PATH", str(blocked / "hub.sqlite3"))

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        assert "Traceback" not in capsys.readouterr().err
        assert started_backend.claimed == frozenset()
        assert started_backend.is_on(PORCH_PIN) is False

    def test_an_unexpected_startup_error_is_not_a_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        started_backend: MockRelayBackend,
    ) -> None:
        """The catch-all, standing in for the next one of these nobody has found yet.

        Two escaped before — a ValueError out of SunClock and a PermissionError out
        of a chmod — and both are fixed at the source. The point of the catch-all is
        that the list of named exceptions is incomplete on purpose, and the cost of
        being wrong about it is a crash loop with the pins held.
        """
        _configure(monkeypatch, tmp_path, "rules: []\n")

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("the sort of thing nobody predicted")

        monkeypatch.setattr("pihome_hub.__main__.prepare_database", explode)

        with pytest.raises(SystemExit) as caught:
            main()

        assert caught.value.code == EXIT_CONFIGURATION_ERROR
        error = capsys.readouterr().err
        assert "RuntimeError" in error
        assert "nobody predicted" in error
        assert "Traceback" not in error
        assert "issues" in error, "an unexpected error should say where to report it"
        assert started_backend.claimed == frozenset()
        assert started_backend.is_on(PORCH_PIN) is False

    def test_a_failure_from_the_running_server_is_not_called_a_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, started_backend: MockRelayBackend
    ) -> None:
        """The catch-all covers startup only.

        Once the server is running, a crash is not the operator's configuration and
        must not be reported as one — but the pins still have to come back.
        """
        _configure_to_start(monkeypatch, tmp_path, started_backend)

        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("the server fell over")

        monkeypatch.setattr("pihome_hub.__main__.uvicorn.run", explode)

        with pytest.raises(RuntimeError, match="fell over"):
            main()

        assert started_backend.claimed == frozenset()
        assert started_backend.is_on(PORCH_PIN) is False
