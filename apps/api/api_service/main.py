"""Order API service."""

from __future__ import annotations

import sys

from platform_common import config, httpapp, runtime, tracing, version

from .faults import FaultInjector, FaultProfile
from .store import OrderStore, QueueFull

SERVICE = "api"
MAX_QUANTITY = 100


def build_application(
    *,
    store: OrderStore | None = None,
    injector: FaultInjector | None = None,
    build: version.BuildInfo | None = None,
    tracer: tracing.Tracer | None = None,
    environ: config.Environ | None = None,
) -> httpapp.Application:
    """Wire the API. Every collaborator is injectable so tests need no network."""
    build = build or version.from_environment(SERVICE, environ=environ)
    store = store or OrderStore()
    injector = injector or FaultInjector(FaultProfile.from_environment(environ=environ))
    app = httpapp.Application(build, tracer=tracer)

    orders_created = app.registry.counter(
        "api_orders_created_total", "Orders accepted by the API."
    )
    orders_rejected = app.registry.counter(
        "api_orders_rejected_total", "Orders rejected, by reason.", ("reason",)
    )
    orders_finished = app.registry.counter(
        "api_orders_finished_total", "Orders reported finished by a worker.", ("outcome",)
    )
    queue_depth = app.registry.gauge(
        "api_task_queue_depth", "Tasks waiting to be claimed by a worker."
    )
    tasks_inflight = app.registry.gauge(
        "api_tasks_inflight", "Tasks claimed by a worker but not yet reported."
    )
    tasks_requeued = app.registry.counter(
        "api_tasks_requeued_total", "Tasks returned to the queue after a stale claim."
    )
    faults_injected = app.registry.counter(
        "api_faults_injected_total", "Deliberate faults injected, by kind.", ("kind",)
    )

    def refresh_queue_gauges() -> None:
        stats = store.stats()
        queue_depth.set(stats["pending_tasks"])
        tasks_inflight.set(stats["inflight_tasks"])

    # The API's own readiness never depends on the worker: a worker outage must
    # show up as a growing backlog and an SLO burn, not as an API that removes
    # itself from service and turns a partial degradation into a full outage.
    app.readiness.add("store", lambda: store.stats()["pending_tasks"] >= 0)

    @app.route("POST", "/api/orders")
    def create_order(request: httpapp.Request) -> httpapp.Response:
        payload = request.json()
        if not isinstance(payload, dict):
            orders_rejected.inc(reason="malformed")
            raise httpapp.HTTPError(400, "order payload must be a JSON object")

        item = str(payload.get("item", "")).strip()
        if not item:
            orders_rejected.inc(reason="missing_item")
            raise httpapp.HTTPError(400, "item is required")

        raw_quantity = payload.get("quantity", 1)
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            orders_rejected.inc(reason="invalid_quantity")
            raise httpapp.HTTPError(
                400, "quantity must be an integer", quantity=raw_quantity
            ) from None
        if not 1 <= quantity <= MAX_QUANTITY:
            orders_rejected.inc(reason="invalid_quantity")
            raise httpapp.HTTPError(
                400, f"quantity must be between 1 and {MAX_QUANTITY}", quantity=quantity
            )

        added = injector.apply_latency()
        if added:
            faults_injected.inc(kind="latency")
            if request.span is not None:
                request.span.set_attribute("fault.latency_seconds", added)
        if injector.should_fail():
            faults_injected.inc(kind="error")
            if request.span is not None:
                request.span.set_attribute("fault.injected", True)
            orders_rejected.inc(reason="injected_fault")
            raise httpapp.HTTPError(500, "injected fault: order processing failed")

        carrier: dict[str, str] = {}
        app.tracer.inject(carrier)
        span_context = tracing.current_context()

        try:
            order, task = store.create_order(
                item,
                quantity,
                traceparent=carrier.get(tracing.TRACEPARENT_HEADER, ""),
                trace_id=span_context.trace_id if span_context else "",
            )
        except QueueFull as exc:
            orders_rejected.inc(reason="queue_full")
            raise httpapp.HTTPError(503, str(exc)) from exc

        orders_created.inc()
        refresh_queue_gauges()
        if request.span is not None:
            request.span.set_attributes(
                {"order.id": order.order_id, "task.id": task.task_id}
            )
        return httpapp.Response.json(order.as_dict(), status=201)

    @app.route("GET", "/api/orders")
    def list_orders(request: httpapp.Request) -> httpapp.Response:
        limit = 50
        raw_limit = request.query_one("limit")
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except ValueError:
                raise httpapp.HTTPError(400, "limit must be an integer") from None
            limit = max(1, min(limit, 500))
        orders = [order.as_dict() for order in store.list_orders(limit)]
        return httpapp.Response.json({"orders": orders, "count": len(orders)})

    @app.route("GET", "/api/orders/{order_id}")
    def get_order(request: httpapp.Request) -> httpapp.Response:
        order_id = request.path_params["order_id"]
        order = store.get_order(order_id)
        if order is None:
            raise httpapp.HTTPError(404, "order not found", order_id=order_id)
        return httpapp.Response.json(order.as_dict())

    @app.route("GET", "/api/stats")
    def stats(request: httpapp.Request) -> httpapp.Response:
        refresh_queue_gauges()
        return httpapp.Response.json(store.stats())

    @app.route("GET", "/api/sli")
    def sli(request: httpapp.Request) -> httpapp.Response:
        """JSON SLI snapshot consumed by Argo Rollouts analysis.

        Prometheus queries belong to M6. Until a collector exists, analysis
        reads the same counters the process already exports, filtered to the
        order-create path so a scrape of this endpoint cannot grade itself.
        Zero samples fail closed: no evidence is not a successful canary.
        """
        route = "/api/orders"
        method = "POST"
        total = 0.0
        errors = 0.0
        for labels, value in app.requests_total.series():
            if labels.get("route") != route or labels.get("method") != method:
                continue
            total += value
            status = labels.get("status", "")
            if status.startswith("5"):
                errors += value
        if total <= 0:
            error_ratio = 1.0
            p95 = 0.0
        else:
            error_ratio = errors / total
            p95 = app.request_duration.quantile(0.95, method=method, route=route)
        return httpapp.Response.json(
            {
                "route": route,
                "method": method,
                "sample": int(total),
                "errors": int(errors),
                "error_ratio": round(error_ratio, 6),
                "success_rate": round(1.0 - error_ratio, 6),
                "p95_seconds": round(p95, 6),
            }
        )

    @app.route("POST", "/internal/tasks/claim")
    def claim_task(request: httpapp.Request) -> httpapp.Response:
        before = store.requeued_count
        task = store.claim_task()
        if store.requeued_count > before:
            tasks_requeued.inc(store.requeued_count - before)
        refresh_queue_gauges()
        if task is None:
            return httpapp.Response.json({"task": None}, status=204)
        return httpapp.Response.json({"task": task.as_dict()})

    @app.route("POST", "/internal/tasks/{task_id}/complete")
    def complete_task(request: httpapp.Request) -> httpapp.Response:
        payload = request.json(required=False) or {}
        if not isinstance(payload, dict):
            raise httpapp.HTTPError(400, "completion payload must be a JSON object")
        succeeded = bool(payload.get("succeeded", True))
        detail = str(payload.get("detail", ""))[:512]
        task_id = request.path_params["task_id"]
        order = store.complete_task(task_id, succeeded=succeeded, detail=detail)
        if order is None:
            raise httpapp.HTTPError(
                404, "task is not in flight", task_id=task_id
            )
        orders_finished.inc(outcome="succeeded" if succeeded else "failed")
        refresh_queue_gauges()
        return httpapp.Response.json(order.as_dict())

    refresh_queue_gauges()
    return app


def main() -> int:
    build, logger, tracer = runtime.configure_service(SERVICE)

    profile = FaultProfile.from_environment()
    if profile.enabled:
        # Loud on purpose: an operator seeing elevated errors must be able to
        # tell an injected fault from a real regression in one log line.
        logger.warning(
            "fault injection is enabled",
            extra={
                "fault_error_ratio": profile.error_ratio,
                "fault_latency_seconds": profile.latency_seconds,
                "fault_latency_ratio": profile.latency_ratio,
            },
        )

    app = build_application(injector=FaultInjector(profile), build=build, tracer=tracer)
    return runtime.serve_http(app, logger, tracer)


if __name__ == "__main__":
    sys.exit(main())
