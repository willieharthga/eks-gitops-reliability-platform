"""Structured JSON logging correlated with the active trace.

Loki indexes these records and Grafana joins them to Tempo traces, so every
line must carry ``trace_id`` and ``span_id`` whenever a span is active. The
formatter reads the trace from the context variable rather than requiring each
call site to pass it, because a forgotten argument silently breaks correlation.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any, Final

from . import tracing

RESERVED: Final = frozenset(
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


class JSONFormatter(logging.Formatter):
    """Render one log record as a single JSON object on one line."""

    def __init__(self, static_fields: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self.static_fields = dict(static_fields or {})

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(self.static_fields)

        span = tracing.current_span()
        if span is not None:
            payload["trace_id"] = span.trace_id
            payload["span_id"] = span.span_id

        for key, value in record.__dict__.items():
            if key not in RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure(
    service: str,
    *,
    level: str = "INFO",
    static_fields: Mapping[str, Any] | None = None,
    stream: Any | None = None,
) -> logging.Logger:
    """Install the JSON formatter on the root logger and return the service logger.

    Handlers are replaced rather than appended so that repeated calls, which
    happen in tests, cannot produce duplicated output.
    """
    resolved = logging.getLevelNamesMapping().get(level.upper())
    if resolved is None:
        raise ValueError(f"unknown log level: {level!r}")

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JSONFormatter(static_fields))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(resolved)

    return logging.getLogger(service)
