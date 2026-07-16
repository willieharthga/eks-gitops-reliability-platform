"""Tests for the synthetic traffic generator.

The generator is test equipment. If its composition varied between runs, a
canary comparison would be measuring the traffic rather than the deployment,
so determinism and pacing are asserted here.
"""

from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401
from loadgen_service.generator import TrafficPlan, TrafficProfile, sleep_until
from loadgen_service.main import LoadRunner, build_application
from platform_common import httpapp, tracing
from platform_common.version import BuildInfo

BUILD = BuildInfo(service="load-generator", version="1.0.0", environment="dev")


class StubTarget:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.status = 200
        self.available = True

    def __call__(self, upstream, method, url, payload=None, *, timeout=5.0):
        self.calls.append((method, url, payload))
        if not self.available:
            raise httpapp.HTTPError(502, "frontend unreachable")
        return self.status, {}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class CompositionTests(unittest.TestCase):
    def test_the_operation_sequence_is_identical_across_instances(self) -> None:
        profile = TrafficProfile(read_weight=3, write_weight=1)
        plan_a, plan_b = TrafficPlan(profile), TrafficPlan(profile)
        self.assertEqual(
            [plan_a.next_operation().kind for _ in range(40)],
            [plan_b.next_operation().kind for _ in range(40)],
        )

    def test_read_and_write_weights_are_honoured(self) -> None:
        plan = TrafficPlan(TrafficProfile(read_weight=3, write_weight=1))
        kinds = [plan.next_operation().kind for _ in range(40)]
        self.assertEqual(kinds.count("create"), 10)
        self.assertEqual(kinds.count("list"), 30)

    def test_a_write_only_profile_never_reads(self) -> None:
        plan = TrafficPlan(TrafficProfile(read_weight=0, write_weight=1))
        self.assertEqual(
            {plan.next_operation().kind for _ in range(10)}, {"create"}
        )

    def test_zero_total_weight_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TrafficProfile(read_weight=0, write_weight=0)

    def test_invalid_requests_appear_at_the_configured_ratio(self) -> None:
        plan = TrafficPlan(
            TrafficProfile(read_weight=0, write_weight=1, invalid_ratio=0.25)
        )
        kinds = [plan.next_operation().kind for _ in range(100)]
        self.assertEqual(kinds.count("create_invalid"), 25)

    def test_no_invalid_requests_by_default(self) -> None:
        plan = TrafficPlan(TrafficProfile(read_weight=0, write_weight=1))
        kinds = [plan.next_operation().kind for _ in range(50)]
        self.assertNotIn("create_invalid", kinds)

    def test_generated_orders_stay_within_the_configured_quantity(self) -> None:
        plan = TrafficPlan(
            TrafficProfile(read_weight=0, write_weight=1, max_quantity=3)
        )
        quantities = [plan.next_operation().payload["quantity"] for _ in range(30)]
        self.assertTrue(all(1 <= quantity <= 3 for quantity in quantities))

    def test_the_profile_is_read_from_the_environment(self) -> None:
        profile = TrafficProfile.from_environment(
            environ={"LOAD_RPS": "20", "LOAD_INVALID_RATIO": "0.1"}
        )
        self.assertEqual(profile.requests_per_second, 20.0)
        self.assertEqual(profile.invalid_ratio, 0.1)


class PacingTests(unittest.TestCase):
    def test_the_schedule_is_absolute_and_evenly_spaced(self) -> None:
        plan = TrafficPlan(TrafficProfile(requests_per_second=4.0))
        times = list(plan.schedule(100.0, count=4))
        self.assertEqual(times, [100.0, 100.25, 100.5, 100.75])

    def test_a_zero_rate_produces_no_schedule(self) -> None:
        plan = TrafficPlan(TrafficProfile(requests_per_second=0.0))
        self.assertEqual(list(plan.schedule(0.0, count=5)), [])

    def test_sleep_until_waits_for_a_future_deadline(self) -> None:
        clock = FakeClock()
        lag = sleep_until(2.0, now=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(clock.slept, [2.0])
        self.assertEqual(lag, 0.0)

    def test_a_missed_deadline_reports_lag_and_does_not_sleep(self) -> None:
        # Sleeping a fixed gap after each response would quietly reduce the
        # offered rate and hide the regression the test is meant to expose.
        clock = FakeClock()
        clock.now = 5.0
        lag = sleep_until(2.0, now=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(clock.slept, [])
        self.assertEqual(lag, 3.0)


class RunnerTests(unittest.TestCase):
    def make_runner(self, profile: TrafficProfile) -> tuple[LoadRunner, StubTarget, FakeClock]:
        app = build_application(
            build=BUILD, tracer=tracing.Tracer("load-generator", tracing.InMemorySpanExporter())
        )
        stub = StubTarget()
        app.call_json = stub  # type: ignore[method-assign]
        clock = FakeClock()
        runner = LoadRunner(
            app,
            TrafficPlan(profile),
            target_base_url="http://frontend:8080",
            now=clock.monotonic,
            sleep=clock.sleep,
        )
        return runner, stub, clock

    def test_writes_post_orders_and_reads_get_the_summary(self) -> None:
        runner, stub, _ = self.make_runner(
            TrafficProfile(requests_per_second=10.0, read_weight=1, write_weight=1)
        )
        runner.run_once()
        runner.run_once()

        self.assertEqual(stub.calls[0][0], "POST")
        self.assertEqual(stub.calls[0][1], "http://frontend:8080/orders")
        self.assertEqual(stub.calls[1][0], "GET")
        self.assertEqual(stub.calls[1][1], "http://frontend:8080/api/summary")

    def test_responses_are_counted_by_operation_and_status(self) -> None:
        runner, stub, _ = self.make_runner(
            TrafficProfile(requests_per_second=10.0, read_weight=0, write_weight=1)
        )
        stub.status = 500
        runner.run_once()
        self.assertEqual(runner.requests.value(operation="create", status="500"), 1)

    def test_an_unreachable_target_is_counted_as_status_zero(self) -> None:
        runner, stub, _ = self.make_runner(
            TrafficProfile(requests_per_second=10.0, read_weight=0, write_weight=1)
        )
        stub.available = False
        runner.run_once()
        self.assertEqual(runner.unreachable.value(), 1)
        self.assertEqual(runner.requests.value(operation="create", status="0"), 1)

    def test_a_zero_rate_keeps_the_process_alive_without_sending(self) -> None:
        runner, stub, _ = self.make_runner(TrafficProfile(requests_per_second=0.0))
        self.assertTrue(runner.run_once())
        self.assertEqual(stub.calls, [])

    def test_schedule_lag_is_exposed_as_a_gauge(self) -> None:
        runner, stub, clock = self.make_runner(
            TrafficProfile(requests_per_second=100.0, read_weight=0, write_weight=1)
        )
        clock.now += 10.0  # fall far behind the schedule
        runner.run_once()
        self.assertGreater(runner.lag.value(), 0)


if __name__ == "__main__":
    unittest.main()
