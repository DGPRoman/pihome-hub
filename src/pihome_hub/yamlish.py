"""Accommodations for how YAML actually behaves.

YAML 1.1 — which PyYAML implements — resolves bare ``on``, ``off``, ``yes`` and
``no`` to booleans. So a configuration field whose natural spelling is ``on``
arrives as ``True``:

    then:
      relay: porch-light
      state: on        # parses as the boolean True, not the string "on"

Quoting fixes it, but nobody writes ``state: "on"`` on the first attempt, and the
failure is a validation error about a type the author never typed. Rather than
push that trap onto whoever is wiring up their house, the affected fields accept
the boolean and mean the obvious thing by it.
"""

from __future__ import annotations

from typing import Any


def switch_word(value: Any) -> Any:  # noqa: ANN401 - a pydantic BeforeValidator sees raw input
    """Map a YAML-coerced boolean back to the on/off word the author wrote."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return value
