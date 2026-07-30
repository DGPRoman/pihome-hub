"""Logging setup.

The service runs under systemd, so everything goes to stdout and journald owns
persistence and rotation. Two formats are supported: readable lines for a terminal,
and one JSON object per record for log shipping.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Final

from pihome_hub.config import LogLevel

_LINE_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

#: Attributes present on every ``LogRecord``. Anything outside this set was added
#: by the caller via ``extra=`` and therefore belongs in the JSON payload.
_STANDARD_RECORD_ATTRS: Final = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _caller_supplied_fields(record: logging.LogRecord) -> dict[str, Any]:
    """Everything the caller attached via ``extra=``, in the order it was added."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_")
    }


class TextFormatter(logging.Formatter):
    """Readable lines that still carry ``extra=`` fields.

    The stdlib formatter drops them: ``%(message)s`` renders only the message, so a
    line logged with ``extra={"client": ...}`` silently loses the one detail worth
    having. Since every diagnostic in this service — who failed authentication, which
    relay was switched — travels in ``extra``, they are appended as ``key=value``.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = _caller_supplied_fields(record)
        if fields:
            line += " " + " ".join(f"{key}={value!r}" for key, value in fields.items())
        return line


class JsonFormatter(logging.Formatter):
    """Render each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        payload.update(_caller_supplied_fields(record))

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: LogLevel, *, json_output: bool = False) -> None:
    """Install a single stdout handler on the root logger.

    Safe to call more than once: existing handlers are replaced rather than added
    to, so repeated calls cannot produce duplicated log lines.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter(_LINE_FORMAT))

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
