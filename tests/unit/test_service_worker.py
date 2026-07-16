"""Tests for the queue worker.

The worker is where the asynchronous half of a trace is stitched back onto the
request that caused it, so trace continuity is asserted rather than assumed.
Upstream calls are stubbed: these tests must run with no network.
"""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from platform_common import httpapp, tracing
from platform_common.version import BuildInfo
from worker_service.main import Worker, build_application
from worker_service.processor import ProcessingProfile, TaskProcessor

BUILD = BuildInfo(service="worker", version="1.0.0", environment="dev")
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class StubUpstream:
    """Records outbound calls and replays scripted responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.claim_responses: list[tuple[int, object]] = []
        self.complete_response: tuple[int, object] = (200, {})
        self.raise_on: set[str] = set()

    def __call__(self, upstream, method, url, payload=None, *, timeout=5.0):
        self.calls.append((method, url, payload))
        if "claim" in url:
            if "claim" in self.raise_on:
                raise httpapp.HTTPError(502, "upstream api unreachable")
            if self.claim_responses:
                return self.claim_responses.pop(0)
            return 204, None
        if "complete" in self.raise_on:
            raise httpapp.HTTPError(502, "upstream api unreachable")
        return self.complete_response


def make_worker(
    *, profile: ProcessingProfile | None = None
) -> tuple[Worker, StubUpstream, tracing.InMemorySpanExporter]:
    exporter = tracing.InMemorySpanExporter()
    app = build_application(build=BUILD, tracer=tracing.Tracer("worker", exporter))
    stub = StubUpstream()
    app.call_json = stub  # type: ignore[method-assign]
    processor = TaskProcessor(
        profile or ProcessingProfile(base_seconds=0, per_unit_seconds=0),
        sleep=lambda _seconds: None,
    )
    worker = Worker(app, processor, api_base_url="http://api:8080")
    return worker, stub, exporter


def task(**overrides) -> dict:
    payload = {
        "task_id": "tsk-000001",
        "order_id": "ord-000001",
        "item": "widget",
        "quantity": 2,
        "traceparent": TRACEPARENT,
        "attempts": 1,
    }
    payload.update(overrides)
    return payload


class ClaimLoopTests(unittest.TestCase):
    def test_an_empty_queue_reports_no_work(self) -> None:
        worker, stub, _ = make_worker()
        self.assertFalse(worker.run_once())
        self.assertEqual(worker.idle_polls.value(), 1)

    def test_a_claimed_task_is_processed_and_reported(self) -> None:
        worker, stub, _ = make_worker()
        stub.claim_responses = [(200, {"task": task()})]

        self.assertTrue(worker.run_once())

        methods_and_urls = [(method, url) for method, url, _ in stub.calls]
        self.assertEqual(
            methods_and_urls,
            [
                ("POST", "http://api:8080/internal/tasks/claim"),
                ("POST", "http://api:8080/internal/tasks/tsk-000001/complete"),
            ],
        )
        self.assertEqual(stub.calls[1][2]["succeeded"], True)
        self.assertEqual(worker.processed.value(outcome="succeeded"), 1)

    def test_a_task_configured_to_fail_is_reported_as_failed(self) -> None:
        worker, stub, _ = make_worker(
            profile=ProcessingProfile(
                base_seconds=0, per_unit_seconds=0, fail_on_item="poison"
            )
        )
        stub.claim_responses = [(200, {"task": task(item="poison")})]

        worker.run_once()

        self.assertEqual(stub.calls[1][2]["succeeded"], False)
        self.assertEqual(worker.processed.value(outcome="failed"), 1)

    def test_an_unreachable_api_is_counted_and_does_not_raise(self) -> None:
        worker, stub, _ = make_worker()
        stub.raise_on.add("claim")
        with self.assertLogs("worker", level="WARNING"):
            self.assertFalse(worker.run_once())
        self.assertEqual(worker.poll_errors.value(phase="claim"), 1)

    def test_a_failed_completion_is_counted_but_the_task_is_still_processed(self) -> None:
        # The API requeues unreported claims, so this delays an order rather
        # than losing it; the loop must keep running.
        worker, stub, _ = make_worker()
        stub.claim_responses = [(200, {"task": task()})]
        stub.raise_on.add("complete")

        with self.assertLogs("worker", level="WARNING"):
            self.assertTrue(worker.run_once())

        self.assertEqual(worker.processed.value(outcome="succeeded"), 1)
        self.assertEqual(worker.poll_errors.value(phase="complete"), 1)

    def test_a_non_200_completion_status_is_counted(self) -> None:
        worker, stub, _ = make_worker()
        stub.claim_responses = [(200, {"task": task()})]
        stub.complete_response = (404, {"error": "task is not in flight"})
        worker.run_once()
        self.assertEqual(worker.poll_errors.value(phase="complete"), 1)

    def test_a_malformed_claim_response_is_ignored_safely(self) -> None:
        worker, stub, _ = make_worker()
        stub.claim_responses = [(200, {"task": "not-an-object"})]
        self.assertFalse(worker.run_once())


class TraceContinuityTests(unittest.TestCase):
    def test_processing_continues_the_trace_carried_on_the_message(self) -> None:
        worker, stub, exporter = make_worker()
        stub.claim_responses = [(200, {"task": task()})]

        worker.run_once()

        span = next(s for s in exporter.finished() if s.name == "process task")
        self.assertEqual(span.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(span.parent_span_id, "00f067aa0ba902b7")
        self.assertEqual(span.kind, tracing.SpanKind.CONSUMER)
        self.assertEqual(worker.trace_continuity.value(continued="true"), 1)

    def test_a_task_without_a_trace_starts_a_new_one_and_is_counted(self) -> None:
        worker, stub, exporter = make_worker()
        stub.claim_responses = [(200, {"task": task(traceparent="")})]

        worker.run_once()

        span = next(s for s in exporter.finished() if s.name == "process task")
        self.assertTrue(span.context.valid)
        self.assertEqual(span.parent_span_id, "")
        self.assertEqual(worker.trace_continuity.value(continued="false"), 1)

    def test_a_failed_task_marks_its_span_as_an_error(self) -> None:
        worker, stub, exporter = make_worker(
            profile=ProcessingProfile(
                base_seconds=0, per_unit_seconds=0, fail_on_item="poison"
            )
        )
        stub.claim_responses = [(200, {"task": task(item="poison")})]
        worker.run_once()
        span = next(s for s in exporter.finished() if s.name == "process task")
        self.assertEqual(span.status_code, tracing.StatusCode.ERROR)


class ReadinessTests(unittest.TestCase):
    def test_the_worker_is_not_ready_before_its_first_successful_poll(self) -> None:
        worker, _, _ = make_worker()
        ready, checks = worker.app.readiness.evaluate()
        self.assertFalse(ready)
        self.assertEqual(checks["api_reachable"], "fail")

    def test_the_worker_becomes_ready_after_reaching_the_api(self) -> None:
        worker, stub, _ = make_worker()
        worker.run_once()
        ready, _ = worker.app.readiness.evaluate()
        self.assertTrue(ready)


class ProcessingProfileTests(unittest.TestCase):
    def test_duration_scales_with_quantity_and_is_capped(self) -> None:
        profile = ProcessingProfile(
            base_seconds=0.1, per_unit_seconds=0.05, max_seconds=1.0
        )
        self.assertAlmostEqual(profile.duration_for(0), 0.1)
        self.assertAlmostEqual(profile.duration_for(4), 0.3)
        self.assertAlmostEqual(profile.duration_for(1000), 1.0)

    def test_duration_is_deterministic(self) -> None:
        # M9's scaling evidence is only reproducible if the work is not random.
        profile = ProcessingProfile()
        self.assertEqual(
            [profile.duration_for(n) for n in range(10)],
            [profile.duration_for(n) for n in range(10)],
        )

    def test_the_profile_is_read_from_the_environment(self) -> None:
        profile = ProcessingProfile.from_environment(
            environ={"WORK_BASE_SECONDS": "0.2", "WORK_FAIL_ON_ITEM": "poison"}
        )
        self.assertEqual(profile.base_seconds, 0.2)
        self.assertEqual(profile.fail_on_item, "poison")

    def test_processing_reports_a_useful_detail(self) -> None:
        processor = TaskProcessor(
            ProcessingProfile(base_seconds=0, per_unit_seconds=0),
            sleep=lambda _s: None,
        )
        result = processor.process("widget", 3)
        self.assertTrue(result.succeeded)
        self.assertEqual(result.detail, "processed 3 x widget")


if __name__ == "__main__":
    unittest.main()
