"""Shared process lifecycle: telemetry wiring, signal handling, graceful drain.

Every service needs the same startup and shutdown behavior. Duplicating it per
service is how one of them ends up without a SIGTERM handler and starts
returning connection resets during a rollout.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from types import FrameType

from . import config, httpapp, tracing
from . import logging as platform_logging
from .version import BuildInfo, from_environment


def build_tracer(build: BuildInfo, *, environ: config.Environ | None = None) -> tracing.Tracer:
    """Create a tracer that exports over OTLP/HTTP only when an endpoint is set."""
    endpoint = config.get_url("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "", environ=environ)
    if not endpoint:
        return tracing.Tracer(build.service)
    exporter = tracing.OTLPSpanExporter(
        endpoint,
        {
            "service.name": build.service,
            "service.version": build.version,
            "service.instance.id": build.instance,
            "deployment.environment.name": build.environment,
        },
    )
    return tracing.Tracer(build.service, exporter)


def configure_service(
    service: str, *, environ: config.Environ | None = None
) -> tuple[BuildInfo, logging.Logger, tracing.Tracer]:
    """Resolve build identity and start structured logging and tracing."""
    build = from_environment(service, environ=environ)
    logger = platform_logging.configure(
        service,
        level=config.get_str("LOG_LEVEL", "INFO", environ=environ),
        static_fields={
            "service": build.service,
            "environment": build.environment,
            "version": build.version,
            "instance": build.instance,
        },
    )
    if not build.is_traceable:
        logger.warning(
            "build identity is incomplete; this artifact cannot be traced to a "
            "reviewed promotion",
            extra={
                "version": build.version,
                "revision": build.revision,
                "image_digest": build.image_digest,
            },
        )
    return build, logger, build_tracer(build, environ=environ)


def install_signal_handlers(
    logger: logging.Logger,
    *,
    on_drain: Callable[[], None],
    on_stop: Callable[[], None],
    drain_seconds: float,
) -> None:
    """Drain readiness, pause, then stop.

    The pause matters: kubelet sends SIGTERM and the endpoints controller
    removes the pod concurrently, so a process that stops instantly will refuse
    requests that were routed microseconds earlier. Waiting one propagation
    interval before shutting down converts those errors into clean responses.
    """
    stopping = threading.Event()

    def handler(signum: int, frame: FrameType | None) -> None:
        if stopping.is_set():
            return
        stopping.set()
        logger.info("shutdown signal received", extra={"signal": signum})
        on_drain()

        def finish() -> None:
            time.sleep(drain_seconds)
            logger.info("drain complete; stopping")
            on_stop()

        threading.Thread(target=finish, name="drain", daemon=True).start()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def serve_http(
    app: httpapp.Application,
    logger: logging.Logger,
    tracer: tracing.Tracer,
    *,
    environ: config.Environ | None = None,
) -> int:
    """Run an HTTP service until it is signalled, then shut down gracefully."""
    host = config.get_str("HTTP_HOST", "0.0.0.0", environ=environ)  # noqa: S104
    port = config.get_int("HTTP_PORT", 8080, minimum=1, maximum=65535, environ=environ)
    drain = config.get_float(
        "SHUTDOWN_DRAIN_SECONDS", 5.0, minimum=0.0, maximum=120.0, environ=environ
    )

    server: ThreadingHTTPServer = httpapp.build_server(app, host, port)
    install_signal_handlers(
        logger,
        on_drain=app.readiness.start_draining,
        # serve_forever() cannot be stopped from the thread running it.
        on_stop=lambda: threading.Thread(target=server.shutdown, daemon=True).start(),
        drain_seconds=drain,
    )

    logger.info("listening", extra={"host": host, "port": port})
    try:
        server.serve_forever()
    finally:
        server.server_close()
        tracer.shutdown()
        logger.info("stopped")
    return 0


def run_loop(
    logger: logging.Logger,
    tracer: tracing.Tracer,
    *,
    iteration: Callable[[], bool],
    interval: float,
    error_backoff: float = 1.0,
    metrics_app: httpapp.Application | None = None,
    environ: config.Environ | None = None,
) -> int:
    """Run a polling worker until signalled, serving metrics alongside it.

    ``iteration`` returns whether it performed work. When it did, the next
    iteration starts immediately so a backlog drains at full speed; when it did
    not, the loop idles for ``interval`` instead of spinning the CPU.
    """
    stop = threading.Event()
    server: ThreadingHTTPServer | None = None

    if metrics_app is not None:
        host = config.get_str("HTTP_HOST", "0.0.0.0", environ=environ)  # noqa: S104
        port = config.get_int(
            "HTTP_PORT", 8080, minimum=1, maximum=65535, environ=environ
        )
        server = httpapp.build_server(metrics_app, host, port)
        threading.Thread(
            target=server.serve_forever, name="metrics", daemon=True
        ).start()
        logger.info("metrics listening", extra={"host": host, "port": port})

    def drain() -> None:
        if metrics_app is not None:
            metrics_app.readiness.start_draining()

    install_signal_handlers(
        logger,
        on_drain=drain,
        on_stop=stop.set,
        drain_seconds=config.get_float(
            "SHUTDOWN_DRAIN_SECONDS", 5.0, minimum=0.0, maximum=120.0, environ=environ
        ),
    )

    logger.info("worker loop started", extra={"interval_seconds": interval})
    try:
        while not stop.is_set():
            try:
                did_work = iteration()
            except Exception:  # noqa: BLE001 - one bad iteration must not kill the loop
                logger.exception("iteration failed")
                stop.wait(error_backoff)
                continue
            if not did_work:
                stop.wait(interval)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        tracer.shutdown()
        logger.info("stopped")
    return 0
