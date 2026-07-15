"""Tests for W3C trace context handling and OTLP encoding.

Trace propagation is the mechanism M6 relies on to link an HTTP request to the
queued work it caused. If a malformed header silently produced an invalid
context, traces would fragment in ways that are very hard to notice on a
dashboard, so the parser's rejection rules are pinned here.
"""

from __future__ import annotations

import json
import threading
import unittest

import _bootstrap  # noqa: F401
from platform_common import tracing

VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


class TraceparentParsingTests(unittest.TestCase):
    def test_valid_header_round_trips(self) -> None:
        context = tracing.parse_traceparent(VALID)
        assert context is not None
        self.assertEqual(context.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(context.span_id, "00f067aa0ba902b7")
        self.assertTrue(context.sampled)
        self.assertTrue(context.remote)
        self.assertEqual(context.to_traceparent(), VALID)

    def test_unsampled_flag_is_preserved(self) -> None:
        context = tracing.parse_traceparent(VALID[:-2] + "00")
        assert context is not None
        self.assertFalse(context.sampled)
        self.assertTrue(context.to_traceparent().endswith("-00"))

    def test_a_future_version_with_extra_fields_is_accepted(self) -> None:
        # The specification requires forward compatibility: a newer version may
        # append fields, and the first four keep their meaning.
        context = tracing.parse_traceparent(f"01-{VALID[3:]}-extra")
        self.assertIsNotNone(context)

    def test_unparsable_headers_are_treated_as_absent(self) -> None:
        cases = {
            "empty": "",
            "none": None,
            "too few fields": "00-abc-def",
            "all-zero trace id": "00-" + "0" * 32 + "-00f067aa0ba902b7-01",
            "all-zero span id": "00-4bf92f3577b34da6a3ce929d0e0e4736-" + "0" * 16 + "-01",
            "short trace id": "00-4bf92f-00f067aa0ba902b7-01",
            "non-hex trace id": "00-zzf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "uppercase hex": VALID.upper(),
            "forbidden version ff": "ff-" + VALID[3:],
            "extra field on version 00": VALID + "-extra",
            "non-hex flags": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-zz",
        }
        for label, header in cases.items():
            with self.subTest(case=label):
                self.assertIsNone(tracing.parse_traceparent(header))

    def test_generated_identifiers_are_valid(self) -> None:
        for _ in range(200):
            context = tracing.SpanContext(
                trace_id=tracing.new_trace_id(), span_id=tracing.new_span_id()
            )
            self.assertTrue(context.valid)


class TracerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exporter = tracing.InMemorySpanExporter()
        self.tracer = tracing.Tracer("test", self.exporter)

    def test_span_is_exported_once_ended(self) -> None:
        with self.tracer.span("work") as span:
            span.set_attribute("k", "v")
            self.assertEqual(self.exporter.finished(), [])
        finished = self.exporter.finished()
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].name, "work")
        self.assertIsNotNone(finished[0].end_time_ns)

    def test_nested_spans_share_a_trace_and_link_to_the_parent(self) -> None:
        with self.tracer.span("outer") as outer:
            with self.tracer.span("inner") as inner:
                self.assertEqual(inner.trace_id, outer.trace_id)
                self.assertEqual(inner.parent_span_id, outer.span_id)
                self.assertNotEqual(inner.span_id, outer.span_id)

    def test_current_span_is_restored_after_exit(self) -> None:
        self.assertIsNone(tracing.current_span())
        with self.tracer.span("outer") as outer:
            with self.tracer.span("inner"):
                pass
            self.assertIs(tracing.current_span(), outer)
        self.assertIsNone(tracing.current_span())

    def test_exception_marks_the_span_and_still_ends_it(self) -> None:
        with self.assertRaises(ValueError):
            with self.tracer.span("boom"):
                raise ValueError("kaboom")
        span = self.exporter.finished()[0]
        self.assertEqual(span.status_code, tracing.StatusCode.ERROR)
        self.assertEqual(span.attributes["exception.type"], "ValueError")
        self.assertIsNotNone(span.end_time_ns)

    def test_remote_parent_continues_the_incoming_trace(self) -> None:
        parent = tracing.parse_traceparent(VALID)
        with self.tracer.span("server", parent=parent) as span:
            self.assertEqual(span.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
            self.assertEqual(span.parent_span_id, "00f067aa0ba902b7")

    def test_inject_and_extract_round_trip(self) -> None:
        with self.tracer.span("client") as span:
            carrier: dict[str, str] = {}
            self.tracer.inject(carrier)
            self.assertEqual(
                carrier[tracing.TRACEPARENT_HEADER], span.context.to_traceparent()
            )
        extracted = self.tracer.extract(carrier)
        assert extracted is not None
        self.assertEqual(extracted.trace_id, span.trace_id)

    def test_extract_is_case_insensitive(self) -> None:
        context = self.tracer.extract({"TraceParent": VALID})
        self.assertIsNotNone(context)

    def test_inject_without_an_active_span_is_a_no_op(self) -> None:
        self.assertEqual(self.tracer.inject({}), {})

    def test_ending_a_span_twice_exports_once(self) -> None:
        span = self.tracer.start_span("once")
        span.end()
        span.end()
        self.assertEqual(len(self.exporter.finished()), 1)

    def test_spans_in_separate_threads_do_not_leak_context(self) -> None:
        observed: list[object] = []

        def worker() -> None:
            observed.append(tracing.current_span())

        with self.tracer.span("outer"):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()

        self.assertEqual(observed, [None])

    def test_unsampled_spans_are_not_exported(self) -> None:
        parent = tracing.parse_traceparent(VALID[:-2] + "00")
        with self.tracer.span("skipped", parent=parent):
            pass
        self.assertEqual(self.exporter.finished(), [])


class OTLPEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.exporter = tracing.OTLPSpanExporter.__new__(tracing.OTLPSpanExporter)
        self.exporter.resource_attributes = {"service.name": "api"}
        self.exporter.scope_name = "test"

    def test_ids_are_hex_strings_not_base64(self) -> None:
        # OTLP/JSON encodes most `bytes` fields as base64 but specifies hex for
        # traceId and spanId. Getting this wrong makes the collector drop spans.
        tracer = tracing.Tracer("api", tracing.InMemorySpanExporter())
        with tracer.span("op") as span:
            pass
        encoded = self.exporter.encode([span])
        payload = encoded["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

        self.assertEqual(payload["traceId"], span.trace_id)
        self.assertEqual(len(payload["traceId"]), 32)
        self.assertEqual(len(payload["spanId"]), 16)
        self.assertNotIn("=", payload["traceId"])

    def test_timestamps_are_strings_and_kind_is_numeric(self) -> None:
        tracer = tracing.Tracer("api", tracing.InMemorySpanExporter())
        with tracer.span("op", kind=tracing.SpanKind.SERVER) as span:
            pass
        payload = self.exporter.encode([span])["resourceSpans"][0]["scopeSpans"][0][
            "spans"
        ][0]

        self.assertIsInstance(payload["startTimeUnixNano"], str)
        self.assertIsInstance(payload["endTimeUnixNano"], str)
        self.assertEqual(payload["kind"], 2)

    def test_attribute_types_map_to_the_correct_any_value_field(self) -> None:
        tracer = tracing.Tracer("api", tracing.InMemorySpanExporter())
        with tracer.span("op") as span:
            span.set_attributes(
                {"s": "text", "i": 7, "f": 1.5, "b": True, "l": ["a", "b"]}
            )
        attributes = {
            entry["key"]: entry["value"]
            for entry in self.exporter.encode([span])["resourceSpans"][0][
                "scopeSpans"
            ][0]["spans"][0]["attributes"]
        }

        self.assertEqual(attributes["s"], {"stringValue": "text"})
        self.assertEqual(attributes["i"], {"intValue": "7"})
        self.assertEqual(attributes["f"], {"doubleValue": 1.5})
        self.assertEqual(attributes["b"], {"boolValue": True})
        self.assertEqual(
            attributes["l"],
            {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}},
        )

    def test_bool_is_not_encoded_as_int(self) -> None:
        # bool is a subclass of int in Python; checking order matters.
        self.assertEqual(tracing._encode_value(True), {"boolValue": True})

    def test_payload_is_json_serialisable(self) -> None:
        tracer = tracing.Tracer("api", tracing.InMemorySpanExporter())
        with tracer.span("op") as span:
            span.set_status(tracing.StatusCode.ERROR, "failed")
        json.dumps(self.exporter.encode([span]))

    def test_root_span_omits_parent_span_id(self) -> None:
        tracer = tracing.Tracer("api", tracing.InMemorySpanExporter())
        with tracer.span("root") as span:
            pass
        payload = self.exporter.encode([span])["resourceSpans"][0]["scopeSpans"][0][
            "spans"
        ][0]
        self.assertNotIn("parentSpanId", payload)


class ExporterBackpressureTests(unittest.TestCase):
    def test_a_full_queue_drops_spans_instead_of_blocking(self) -> None:
        # Telemetry backpressure must never become application backpressure.
        exporter = tracing.OTLPSpanExporter(
            "http://127.0.0.1:1/v1/traces", {}, max_queue_size=2, schedule_delay=3600
        )
        try:
            tracer = tracing.Tracer("api", exporter)
            for _ in range(10):
                with tracer.span("op"):
                    pass
            self.assertGreater(exporter.dropped_spans, 0)
        finally:
            exporter.shutdown(timeout=1)


if __name__ == "__main__":
    unittest.main()
