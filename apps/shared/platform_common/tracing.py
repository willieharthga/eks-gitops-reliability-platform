"""W3C trace context propagation and an OTLP/HTTP JSON span exporter.

The OpenTelemetry SDK is intentionally not a dependency. This module implements
the two wire contracts the platform actually relies on:

* `W3C Trace Context <https://www.w3.org/TR/trace-context/>`_ for propagating a
  trace across synchronous HTTP calls and across queued work.
* The OTLP/JSON encoding accepted by the OpenTelemetry Collector's ``otlphttp``
  receiver, in which ``traceId`` and ``spanId`` are hex strings rather than the
  base64 used for other ``bytes`` fields.

Exporting is best effort. Losing telemetry must never fail a user request, so
the exporter drops spans when its bounded queue is full and reports the loss
through a counter instead of blocking the caller.
"""

from __future__ import annotations

import contextvars
import json
import queue
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from types import TracebackType
from typing import Any, Final

TRACEPARENT_HEADER: Final = "traceparent"
TRACESTATE_HEADER: Final = "tracestate"
SUPPORTED_VERSION: Final = "00"
FLAG_SAMPLED: Final = 0x01

INVALID_TRACE_ID: Final = "0" * 32
INVALID_SPAN_ID: Final = "0" * 16

_HEX_DIGITS: Final = frozenset("0123456789abcdef")


class SpanKind(IntEnum):
    """OTLP span kind values."""

    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class StatusCode(IntEnum):
    """OTLP status code values."""

    UNSET = 0
    OK = 1
    ERROR = 2


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(char in _HEX_DIGITS for char in value)


@dataclass(frozen=True)
class SpanContext:
    """Identity of a span, whether created locally or received from a peer."""

    trace_id: str
    span_id: str
    sampled: bool = True
    remote: bool = False
    tracestate: str = ""

    @property
    def valid(self) -> bool:
        return (
            _is_hex(self.trace_id, 32)
            and _is_hex(self.span_id, 16)
            and self.trace_id != INVALID_TRACE_ID
            and self.span_id != INVALID_SPAN_ID
        )

    def to_traceparent(self) -> str:
        flags = FLAG_SAMPLED if self.sampled else 0
        return f"{SUPPORTED_VERSION}-{self.trace_id}-{self.span_id}-{flags:02x}"


def new_trace_id() -> str:
    """Return a random, non-zero 16-byte trace id in lowercase hex."""
    while True:
        candidate = secrets.token_hex(16)
        if candidate != INVALID_TRACE_ID:
            return candidate


def new_span_id() -> str:
    """Return a random, non-zero 8-byte span id in lowercase hex."""
    while True:
        candidate = secrets.token_hex(8)
        if candidate != INVALID_SPAN_ID:
            return candidate


def parse_traceparent(header: str | None, tracestate: str = "") -> SpanContext | None:
    """Parse a ``traceparent`` header, returning ``None`` when it is unusable.

    An unparsable header is treated as an absent one, as the specification
    requires: the receiver starts a new trace rather than rejecting the request.
    """
    if not header:
        return None
    fields = header.strip().split("-")
    if len(fields) < 4:
        return None
    version, trace_id, span_id, flags = fields[0], fields[1], fields[2], fields[3]
    if not _is_hex(version, 2) or version == "ff":
        return None
    # A future version may append fields, but the first four keep this meaning.
    if version == SUPPORTED_VERSION and len(fields) != 4:
        return None
    if not _is_hex(trace_id, 32) or trace_id == INVALID_TRACE_ID:
        return None
    if not _is_hex(span_id, 16) or span_id == INVALID_SPAN_ID:
        return None
    if not _is_hex(flags, 2):
        return None
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        sampled=bool(int(flags, 16) & FLAG_SAMPLED),
        remote=True,
        tracestate=tracestate.strip(),
    )


@dataclass
class Span:
    """A finished or in-flight unit of work."""

    name: str
    context: SpanContext
    kind: SpanKind = SpanKind.INTERNAL
    parent_span_id: str = ""
    start_time_ns: int = field(default_factory=time.time_ns)
    end_time_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status_code: StatusCode = StatusCode.UNSET
    status_message: str = ""

    _tracer: Tracer | None = field(default=None, repr=False, compare=False)
    _token: contextvars.Token[Span | None] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def trace_id(self) -> str:
        return self.context.trace_id

    @property
    def span_id(self) -> str:
        return self.context.span_id

    def set_attribute(self, key: str, value: Any) -> Span:
        self.attributes[key] = value
        return self

    def set_attributes(self, values: Mapping[str, Any]) -> Span:
        self.attributes.update(values)
        return self

    def set_status(self, code: StatusCode, message: str = "") -> Span:
        self.status_code = code
        self.status_message = message
        return self

    def record_exception(self, exc: BaseException) -> Span:
        self.set_attribute("exception.type", type(exc).__name__)
        self.set_attribute("exception.message", str(exc))
        return self.set_status(StatusCode.ERROR, type(exc).__name__)

    @property
    def duration_seconds(self) -> float:
        end = time.time_ns() if self.end_time_ns is None else self.end_time_ns
        return (end - self.start_time_ns) / 1_000_000_000

    def end(self) -> None:
        if self.end_time_ns is not None:
            return
        self.end_time_ns = time.time_ns()
        if self._tracer is not None:
            self._tracer._finish(self)

    def to_otlp(self) -> dict[str, Any]:
        span: dict[str, Any] = {
            "traceId": self.context.trace_id,
            "spanId": self.context.span_id,
            "name": self.name,
            "kind": int(self.kind),
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns or self.start_time_ns),
            "attributes": _encode_attributes(self.attributes),
        }
        if self.parent_span_id:
            span["parentSpanId"] = self.parent_span_id
        if self.status_code is not StatusCode.UNSET:
            status: dict[str, Any] = {"code": int(self.status_code)}
            if self.status_message:
                status["message"] = self.status_message
            span["status"] = status
        return span


def _encode_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_encode_value(item) for item in value]}}
    return {"stringValue": str(value)}


def _encode_attributes(attributes: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": key, "value": _encode_value(value)}
        for key, value in sorted(attributes.items())
    ]


_CURRENT_SPAN: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
    "platform_common_current_span", default=None
)


def current_span() -> Span | None:
    """Return the span active in this execution context, if any."""
    return _CURRENT_SPAN.get()


def current_context() -> SpanContext | None:
    span = current_span()
    return span.context if span is not None else None


class SpanExporter:
    """Exporter interface; the default implementation discards spans."""

    def export(self, spans: list[Span]) -> None:  # pragma: no cover - no-op base
        return None

    def shutdown(self, timeout: float = 5.0) -> None:  # pragma: no cover - no-op base
        return None


class InMemorySpanExporter(SpanExporter):
    """Collects spans for assertions in tests."""

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self._lock = threading.Lock()

    def export(self, spans: list[Span]) -> None:
        with self._lock:
            self.spans.extend(spans)

    def finished(self, name: str | None = None) -> list[Span]:
        with self._lock:
            if name is None:
                return list(self.spans)
            return [span for span in self.spans if span.name == name]

    def clear(self) -> None:
        with self._lock:
            self.spans.clear()


class OTLPSpanExporter(SpanExporter):
    """Batches spans onto a background thread and POSTs OTLP/JSON."""

    def __init__(
        self,
        endpoint: str,
        resource_attributes: Mapping[str, Any],
        *,
        max_queue_size: int = 2048,
        max_batch_size: int = 256,
        schedule_delay: float = 2.0,
        timeout: float = 5.0,
        scope_name: str = "platform_common.tracing",
    ) -> None:
        self.endpoint = endpoint
        self.resource_attributes = dict(resource_attributes)
        self.max_batch_size = max_batch_size
        self.schedule_delay = schedule_delay
        self.timeout = timeout
        self.scope_name = scope_name
        self.dropped_spans = 0
        self.failed_exports = 0
        self._queue: queue.Queue[Span] = queue.Queue(maxsize=max_queue_size)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run, name="otlp-span-exporter", daemon=True
        )
        self._worker.start()

    def export(self, spans: list[Span]) -> None:
        for span in spans:
            try:
                self._queue.put_nowait(span)
            except queue.Full:
                with self._lock:
                    self.dropped_spans += 1

    def _drain(self) -> list[Span]:
        batch: list[Span] = []
        while len(batch) < self.max_batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _run(self) -> None:
        while not self._stop.wait(self.schedule_delay):
            self._flush_once()
        self._flush_once()

    def _flush_once(self) -> None:
        while True:
            batch = self._drain()
            if not batch:
                return
            self._send(batch)

    def encode(self, spans: list[Span]) -> dict[str, Any]:
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": _encode_attributes(self.resource_attributes)
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": self.scope_name},
                            "spans": [span.to_otlp() for span in spans],
                        }
                    ],
                }
            ]
        }

    def _send(self, spans: list[Span]) -> None:
        payload = json.dumps(self.encode(spans)).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme validated by config
            self.endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):  # noqa: S310
                return
        except (urllib.error.URLError, TimeoutError, OSError):
            with self._lock:
                self.failed_exports += 1

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._worker.join(timeout=timeout)


class Tracer:
    """Creates spans, propagates context, and hands finished spans to an exporter."""

    def __init__(
        self,
        service_name: str,
        exporter: SpanExporter | None = None,
        *,
        resource_attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self.service_name = service_name
        self.exporter = exporter or SpanExporter()
        self.resource_attributes = dict(resource_attributes or {})

    def _finish(self, span: Span) -> None:
        if span.context.sampled:
            self.exporter.export([span])

    def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: SpanContext | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Span:
        """Create a span without making it current."""
        if parent is None:
            active = current_span()
            parent = active.context if active is not None else None
        if parent is not None and parent.valid:
            context = SpanContext(
                trace_id=parent.trace_id,
                span_id=new_span_id(),
                sampled=parent.sampled,
                tracestate=parent.tracestate,
            )
            parent_span_id = parent.span_id
        else:
            context = SpanContext(trace_id=new_trace_id(), span_id=new_span_id())
            parent_span_id = ""
        span = Span(
            name=name,
            context=context,
            kind=kind,
            parent_span_id=parent_span_id,
            attributes=dict(attributes or {}),
        )
        span._tracer = self
        return span

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        parent: SpanContext | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        """Start a span, make it current, and end it even if the body raises."""
        span = self.start_span(name, kind=kind, parent=parent, attributes=attributes)
        token = _CURRENT_SPAN.set(span)
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise
        finally:
            _CURRENT_SPAN.reset(token)
            span.end()

    def inject(self, carrier: dict[str, str], context: SpanContext | None = None) -> dict[str, str]:
        """Write the current (or supplied) context into a header-like carrier."""
        if context is None:
            context = current_context()
        if context is None or not context.valid:
            return carrier
        carrier[TRACEPARENT_HEADER] = context.to_traceparent()
        if context.tracestate:
            carrier[TRACESTATE_HEADER] = context.tracestate
        return carrier

    @staticmethod
    def extract(carrier: Mapping[str, str]) -> SpanContext | None:
        """Read a context from a header-like carrier, ignoring header case."""
        lowered = {key.lower(): value for key, value in carrier.items()}
        return parse_traceparent(
            lowered.get(TRACEPARENT_HEADER), lowered.get(TRACESTATE_HEADER, "")
        )

    def shutdown(self, timeout: float = 5.0) -> None:
        self.exporter.shutdown(timeout)

    def __enter__(self) -> Tracer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.shutdown()
