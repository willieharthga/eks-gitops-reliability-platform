"""Load generator service."""

from __future__ import annotations

import sys
import time

from platform_common import config, httpapp, runtime, tracing, version

from .generator import TrafficPlan, TrafficProfile, sleep_until

SERVICE = "load-generator"
UPSTREAM = "frontend"


class LoadRunner:
    """Sends the planned traffic and records what the platform did with it."""

    def __init__(
        self,
        app: httpapp.Application,
        plan: TrafficPlan,
        *,
        target_base_url: str,
        now=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.app = app
        self.plan = plan
        self.target_base_url = target_base_url.rstrip("/")
        self._now = now
        self._sleep = sleep
        self._start = now()
        self._schedule = plan.schedule(self._start)

        self.requests = app.registry.counter(
            "loadgen_requests_total",
            "Synthetic requests sent, by operation and response status.",
            ("operation", "status"),
        )
        self.duration = app.registry.histogram(
            "loadgen_request_duration_seconds",
            "Synthetic request duration observed by the client.",
            ("operation",),
        )
        self.lag = app.registry.gauge(
            "loadgen_schedule_lag_seconds",
            "How far behind the offered-rate schedule the generator is running.",
        )
        self.unreachable = app.registry.counter(
            "loadgen_unreachable_total", "Requests that never received a response."
        )

    def _send(self, operation: str, method: str, path: str, payload: dict | None):
        started = self._now()
        try:
            status, _ = self.app.call_json(
                UPSTREAM,
                method,
                f"{self.target_base_url}{path}",
                payload,
                timeout=10.0,
            )
        except httpapp.HTTPError:
            self.unreachable.inc()
            status = 0
        finally:
            self.duration.observe(self._now() - started, operation=operation)
        self.requests.inc(operation=operation, status=str(status))

    def run_once(self) -> bool:
        """Wait for the next scheduled slot and send exactly one request."""
        deadline = next(self._schedule, None)
        if deadline is None:
            # A zero offered rate means "stay up and serve metrics, send nothing".
            self._sleep(1.0)
            return True

        self.lag.set(sleep_until(deadline, now=self._now, sleep=self._sleep))

        operation = self.plan.next_operation()
        if operation.kind == "list":
            self._send("list", "GET", "/api/summary", None)
        else:
            self._send(operation.kind, "POST", "/orders", operation.payload)
        return True


def build_application(
    *,
    build: version.BuildInfo | None = None,
    tracer: tracing.Tracer | None = None,
    environ: config.Environ | None = None,
) -> httpapp.Application:
    build = build or version.from_environment(SERVICE, environ=environ)
    app = httpapp.Application(build, tracer=tracer)
    app.readiness.add("self", lambda: True)
    return app


def main() -> int:
    build, logger, tracer = runtime.configure_service(SERVICE)
    app = build_application(build=build, tracer=tracer)

    profile = TrafficProfile.from_environment()
    runner = LoadRunner(
        app,
        TrafficPlan(profile),
        target_base_url=config.get_url("TARGET_BASE_URL", "http://frontend:8080"),
    )
    logger.info(
        "traffic profile resolved",
        extra={
            "requests_per_second": profile.requests_per_second,
            "read_weight": profile.read_weight,
            "write_weight": profile.write_weight,
            "invalid_ratio": profile.invalid_ratio,
        },
    )

    return runtime.run_loop(
        logger,
        tracer,
        iteration=runner.run_once,
        interval=1.0,
        metrics_app=app,
    )


if __name__ == "__main__":
    sys.exit(main())
