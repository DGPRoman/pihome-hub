"""Every model that parses input outside this process must refuse unknown fields.

Ignoring an unrecognised key is the quietest way to be wrong: ``active-low`` for
``active_low`` reads as nothing at all, and the relay runs at the default polarity
with a configuration file that looks correct. This is checked structurally rather
than model by model, so a model added later has to make the decision explicitly.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Final

import pytest
from pydantic import BaseModel

import pihome_hub

#: Models that only ever leave this process. Nothing external is parsed into them,
#: so ``extra`` has nothing to guard: they are built from values already validated.
_OUTPUT_ONLY: Final = {
    "AutomationRuleCollection",
    "DeviceSnapshot",
    "HealthResponse",
    "RelayCollection",
    "RelayState",
    "SensorCollection",
}

#: ``Settings`` reads the environment, where the same argument applies — a misspelled
#: ``PIHOME_AUTH_MAX_FAILUERS`` is accepted in silence today and the hardening it was
#: meant to apply never happens. Left as it is here because tightening it changes how
#: every deployment's ``.env`` is read, which is a change of its own and not this one.
_ENVIRONMENT: Final = {"Settings"}

#: gpiozero lives in the optional ``rpi`` extra, so this module is not importable
#: off a Pi. Named rather than skipped silently, so a second unimportable module
#: fails this test instead of quietly dropping its models from the sweep.
_NOT_IMPORTABLE: Final = {"pihome_hub.relays.gpio"}


def _all_models() -> dict[str, type[BaseModel]]:
    """Every pydantic model defined anywhere in the package."""
    unimportable: set[str] = set()

    for module in pkgutil.walk_packages(pihome_hub.__path__, f"{pihome_hub.__name__}."):
        try:
            importlib.import_module(module.name)
        except ImportError:
            unimportable.add(module.name)

    assert unimportable == _NOT_IMPORTABLE, f"unexpected import failures: {unimportable}"

    found: dict[str, type[BaseModel]] = {}
    stack: list[type[BaseModel]] = [BaseModel]
    while stack:
        for subclass in stack.pop().__subclasses__():
            if subclass.__module__.startswith(pihome_hub.__name__):
                found[subclass.__name__] = subclass
            stack.append(subclass)

    assert found, "model discovery found nothing — this test is out of date"
    return found


_MODELS = _all_models()
_EXEMPT: Final = _OUTPUT_ONLY | _ENVIRONMENT
_INPUT_MODELS = sorted(name for name in _MODELS if name not in _EXEMPT)


class TestUnknownFieldsAreRefused:
    @pytest.mark.parametrize("name", _INPUT_MODELS)
    def test_the_model_forbids_extra_fields(self, name: str) -> None:
        assert _MODELS[name].model_config.get("extra") == "forbid"

    def test_the_exemptions_all_still_exist(self) -> None:
        """A renamed or deleted model must not leave a stale hole in the sweep."""
        assert _MODELS.keys() >= _EXEMPT
