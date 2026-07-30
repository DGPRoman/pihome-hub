"""pihome-hub — HTTP control plane for Raspberry Pi relays, sensors and automation rules."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pihome-hub")
except PackageNotFoundError:  # pragma: no cover - only hit when running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
