"""Tests for the shared HTTP application behavior.

These drive `Application.handle` directly rather than starting a socket
server: the routing, metric, and trace behavior under test is the same, and the
tests stay fast and usable in an environment with no network.
"""

from __future__ import annotations

import json
import unittest

import _bootstrap  # noqa: F401
from platform_common import httpapp, metrics, tracing
from platform_common.version import BuildInfo

BUILD = BuildInfo(
    service="test",
    version="1.2.3",
    revision="abc123",
    image_digest="sha256:" + "0" * 64,
    environment="dev",
    instance="test-0",
)


def make_app() -> tuple[httpapp.Application, tracing.InMemorySpanExporter]:
    exporter = tracing.InMemorySpanExporter()
    app = httpapp.Application(BUILD, tracer=tracing.Tracer("test", exporter))
    return app, exporter


class BuiltinEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.exporter = make_app()

    def test_healthz_is_ok(self) -> None:
        response = self.app.handle("GET", "/healthz", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["status"], "ok")

    def test_readyz_reports_failing_checks_with_503(self) -> None:
        self.app.readiness.add("upstream", lambda: False)
        response = self.app.handle("GET", "/readyz", {})
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["checks"]["upstream"], "fail")

    def test_readyz_is_200_when_every_check_passes(self) -> None:
        self.app.readiness.add("upstream", lambda: True)
        self.assertEqual(self.app.handle("GET", "/readyz", {}).status, 200)

    def test_a_raising_check_fails_readiness_rather_than_the_endpoint(self) -> None:
        def broken() -> bool:
            raise RuntimeError("cannot evaluate")

        self.app.readiness.add("broken", broken)
        response = self.app.handle("GET", "/readyz", {})
        self.assertEqual(response.status, 503)
        self.assertIn("error", json.loads(response.body)["checks"]["broken"])

    def test_draining_fails_readiness_but_not_liveness(self) -> None:
        # Conflating the two turns a graceful shutdown into a restart loop.
        self.app.readiness.start_draining()
        self.assertEqual(self.app.handle("GET", "/readyz", {}).status, 503)
        self.assertEqual(self.app.handle("GET", "/healthz", {}).status, 200)

    def test_metrics_uses_the_prometheus_content_type(self) -> None:
        response = self.app.handle("GET", "/metrics", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, metrics.CONTENT_TYPE)

    def test_buildz_reports_the_running_artifact(self) -> None:
        payload = json.loads(self.app.handle("GET", "/buildz", {}).body)
        self.assertEqual(payload["version"], "1.2.3")
        self.assertEqual(payload["image_digest"], BUILD.image_digest)
        self.assertTrue(payload["traceable"])

    def test_build_info_metric_carries_revision_and_digest(self) -> None:
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('revision="abc123"', rendered)
        self.assertIn("service_build_info", rendered)


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.exporter = make_app()
        self.app.add_route(
            "GET",
            "/items/{item_id}",
            lambda request: httpapp.Response.json({"id": request.path_params["item_id"]}),
        )

    def test_path_parameters_are_extracted_and_unquoted(self) -> None:
        response = self.app.handle("GET", "/items/a%20b", {})
        self.assertEqual(json.loads(response.body)["id"], "a b")

    def test_unknown_path_is_404(self) -> None:
        self.assertEqual(self.app.handle("GET", "/nope", {}).status, 404)

    def test_known_path_with_wrong_method_is_405(self) -> None:
        self.assertEqual(self.app.handle("POST", "/items/1", {}).status, 405)

    def test_query_string_is_parsed_and_excluded_from_the_path(self) -> None:
        captured: dict = {}

        def handler(request: httpapp.Request) -> httpapp.Response:
            captured["limit"] = request.query_one("limit")
            captured["path"] = request.path
            return httpapp.Response.empty()

        self.app.add_route("GET", "/search", handler)
        self.app.handle("GET", "/search?limit=10&x=1", {})
        self.assertEqual(captured["limit"], "10")
        self.assertEqual(captured["path"], "/search")

    def test_templates_that_match_the_same_paths_are_rejected(self) -> None:
        # Different parameter names do not make the routes distinct.
        with self.assertRaises(ValueError):
            self.app.add_route("GET", "/items/{other}", lambda r: httpapp.Response.empty())

    def test_a_literal_segment_wins_over_a_parameter(self) -> None:
        self.app.add_route(
            "GET", "/items/new", lambda request: httpapp.Response.json({"id": "literal"})
        )
        response = self.app.handle("GET", "/items/new", {})
        self.assertEqual(json.loads(response.body)["id"], "literal")

    def test_the_parameter_route_still_matches_other_values(self) -> None:
        self.app.add_route(
            "GET", "/items/new", lambda request: httpapp.Response.json({"id": "literal"})
        )
        response = self.app.handle("GET", "/items/42", {})
        self.assertEqual(json.loads(response.body)["id"], "42")

    def test_route_label_uses_the_template_not_the_concrete_path(self) -> None:
        # Otherwise every order id becomes its own time series and the
        # Prometheus instance falls over during a load test.
        for item in range(5):
            self.app.handle("GET", f"/items/{item}", {})
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('route="/items/{item_id}"', rendered)
        self.assertNotIn('route="/items/3"', rendered)

    def test_unmatched_requests_share_one_metric_label(self) -> None:
        for path in ("/a", "/b", "/c"):
            self.app.handle("GET", path, {})
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('route="unmatched"', rendered)


class ErrorHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.exporter = make_app()

    def test_http_error_becomes_a_json_response(self) -> None:
        def handler(request: httpapp.Request) -> httpapp.Response:
            raise httpapp.HTTPError(422, "bad input", field="item")

        self.app.add_route("POST", "/x", handler)
        response = self.app.handle("POST", "/x", {})
        payload = json.loads(response.body)
        self.assertEqual(response.status, 422)
        self.assertEqual(payload["error"], "bad input")
        self.assertEqual(payload["field"], "item")

    def test_unexpected_exception_becomes_500_and_does_not_escape(self) -> None:
        def handler(request: httpapp.Request) -> httpapp.Response:
            raise RuntimeError("internal detail that must not leak")

        self.app.add_route("GET", "/boom", handler)
        with self.assertLogs("test", level="ERROR"):
            response = self.app.handle("GET", "/boom", {})
        self.assertEqual(response.status, 500)
        self.assertNotIn(b"internal detail", response.body)

    def test_a_500_is_counted(self) -> None:
        def handler(request: httpapp.Request) -> httpapp.Response:
            raise RuntimeError("boom")

        self.app.add_route("GET", "/boom", handler)
        with self.assertLogs("test", level="ERROR"):
            self.app.handle("GET", "/boom", {})
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('status="500"', rendered)

    def test_malformed_json_body_is_a_400(self) -> None:
        self.app.add_route("POST", "/x", lambda request: httpapp.Response.json(request.json()))
        self.assertEqual(self.app.handle("POST", "/x", {}, b"{not json").status, 400)

    def test_missing_required_body_is_a_400(self) -> None:
        self.app.add_route("POST", "/x", lambda request: httpapp.Response.json(request.json()))
        self.assertEqual(self.app.handle("POST", "/x", {}).status, 400)

    def test_optional_body_may_be_absent(self) -> None:
        self.app.add_route(
            "POST",
            "/x",
            lambda request: httpapp.Response.json({"body": request.json(required=False)}),
        )
        response = self.app.handle("POST", "/x", {})
        self.assertEqual(response.status, 200)
        self.assertIsNone(json.loads(response.body)["body"])


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.exporter = make_app()
        self.app.add_route("GET", "/ok", lambda request: httpapp.Response.empty(200))

    def test_a_server_span_is_recorded_per_request(self) -> None:
        self.app.handle("GET", "/ok", {})
        spans = [s for s in self.exporter.finished() if s.name == "GET /ok"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].kind, tracing.SpanKind.SERVER)
        self.assertEqual(spans[0].attributes["http.response.status_code"], 200)

    def test_inbound_traceparent_continues_the_caller_trace(self) -> None:
        incoming = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        self.app.handle("GET", "/ok", {"traceparent": incoming})
        span = next(s for s in self.exporter.finished() if s.name == "GET /ok")
        self.assertEqual(span.trace_id, "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(span.parent_span_id, "00f067aa0ba902b7")

    def test_a_garbage_traceparent_starts_a_new_trace_instead_of_failing(self) -> None:
        response = self.app.handle("GET", "/ok", {"traceparent": "garbage"})
        self.assertEqual(response.status, 200)
        span = next(s for s in self.exporter.finished() if s.name == "GET /ok")
        self.assertTrue(span.context.valid)
        self.assertEqual(span.parent_span_id, "")

    def test_requests_and_duration_are_recorded(self) -> None:
        self.app.handle("GET", "/ok", {})
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('http_requests_total{environment="dev",method="GET",', rendered)
        self.assertIn('route="/ok",service="test",status="200"', rendered)
        self.assertIn("http_request_duration_seconds_bucket", rendered)

    def test_in_flight_returns_to_zero_even_after_a_failure(self) -> None:
        def handler(request: httpapp.Request) -> httpapp.Response:
            raise RuntimeError("boom")

        self.app.add_route("GET", "/boom", handler)
        with self.assertLogs("test", level="ERROR"):
            self.app.handle("GET", "/boom", {})
        self.assertEqual(self.app.requests_in_flight.value(), 0)

    def test_service_labels_are_applied_to_every_series(self) -> None:
        self.app.handle("GET", "/ok", {})
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn('service="test"', rendered)
        self.assertIn('environment="dev"', rendered)


class BuildInfoTests(unittest.TestCase):
    def test_an_unpinned_build_is_reported_as_untraceable(self) -> None:
        build = BuildInfo(service="test")
        self.assertFalse(build.is_traceable)

    def test_reading_build_identity_from_the_environment(self) -> None:
        from platform_common import version

        build = version.from_environment(
            "api",
            environ={
                "SERVICE_VERSION": "9.9.9",
                "SERVICE_REVISION": "deadbeef",
                "SERVICE_IMAGE_DIGEST": "sha256:" + "1" * 64,
                "PLATFORM_ENVIRONMENT": "production",
            },
        )
        self.assertEqual(build.version, "9.9.9")
        self.assertEqual(build.environment, "production")
        self.assertTrue(build.is_traceable)


if __name__ == "__main__":
    unittest.main()
