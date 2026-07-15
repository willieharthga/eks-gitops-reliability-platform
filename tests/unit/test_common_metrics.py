"""Tests for the Prometheus exposition implementation.

The exposition format is a contract with Prometheus, not an internal detail:
a malformed histogram silently breaks every latency SLO and canary analysis
that depends on it, so the rendered text is asserted directly.
"""

from __future__ import annotations

import math
import threading
import unittest

import _bootstrap  # noqa: F401  (installs the import path)
from platform_common import metrics


class CounterTests(unittest.TestCase):
    def test_renders_help_type_and_value(self) -> None:
        registry = metrics.Registry()
        counter = registry.counter("things_total", "Total things.")
        counter.inc()
        counter.inc(2)

        self.assertEqual(
            registry.render(),
            "# HELP things_total Total things.\n"
            "# TYPE things_total counter\n"
            "things_total 3\n",
        )

    def test_labels_are_sorted_and_escaped(self) -> None:
        registry = metrics.Registry()
        counter = registry.counter("requests_total", "Requests.", ("route", "status"))
        counter.inc(route='/a"b\\c', status="200")

        self.assertIn(
            'requests_total{route="/a\\"b\\\\c",status="200"} 1', registry.render()
        )

    def test_constant_labels_are_merged_into_every_series(self) -> None:
        registry = metrics.Registry(constant_labels={"service": "api"})
        registry.counter("a_total", "A.", ("route",)).inc(route="/x")

        self.assertIn('a_total{route="/x",service="api"} 1', registry.render())

    def test_negative_increment_is_rejected(self) -> None:
        counter = metrics.Registry().counter("a_total", "A.")
        with self.assertRaises(ValueError):
            counter.inc(-1)

    def test_missing_or_unexpected_labels_are_rejected(self) -> None:
        counter = metrics.Registry().counter("a_total", "A.", ("route",))
        with self.assertRaises(ValueError):
            counter.inc()
        with self.assertRaises(ValueError):
            counter.inc(route="/x", extra="y")

    def test_concurrent_increments_are_not_lost(self) -> None:
        counter = metrics.Registry().counter("a_total", "A.")

        def bump() -> None:
            for _ in range(1000):
                counter.inc()

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(counter.value(), 8000)


class GaugeTests(unittest.TestCase):
    def test_set_inc_and_dec(self) -> None:
        gauge = metrics.Registry().gauge("g", "G.")
        gauge.set(5)
        gauge.inc(2)
        gauge.dec(3)
        self.assertEqual(gauge.value(), 4)

    def test_float_values_survive_rendering(self) -> None:
        registry = metrics.Registry()
        registry.gauge("g", "G.").set(1.5)
        self.assertIn("g 1.5", registry.render())


class HistogramTests(unittest.TestCase):
    def test_buckets_are_cumulative_and_include_inf(self) -> None:
        registry = metrics.Registry()
        histogram = registry.histogram("d_seconds", "D.", buckets=(1.0, 2.0))
        histogram.observe(0.5)
        histogram.observe(1.5)
        histogram.observe(9.0)

        rendered = registry.render()
        self.assertIn('d_seconds_bucket{le="1"} 1', rendered)
        self.assertIn('d_seconds_bucket{le="2"} 2', rendered)
        self.assertIn('d_seconds_bucket{le="+Inf"} 3', rendered)
        self.assertIn("d_seconds_count 3", rendered)
        self.assertIn("d_seconds_sum 11", rendered)

    def test_observation_exactly_on_a_boundary_is_included(self) -> None:
        # Prometheus buckets are "less than or equal"; an off-by-one here
        # shifts every latency percentile.
        histogram = metrics.Registry().histogram("d", "D.", buckets=(1.0,))
        histogram.observe(1.0)
        self.assertEqual(histogram.count(), 1)

    def test_bucket_bounds_must_be_sorted_and_unique(self) -> None:
        registry = metrics.Registry()
        with self.assertRaises(ValueError):
            registry.histogram("a", "A.", buckets=(2.0, 1.0))
        with self.assertRaises(ValueError):
            registry.histogram("b", "B.", buckets=(1.0, 1.0))

    def test_le_label_is_reserved(self) -> None:
        with self.assertRaises(ValueError):
            metrics.Registry().histogram("a", "A.", ("le",))

    def test_default_buckets_are_sorted_and_finite(self) -> None:
        self.assertEqual(
            list(metrics.DEFAULT_BUCKETS), sorted(metrics.DEFAULT_BUCKETS)
        )
        self.assertTrue(all(math.isfinite(b) for b in metrics.DEFAULT_BUCKETS))


class RegistryTests(unittest.TestCase):
    def test_duplicate_registration_is_rejected(self) -> None:
        registry = metrics.Registry()
        registry.counter("a_total", "A.")
        with self.assertRaises(ValueError):
            registry.counter("a_total", "A again.")

    def test_invalid_metric_and_label_names_are_rejected(self) -> None:
        registry = metrics.Registry()
        with self.assertRaises(ValueError):
            registry.counter("9bad", "Bad.")
        with self.assertRaises(ValueError):
            registry.counter("ok_total", "OK.", ("bad-label",))
        with self.assertRaises(ValueError):
            registry.counter("ok2_total", "OK.", ("__reserved",))

    def test_empty_registry_renders_nothing(self) -> None:
        self.assertEqual(metrics.Registry().render(), "")

    def test_output_ends_with_a_newline(self) -> None:
        registry = metrics.Registry()
        registry.counter("a_total", "A.").inc()
        self.assertTrue(registry.render().endswith("\n"))

    def test_help_text_newlines_are_escaped(self) -> None:
        registry = metrics.Registry()
        registry.counter("a_total", "line one\nline two")
        self.assertIn("# HELP a_total line one\\nline two\n", registry.render())


if __name__ == "__main__":
    unittest.main()
