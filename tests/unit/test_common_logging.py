"""Tests for structured logging and trace correlation.

The M6 acceptance criterion is that logs carry trace identifiers. That
correlation is produced here, so it is asserted here rather than left to a
manual dashboard inspection.
"""

from __future__ import annotations

import io
import json
import logging
import unittest

import _bootstrap  # noqa: F401
from platform_common import logging as platform_logging
from platform_common import tracing


class JSONLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.logger = platform_logging.configure(
            "test", level="DEBUG", static_fields={"service": "api"}, stream=self.stream
        )

    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    def records(self) -> list[dict]:
        return [
            json.loads(line)
            for line in self.stream.getvalue().splitlines()
            if line.strip()
        ]

    def test_each_record_is_one_json_object_on_one_line(self) -> None:
        self.logger.info("hello")
        self.logger.info("world")
        lines = self.stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)

    def test_core_fields_and_static_fields_are_present(self) -> None:
        self.logger.info("hello")
        record = self.records()[0]
        self.assertEqual(record["message"], "hello")
        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["logger"], "test")
        self.assertEqual(record["service"], "api")
        self.assertIn("timestamp", record)

    def test_extra_fields_are_included(self) -> None:
        self.logger.info("handled", extra={"http_status": 503})
        self.assertEqual(self.records()[0]["http_status"], 503)

    def test_trace_identifiers_are_attached_when_a_span_is_active(self) -> None:
        tracer = tracing.Tracer("api", tracing.InMemorySpanExporter())
        with tracer.span("op") as span:
            self.logger.info("inside")
        record = self.records()[0]
        self.assertEqual(record["trace_id"], span.trace_id)
        self.assertEqual(record["span_id"], span.span_id)

    def test_no_trace_identifiers_outside_a_span(self) -> None:
        self.logger.info("outside")
        self.assertNotIn("trace_id", self.records()[0])

    def test_exceptions_are_captured_as_text(self) -> None:
        try:
            raise RuntimeError("bang")
        except RuntimeError:
            self.logger.exception("failed")
        record = self.records()[0]
        self.assertEqual(record["level"], "ERROR")
        self.assertIn("RuntimeError: bang", record["exception"])

    def test_message_formatting_arguments_are_applied(self) -> None:
        self.logger.info("value is %s", 42)
        self.assertEqual(self.records()[0]["message"], "value is 42")

    def test_non_serialisable_values_do_not_raise(self) -> None:
        self.logger.info("obj", extra={"thing": object()})
        self.assertIn("object", self.records()[0]["thing"])

    def test_reconfiguring_does_not_duplicate_output(self) -> None:
        second = io.StringIO()
        logger = platform_logging.configure("test", stream=second)
        logger.info("once")
        self.assertEqual(len(second.getvalue().splitlines()), 1)

    def test_unknown_level_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            platform_logging.configure("test", level="LOUD", stream=self.stream)


if __name__ == "__main__":
    unittest.main()
