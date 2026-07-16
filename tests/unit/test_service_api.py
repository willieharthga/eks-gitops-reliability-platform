"""Tests for the order API, its store, and its fault injector.

The fault injector is the mechanism M5 uses to make a canary fail on purpose.
If it were not exactly deterministic the rollback test would be flaky, and a
flaky rollback test is not evidence of anything, so its schedule is pinned.
"""

from __future__ import annotations

import json
import unittest

import _bootstrap  # noqa: F401
from api_service.faults import FaultInjector, FaultProfile
from api_service.main import build_application
from api_service.store import OrderStore, OrderStatus, QueueFull
from platform_common import tracing
from platform_common.version import BuildInfo

BUILD = BuildInfo(service="api", version="1.0.0", environment="dev")


def make_app(injector: FaultInjector | None = None, store: OrderStore | None = None):
    exporter = tracing.InMemorySpanExporter()
    app = build_application(
        store=store or OrderStore(),
        injector=injector or FaultInjector(FaultProfile()),
        build=BUILD,
        tracer=tracing.Tracer("api", exporter),
    )
    return app, exporter


def post(app, path: str, payload: dict, headers: dict | None = None):
    return app.handle("POST", path, headers or {}, json.dumps(payload).encode())


class OrderLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.exporter = make_app()

    def test_creating_an_order_returns_201_and_a_pending_order(self) -> None:
        response = post(self.app, "/api/orders", {"item": "widget", "quantity": 2})
        payload = json.loads(response.body)
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["item"], "widget")
        self.assertEqual(payload["quantity"], 2)
        self.assertEqual(payload["status"], "pending")
        self.assertTrue(payload["order_id"].startswith("ord-"))

    def test_quantity_defaults_to_one(self) -> None:
        response = post(self.app, "/api/orders", {"item": "widget"})
        self.assertEqual(json.loads(response.body)["quantity"], 1)

    def test_full_lifecycle_from_creation_to_completion(self) -> None:
        created = json.loads(post(self.app, "/api/orders", {"item": "widget"}).body)
        order_id = created["order_id"]

        claimed = json.loads(
            self.app.handle("POST", "/internal/tasks/claim", {}).body
        )["task"]
        self.assertEqual(claimed["order_id"], order_id)

        during = json.loads(self.app.handle("GET", f"/api/orders/{order_id}", {}).body)
        self.assertEqual(during["status"], "processing")

        completed = post(
            self.app,
            f"/internal/tasks/{claimed['task_id']}/complete",
            {"succeeded": True, "detail": "done"},
        )
        self.assertEqual(completed.status, 200)

        final = json.loads(self.app.handle("GET", f"/api/orders/{order_id}", {}).body)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["detail"], "done")

    def test_a_failed_task_marks_the_order_failed(self) -> None:
        post(self.app, "/api/orders", {"item": "widget"})
        task = json.loads(self.app.handle("POST", "/internal/tasks/claim", {}).body)["task"]
        post(
            self.app,
            f"/internal/tasks/{task['task_id']}/complete",
            {"succeeded": False, "detail": "nope"},
        )
        order = json.loads(
            self.app.handle("GET", f"/api/orders/{task['order_id']}", {}).body
        )
        self.assertEqual(order["status"], "failed")

    def test_claiming_with_an_empty_queue_returns_204(self) -> None:
        self.assertEqual(self.app.handle("POST", "/internal/tasks/claim", {}).status, 204)

    def test_completing_an_unknown_task_is_404(self) -> None:
        response = post(self.app, "/internal/tasks/tsk-999999/complete", {})
        self.assertEqual(response.status, 404)

    def test_completing_the_same_task_twice_is_404_the_second_time(self) -> None:
        post(self.app, "/api/orders", {"item": "widget"})
        task = json.loads(self.app.handle("POST", "/internal/tasks/claim", {}).body)["task"]
        self.assertEqual(
            post(self.app, f"/internal/tasks/{task['task_id']}/complete", {}).status, 200
        )
        self.assertEqual(
            post(self.app, f"/internal/tasks/{task['task_id']}/complete", {}).status, 404
        )

    def test_unknown_order_is_404(self) -> None:
        self.assertEqual(self.app.handle("GET", "/api/orders/ord-000999", {}).status, 404)

    def test_orders_are_listed_newest_first_and_respect_the_limit(self) -> None:
        for index in range(5):
            post(self.app, "/api/orders", {"item": f"item-{index}"})
        payload = json.loads(self.app.handle("GET", "/api/orders?limit=2", {}).body)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["orders"][0]["item"], "item-4")


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, _ = make_app()

    def test_invalid_payloads_are_rejected_with_400(self) -> None:
        cases = {
            "missing item": {"quantity": 1},
            "blank item": {"item": "   "},
            "zero quantity": {"item": "widget", "quantity": 0},
            "negative quantity": {"item": "widget", "quantity": -3},
            "over maximum": {"item": "widget", "quantity": 101},
            "non-numeric quantity": {"item": "widget", "quantity": "many"},
            "null quantity": {"item": "widget", "quantity": None},
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                self.assertEqual(post(self.app, "/api/orders", payload).status, 400)

    def test_a_non_object_body_is_rejected(self) -> None:
        response = self.app.handle("POST", "/api/orders", {}, b'["not", "an", "object"]')
        self.assertEqual(response.status, 400)

    def test_the_boundary_quantities_are_accepted(self) -> None:
        for quantity in (1, 100):
            with self.subTest(quantity=quantity):
                response = post(self.app, "/api/orders", {"item": "w", "quantity": quantity})
                self.assertEqual(response.status, 201)

    def test_a_non_integer_limit_is_rejected(self) -> None:
        self.assertEqual(self.app.handle("GET", "/api/orders?limit=abc", {}).status, 400)

    def test_rejections_are_counted_by_reason(self) -> None:
        post(self.app, "/api/orders", {"quantity": 1})
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('api_orders_rejected_total{environment="dev",reason="missing_item"', rendered)


class DeterministicFaultTests(unittest.TestCase):
    def test_a_ratio_of_one_fails_every_call(self) -> None:
        injector = FaultInjector(FaultProfile(error_ratio=1.0))
        self.assertEqual([injector.should_fail() for _ in range(5)], [True] * 5)

    def test_a_ratio_of_zero_never_fails(self) -> None:
        injector = FaultInjector(FaultProfile(error_ratio=0.0))
        self.assertEqual([injector.should_fail() for _ in range(5)], [False] * 5)

    def test_half_fails_exactly_every_other_call(self) -> None:
        injector = FaultInjector(FaultProfile(error_ratio=0.5))
        self.assertEqual(
            [injector.should_fail() for _ in range(6)],
            [False, True, False, True, False, True],
        )

    def test_the_realised_ratio_is_exact_over_many_calls(self) -> None:
        for ratio, expected in ((0.1, 100), (0.25, 250), (0.3, 300), (0.75, 750)):
            with self.subTest(ratio=ratio):
                injector = FaultInjector(FaultProfile(error_ratio=ratio))
                failures = sum(injector.should_fail() for _ in range(1000))
                self.assertEqual(failures, expected)

    def test_the_sequence_is_identical_across_instances(self) -> None:
        # This is the property that makes a canary rollback test reproducible.
        first = FaultInjector(FaultProfile(error_ratio=0.37))
        second = FaultInjector(FaultProfile(error_ratio=0.37))
        self.assertEqual(
            [first.should_fail() for _ in range(200)],
            [second.should_fail() for _ in range(200)],
        )

    def test_latency_is_applied_on_schedule_only(self) -> None:
        slept: list[float] = []
        injector = FaultInjector(
            FaultProfile(latency_seconds=0.25, latency_ratio=0.5),
            sleep=slept.append,
        )
        added = [injector.apply_latency() for _ in range(4)]
        self.assertEqual(added, [0.0, 0.25, 0.0, 0.25])
        self.assertEqual(slept, [0.25, 0.25])

    def test_latency_needs_both_a_duration_and_a_ratio(self) -> None:
        self.assertEqual(
            FaultInjector(FaultProfile(latency_seconds=1.0)).apply_latency(), 0.0
        )
        self.assertEqual(
            FaultInjector(FaultProfile(latency_ratio=1.0)).apply_latency(), 0.0
        )

    def test_a_profile_is_only_enabled_when_it_would_do_something(self) -> None:
        self.assertFalse(FaultProfile().enabled)
        self.assertFalse(FaultProfile(latency_seconds=2.0).enabled)
        self.assertTrue(FaultProfile(error_ratio=0.01).enabled)
        self.assertTrue(FaultProfile(latency_seconds=2.0, latency_ratio=0.1).enabled)

    def test_the_profile_is_read_from_the_environment(self) -> None:
        profile = FaultProfile.from_environment(
            environ={
                "FAULT_ERROR_RATIO": "0.4",
                "FAULT_LATENCY_SECONDS": "1.5",
                "FAULT_LATENCY_RATIO": "0.2",
            }
        )
        self.assertEqual(profile.error_ratio, 0.4)
        self.assertEqual(profile.latency_seconds, 1.5)

    def test_canary_faults_do_not_apply_to_stable_pods(self) -> None:
        stable = FaultProfile.from_environment(
            environ={"POD_ROLE": "stable", "CANARY_FAULT_ERROR_RATIO": "1"}
        )
        canary = FaultProfile.from_environment(
            environ={"POD_ROLE": "canary", "CANARY_FAULT_ERROR_RATIO": "1"}
        )
        self.assertEqual(stable.error_ratio, 0.0)
        self.assertEqual(canary.error_ratio, 1.0)

    def test_an_out_of_range_ratio_is_rejected_at_startup(self) -> None:
        from platform_common.config import ConfigError

        with self.assertRaises(ConfigError):
            FaultProfile.from_environment(environ={"FAULT_ERROR_RATIO": "1.5"})


class InjectedFaultBehaviourTests(unittest.TestCase):
    def test_injected_faults_produce_500s_and_are_labelled(self) -> None:
        app, _ = make_app(FaultInjector(FaultProfile(error_ratio=1.0)))
        response = post(app, "/api/orders", {"item": "widget"})
        self.assertEqual(response.status, 500)

        rendered = app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('api_faults_injected_total{environment="dev",kind="error"', rendered)

    def test_an_injected_fault_does_not_create_an_order(self) -> None:
        app, _ = make_app(FaultInjector(FaultProfile(error_ratio=1.0)))
        post(app, "/api/orders", {"item": "widget"})
        self.assertEqual(json.loads(app.handle("GET", "/api/stats", {}).body)["orders"], 0)

    def test_the_span_records_that_the_failure_was_injected(self) -> None:
        app, exporter = make_app(FaultInjector(FaultProfile(error_ratio=1.0)))
        post(app, "/api/orders", {"item": "widget"})
        span = next(s for s in exporter.finished() if s.name == "POST /api/orders")
        self.assertTrue(span.attributes.get("fault.injected"))
        self.assertEqual(span.status_code, tracing.StatusCode.ERROR)


class TracePropagationTests(unittest.TestCase):
    def test_the_queued_task_carries_the_request_trace(self) -> None:
        # Without this the worker's span starts a new trace and the queued half
        # of the request disappears from the trace view.
        app, exporter = make_app()
        incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        post(app, "/api/orders", {"item": "widget"}, {"traceparent": incoming})

        task = json.loads(app.handle("POST", "/internal/tasks/claim", {}).body)["task"]
        carried = tracing.parse_traceparent(task["traceparent"])
        assert carried is not None
        self.assertEqual(carried.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")

    def test_the_order_records_its_originating_trace_id(self) -> None:
        app, _ = make_app()
        incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        created = json.loads(
            post(app, "/api/orders", {"item": "w"}, {"traceparent": incoming}).body
        )
        self.assertEqual(created["trace_id"], "4bf92f3577b34da6a3ce929d0e0e4736")


class StoreTests(unittest.TestCase):
    def test_a_full_queue_raises_rather_than_growing_without_bound(self) -> None:
        store = OrderStore(max_queue_depth=2)
        store.create_order("a", 1)
        store.create_order("b", 1)
        with self.assertRaises(QueueFull):
            store.create_order("c", 1)

    def test_a_full_queue_surfaces_as_503_not_500(self) -> None:
        app, _ = make_app(store=OrderStore(max_queue_depth=1))
        self.assertEqual(post(app, "/api/orders", {"item": "a"}).status, 201)
        self.assertEqual(post(app, "/api/orders", {"item": "b"}).status, 503)

    def test_an_abandoned_claim_is_requeued(self) -> None:
        # Models a worker evicted mid-task: the order must not be stranded.
        clock = [1000.0]
        store = OrderStore(claim_timeout=30.0, clock=lambda: clock[0])
        store.create_order("widget", 1)

        first = store.claim_task()
        assert first is not None
        self.assertIsNone(store.claim_task())

        clock[0] += 31.0
        second = store.claim_task()
        assert second is not None
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(second.attempts, 2)
        self.assertEqual(store.requeued_count, 1)

    def test_a_claim_within_the_timeout_is_not_requeued(self) -> None:
        clock = [1000.0]
        store = OrderStore(claim_timeout=30.0, clock=lambda: clock[0])
        store.create_order("widget", 1)
        store.claim_task()
        clock[0] += 29.0
        self.assertIsNone(store.claim_task())
        self.assertEqual(store.requeued_count, 0)

    def test_terminal_orders_are_evicted_once_the_bound_is_exceeded(self) -> None:
        store = OrderStore(max_orders=3, max_queue_depth=100)
        for index in range(3):
            order, task = store.create_order(f"item-{index}", 1)
            store.claim_task()
            store.complete_task(task.task_id)
        store.create_order("newest", 1)
        self.assertLessEqual(store.stats()["orders"], 3)

    def test_stats_report_queue_depth_and_outcomes(self) -> None:
        store = OrderStore()
        _, task = store.create_order("widget", 1)
        self.assertEqual(store.stats()["pending_tasks"], 1)
        store.claim_task()
        self.assertEqual(store.stats()["inflight_tasks"], 1)
        store.complete_task(task.task_id)
        stats = store.stats()
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["orders_" + OrderStatus.COMPLETED], 1)


class OperationalSurfaceTests(unittest.TestCase):
    def test_the_api_stays_ready_when_the_queue_is_backed_up(self) -> None:
        # A worker outage must show as a backlog and an SLO burn, not as an API
        # that removes itself and converts partial degradation into an outage.
        app, _ = make_app()
        for index in range(20):
            post(app, "/api/orders", {"item": f"item-{index}"})
        self.assertEqual(app.handle("GET", "/readyz", {}).status, 200)

    def test_queue_depth_is_exposed_as_a_gauge(self) -> None:
        app, _ = make_app()
        post(app, "/api/orders", {"item": "widget"})
        rendered = app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn("api_task_queue_depth", rendered)


if __name__ == "__main__":
    unittest.main()
