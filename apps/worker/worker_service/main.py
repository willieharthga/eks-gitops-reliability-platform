"""Worker service: claims tasks from the API and reports their outcome."""

from __future__ import annotations

import sys
import time

from platform_common import config, httpapp, runtime, tracing, version

from .processor import ProcessingProfile, TaskProcessor

SERVICE = "worker"
UPSTREAM = "api"


class Worker:
    """One poll-process-report cycle, instrumented end to end.

    The span for a task is parented to the ``traceparent`` carried on the
    message, not to the poll that claimed it. That is what makes a single trace
    span the synchronous API call and the asynchronous processing that followed
    it; parenting to the poll would produce one useless trace per worker tick.
    """

    def __init__(
        self,
        app: httpapp.Application,
        processor: TaskProcessor,
        *,
        api_base_url: str,
    ) -> None:
        self.app = app
        self.processor = processor
        self.api_base_url = api_base_url.rstrip("/")
        self.logger = app.logger
        self.last_successful_poll = 0.0

        self.claimed = app.registry.counter(
            "worker_tasks_claimed_total", "Tasks claimed from the API."
        )
        self.processed = app.registry.counter(
            "worker_tasks_processed_total", "Tasks processed, by outcome.", ("outcome",)
        )
        self.poll_errors = app.registry.counter(
            "worker_poll_errors_total", "Failed interactions with the API.", ("phase",)
        )
        self.duration = app.registry.histogram(
            "worker_task_duration_seconds", "Task processing duration in seconds."
        )
        self.idle_polls = app.registry.counter(
            "worker_idle_polls_total", "Polls that found no work."
        )
        self.trace_continuity = app.registry.counter(
            "worker_trace_continuity_total",
            "Tasks by whether they carried a usable trace context.",
            ("continued",),
        )

        app.readiness.add("api_reachable", self._api_recently_reachable)

    def _api_recently_reachable(self) -> bool:
        # Before the first successful poll the worker is genuinely not ready.
        # After that, tolerate one polling gap so a single blip does not flap
        # the endpoint out of service.
        if self.last_successful_poll == 0.0:
            return False
        return (time.monotonic() - self.last_successful_poll) < 60.0

    def claim(self) -> dict | None:
        status, body = self.app.call_json(
            UPSTREAM, "POST", f"{self.api_base_url}/internal/tasks/claim"
        )
        if status == 204 or not body:
            self.last_successful_poll = time.monotonic()
            self.idle_polls.inc()
            return None
        if status != 200:
            self.poll_errors.inc(phase="claim")
            return None
        self.last_successful_poll = time.monotonic()
        task = body.get("task") if isinstance(body, dict) else None
        return task if isinstance(task, dict) else None

    def report(self, task_id: str, *, succeeded: bool, detail: str) -> None:
        status, _ = self.app.call_json(
            UPSTREAM,
            "POST",
            f"{self.api_base_url}/internal/tasks/{task_id}/complete",
            {"succeeded": succeeded, "detail": detail},
        )
        if status >= 400:
            self.poll_errors.inc(phase="complete")

    def run_once(self) -> bool:
        """Claim and process at most one task. Returns whether work was done."""
        try:
            task = self.claim()
        except httpapp.HTTPError:
            self.poll_errors.inc(phase="claim")
            self.logger.warning("could not reach the API to claim work")
            return False

        if task is None:
            return False

        self.claimed.inc()
        parent = tracing.parse_traceparent(task.get("traceparent") or "")
        self.trace_continuity.inc(continued="true" if parent else "false")

        with self.app.tracer.span(
            "process task",
            kind=tracing.SpanKind.CONSUMER,
            parent=parent,
            attributes={
                "messaging.operation.name": "process",
                "messaging.message.id": task.get("task_id", ""),
                "order.id": task.get("order_id", ""),
                "task.attempts": task.get("attempts", 1),
            },
        ) as span:
            result = self.processor.process(
                str(task.get("item", "")), int(task.get("quantity", 1))
            )
            self.duration.observe(result.duration_seconds)
            self.processed.inc(outcome="succeeded" if result.succeeded else "failed")
            span.set_attribute("task.succeeded", result.succeeded)
            if not result.succeeded:
                span.set_status(tracing.StatusCode.ERROR, result.detail)

            try:
                self.report(
                    str(task.get("task_id", "")),
                    succeeded=result.succeeded,
                    detail=result.detail,
                )
            except httpapp.HTTPError:
                # The API requeues claims that are never reported, so losing
                # this call delays the order rather than losing it.
                self.poll_errors.inc(phase="complete")
                self.logger.warning(
                    "could not report task completion; the API will requeue it",
                    extra={"task_id": task.get("task_id", "")},
                )
        return True


def build_application(
    *,
    build: version.BuildInfo | None = None,
    tracer: tracing.Tracer | None = None,
    environ: config.Environ | None = None,
) -> httpapp.Application:
    """Create the worker's metrics/health surface (it serves no business routes)."""
    build = build or version.from_environment(SERVICE, environ=environ)
    return httpapp.Application(build, tracer=tracer)


def main() -> int:
    build, logger, tracer = runtime.configure_service(SERVICE)
    app = build_application(build=build, tracer=tracer)

    worker = Worker(
        app,
        TaskProcessor(ProcessingProfile.from_environment()),
        api_base_url=config.get_url("API_BASE_URL", "http://api:8080"),
    )
    interval = config.get_float(
        "POLL_INTERVAL_SECONDS", 0.5, minimum=0.01, maximum=60.0
    )

    return runtime.run_loop(
        logger,
        tracer,
        iteration=worker.run_once,
        interval=interval,
        metrics_app=app,
    )


if __name__ == "__main__":
    sys.exit(main())
