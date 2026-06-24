"""A minimal HTTP application framework with built-in platform behavior.

Every service gets the same operational surface for free: liveness and
readiness endpoints that mean different things, a Prometheus scrape endpoint, a
build-info endpoint, golden-signal metrics, structured request logs, and W3C
trace propagation both inbound and outbound.

Routes are matched against templates such as ``/api/orders/{order_id}``. The
template, never the concrete path, is used as the ``route`` metric label so a
high-cardinality identifier cannot create unbounded time series.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final

from . import metrics as metrics_module
from . import tracing
from .version import BuildInfo

JSON_CONTENT_TYPE: Final = "application/json"
_PARAM = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")

LOGGER = logging.getLogger("platform_common.http")


class HTTPError(Exception):
    """Raised by a handler to return a specific status with a JSON body."""

    def __init__(self, status: int, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details

    def to_response(self) -> Response:
        payload: dict[str, Any] = {"error": self.message, "status": self.status}
        payload.update(self.details)
        return Response.json(payload, status=self.status)


@dataclass
class Request:
    """An inbound request together with its routing and trace context."""

    method: str
    path: str
    route: str
    headers: Mapping[str, str]
    path_params: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, list[str]] = field(default_factory=dict)
    body: bytes = b""
    span: tracing.Span | None = None

    def json(self, *, required: bool = True) -> Any:
        if not self.body:
            if required:
                raise HTTPError(400, "request body is required")
            return None
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPError(400, f"request body is not valid JSON: {exc}") from exc

    def query_one(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def header(self, name: str, default: str = "") -> str:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default


@dataclass
class Response:
    """An outbound response."""

    status: int = 200
    body: bytes = b""
    content_type: str = JSON_CONTENT_TYPE
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, payload: Any, status: int = 200, **headers: str) -> Response:
        body = json.dumps(payload, default=str, sort_keys=True).encode("utf-8")
        return cls(status=status, body=body, content_type=JSON_CONTENT_TYPE, headers=dict(headers))

    @classmethod
    def text(cls, body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> Response:
        return cls(status=status, body=body.encode("utf-8"), content_type=content_type)

    @classmethod
    def html(cls, body: str, status: int = 200) -> Response:
        return cls.text(body, status=status, content_type="text/html; charset=utf-8")

    @classmethod
    def empty(cls, status: int = 204) -> Response:
        return cls(status=status, body=b"", content_type=JSON_CONTENT_TYPE)


Handler = Callable[[Request], Response]


@dataclass(frozen=True)
class Route:
    """One method/template pair and the segments used to match it."""

    method: str
    template: str
    handler: Handler
    segments: tuple[str, ...]

    @staticmethod
    def split(path: str) -> tuple[str, ...]:
        return tuple(segment for segment in path.split("/") if segment)

    @staticmethod
    def shape(segments: Sequence[str]) -> tuple[str, ...]:
        """Collapse parameter names so equivalent templates compare equal.

        ``/items/{item_id}`` and ``/items/{other}`` match exactly the same
        requests. Comparing raw segments would treat them as distinct and let
        both register, after which the winner depends on declaration order.
        """
        return tuple("{}" if _PARAM.match(segment) else segment for segment in segments)

    @property
    def parameter_count(self) -> int:
        return sum(1 for segment in self.segments if _PARAM.match(segment))

    def match(self, segments: Sequence[str]) -> dict[str, str] | None:
        if len(segments) != len(self.segments):
            return None
        params: dict[str, str] = {}
        for pattern, actual in zip(self.segments, segments, strict=True):
            parameter = _PARAM.match(pattern)
            if parameter:
                params[parameter.group(1)] = urllib.parse.unquote(actual)
            elif pattern != actual:
                return None
        return params


class ReadinessRegistry:
    """Readiness checks that gate traffic without affecting liveness.

    Liveness answers "should the kubelet restart this container"; readiness
    answers "can this replica serve". Conflating them turns a dependency outage
    into a restart loop, so the two endpoints are backed by different state.
    """

    def __init__(self) -> None:
        self._checks: dict[str, Callable[[], bool]] = {}
        self._lock = threading.Lock()
        self._draining = False

    def add(self, name: str, check: Callable[[], bool]) -> None:
        with self._lock:
            self._checks[name] = check

    def start_draining(self) -> None:
        with self._lock:
            self._draining = True

    @property
    def draining(self) -> bool:
        with self._lock:
            return self._draining

    def evaluate(self) -> tuple[bool, dict[str, str]]:
        with self._lock:
            draining = self._draining
            checks = dict(self._checks)
        results: dict[str, str] = {}
        ready = not draining
        if draining:
            results["draining"] = "fail"
        for name, check in checks.items():
            try:
                passed = bool(check())
            except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
                results[name] = f"error: {exc}"
                ready = False
                continue
            results[name] = "pass" if passed else "fail"
            ready = ready and passed
        return ready, results


class Application:
    """Routes requests and applies the shared observability behavior."""

    def __init__(
        self,
        build: BuildInfo,
        *,
        registry: metrics_module.Registry | None = None,
        tracer: tracing.Tracer | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.build = build
        self.registry = registry or metrics_module.Registry(
            constant_labels=build.as_labels()
        )
        self.tracer = tracer or tracing.Tracer(build.service)
        self.logger = logger or logging.getLogger(build.service)
        self.readiness = ReadinessRegistry()
        self._routes: list[Route] = []
        self._started = time.monotonic()

        self.requests_total = self.registry.counter(
            "http_requests_total",
            "Total HTTP requests handled, by route and response status.",
            ("method", "route", "status"),
        )
        self.request_duration = self.registry.histogram(
            "http_request_duration_seconds",
            "HTTP request handling duration in seconds.",
            ("method", "route"),
        )
        self.requests_in_flight = self.registry.gauge(
            "http_requests_in_flight",
            "HTTP requests currently being handled.",
        )
        self.client_requests_total = self.registry.counter(
            "http_client_requests_total",
            "Outbound HTTP requests, by upstream and outcome.",
            ("upstream", "method", "status"),
        )
        self.client_duration = self.registry.histogram(
            "http_client_request_duration_seconds",
            "Outbound HTTP request duration in seconds.",
            ("upstream", "method"),
        )
        self.build_info = self.registry.gauge(
            "service_build_info",
            "Always 1; labels carry the running artifact identity.",
            ("revision", "image_digest"),
        )
        self.build_info.set(
            1, revision=build.revision, image_digest=build.image_digest
        )
        self.uptime = self.registry.gauge(
            "service_uptime_seconds", "Seconds since this process started serving."
        )

        self._register_builtin_routes()

    def route(self, method: str, template: str) -> Callable[[Handler], Handler]:
        def decorator(handler: Handler) -> Handler:
            self.add_route(method, template, handler)
            return handler

        return decorator

    def add_route(self, method: str, template: str, handler: Handler) -> None:
        normalized = method.upper()
        segments = Route.split(template)
        shape = Route.shape(segments)
        for existing in self._routes:
            if existing.method == normalized and Route.shape(existing.segments) == shape:
                raise ValueError(
                    f"route {normalized} {template} conflicts with "
                    f"{existing.method} {existing.template}"
                )
        self._routes.append(Route(normalized, template, handler, segments))
        # Prefer the most specific template so that a literal such as
        # /internal/tasks/claim always beats /internal/tasks/{task_id},
        # regardless of the order the routes happened to be registered in.
        # The sort is stable, so equally specific routes keep insertion order.
        self._routes.sort(key=lambda route: route.parameter_count)

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

    def _register_builtin_routes(self) -> None:
        self.add_route("GET", "/healthz", self._handle_healthz)
        self.add_route("GET", "/readyz", self._handle_readyz)
        self.add_route("GET", "/metrics", self._handle_metrics)
        self.add_route("GET", "/buildz", self._handle_buildz)

    def _handle_healthz(self, request: Request) -> Response:
        return Response.json({"status": "ok", "service": self.build.service})

    def _handle_readyz(self, request: Request) -> Response:
        ready, checks = self.readiness.evaluate()
        return Response.json(
            {"status": "ready" if ready else "not-ready", "checks": checks},
            status=200 if ready else 503,
        )

    def _handle_metrics(self, request: Request) -> Response:
        self.uptime.set(time.monotonic() - self._started)
        return Response.text(
            self.registry.render(), content_type=metrics_module.CONTENT_TYPE
        )

    def _handle_buildz(self, request: Request) -> Response:
        return Response.json(self.build.as_dict())

    def _resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]] | int:
        segments = Route.split(path)
        path_exists = False
        for candidate in self._routes:
            params = candidate.match(segments)
            if params is None:
                continue
            path_exists = True
            if candidate.method == method:
                return candidate, params
        return 405 if path_exists else 404

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes = b"") -> Response:
        """Dispatch one request. This is the seam that unit tests drive directly."""
        method = method.upper()
        parsed = urllib.parse.urlsplit(target)
        path = urllib.parse.unquote(parsed.path) or "/"
        resolved = self._resolve(method, path)

        if isinstance(resolved, int):
            route_label = "unmatched"
            response = HTTPError(
                resolved,
                "not found" if resolved == 404 else "method not allowed",
                path=path,
            ).to_response()
            self.requests_total.inc(
                method=method, route=route_label, status=str(response.status)
            )
            return response

        route, params = resolved
        parent = self.tracer.extract(headers)
        # A crash between here and the handler must still be counted, so the
        # response starts as a server error and is replaced on success.
        response = Response.json({"error": "internal server error", "status": 500}, status=500)
        self.requests_in_flight.inc()
        started = time.perf_counter()
        try:
            with self.tracer.span(
                f"{method} {route.template}",
                kind=tracing.SpanKind.SERVER,
                parent=parent,
                attributes={
                    "http.request.method": method,
                    "url.path": path,
                    "http.route": route.template,
                    "service.name": self.build.service,
                },
            ) as span:
                request = Request(
                    method=method,
                    path=path,
                    route=route.template,
                    headers=headers,
                    path_params=params,
                    query=urllib.parse.parse_qs(parsed.query),
                    body=body,
                    span=span,
                )
                try:
                    response = route.handler(request)
                except HTTPError as exc:
                    response = exc.to_response()
                except Exception as exc:  # noqa: BLE001 - contain handler faults
                    self.logger.exception(
                        "unhandled handler error", extra={"http_route": route.template}
                    )
                    span.record_exception(exc)

                span.set_attribute("http.response.status_code", response.status)
                if response.status >= 500:
                    span.set_status(tracing.StatusCode.ERROR, f"status {response.status}")
                elif span.status_code is tracing.StatusCode.UNSET:
                    span.set_status(tracing.StatusCode.OK)

                self.logger.info(
                    "handled request",
                    extra={
                        "http_method": method,
                        "http_route": route.template,
                        "http_status": response.status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    },
                )
                return response
        finally:
            self.requests_in_flight.dec()
            self.request_duration.observe(
                time.perf_counter() - started, method=method, route=route.template
            )
            self.requests_total.inc(
                method=method, route=route.template, status=str(response.status)
            )

    def call_json(
        self,
        upstream: str,
        method: str,
        url: str,
        payload: Any | None = None,
        *,
        timeout: float = 5.0,
    ) -> tuple[int, Any]:
        """Call an upstream service, propagating trace context and recording metrics."""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": JSON_CONTENT_TYPE}
        if data is not None:
            headers["Content-Type"] = JSON_CONTENT_TYPE

        started = time.perf_counter()
        with self.tracer.span(
            f"{method.upper()} {upstream}",
            kind=tracing.SpanKind.CLIENT,
            attributes={
                "http.request.method": method.upper(),
                "server.address": upstream,
                "url.full": url,
            },
        ) as span:
            self.tracer.inject(headers, span.context)
            request = urllib.request.Request(  # noqa: S310 - URL comes from config
                url, data=data, method=method.upper(), headers=headers
            )
            status = 0
            body: Any = None
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                    status = response.status
                    raw = response.read()
                    body = json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                status = exc.code
                raw = exc.read()
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    body = {"error": raw.decode("utf-8", "replace")}
                span.set_status(tracing.StatusCode.ERROR, f"status {status}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                span.record_exception(exc)
                raise HTTPError(502, f"upstream {upstream} unreachable: {exc}") from exc
            finally:
                elapsed = time.perf_counter() - started
                span.set_attribute("http.response.status_code", status)
                self.client_duration.observe(
                    elapsed, upstream=upstream, method=method.upper()
                )
                self.client_requests_total.inc(
                    upstream=upstream, method=method.upper(), status=str(status)
                )
            return status, body


class _RequestHandler(BaseHTTPRequestHandler):
    """Adapts :class:`Application` to :mod:`http.server`."""

    protocol_version = "HTTP/1.1"
    server_version = "platform-common"
    sys_version = ""
    application: Application

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        response = self.application.handle(
            self.command, self.path, dict(self.headers.items()), body
        )
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD" and response.body:
            self.wfile.write(response.body)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_HEAD = _dispatch

    def log_message(self, format: str, *args: Any) -> None:
        # Request logging is emitted as structured JSON by the application.
        LOGGER.debug("http server: " + format, *args)


def build_server(app: Application, host: str, port: int) -> ThreadingHTTPServer:
    """Create a threaded server bound to ``host:port`` without starting it."""
    handler = type("BoundRequestHandler", (_RequestHandler,), {"application": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
