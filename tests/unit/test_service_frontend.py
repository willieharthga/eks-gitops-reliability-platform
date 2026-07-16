"""Tests for the frontend status page and its degraded behavior."""

from __future__ import annotations

import json
import unittest

import _bootstrap  # noqa: F401
from frontend_service import page
from frontend_service.main import build_application
from platform_common import httpapp, tracing
from platform_common.version import BuildInfo

BUILD = BuildInfo(
    service="frontend",
    version="2.0.0",
    revision="cafebabe",
    image_digest="sha256:" + "a" * 64,
    environment="production",
    instance="frontend-0",
)


class StubUpstream:
    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.stats: dict = {"orders": 0, "pending_tasks": 0}
        self.available = True
        self.create_response: tuple[int, object] = (201, {"order_id": "ord-000001"})
        self.calls: list[tuple[str, str]] = []

    def __call__(self, upstream, method, url, payload=None, *, timeout=5.0):
        self.calls.append((method, url))
        if not self.available:
            raise httpapp.HTTPError(502, "upstream api unreachable")
        if url.endswith("/orders") and method == "POST":
            return self.create_response
        if "/api/orders" in url:
            return 200, {"orders": self.orders, "count": len(self.orders)}
        if "/api/stats" in url:
            return 200, self.stats
        return 404, None


def make_app() -> tuple[httpapp.Application, StubUpstream]:
    app = build_application(
        api_base_url="http://api:8080",
        build=BUILD,
        tracer=tracing.Tracer("frontend", tracing.InMemorySpanExporter()),
    )
    stub = StubUpstream()
    app.call_json = stub  # type: ignore[method-assign]
    return app, stub


class StatusPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.stub = make_app()

    def test_the_page_reports_the_running_deployment(self) -> None:
        response = self.app.handle("GET", "/", {})
        body = response.body.decode()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html; charset=utf-8")
        self.assertIn("2.0.0", body)
        self.assertIn("production", body)
        self.assertIn("digest-pinned", body)

    def test_an_unpinned_build_is_flagged_on_the_page(self) -> None:
        rendered = page.render(
            build=BuildInfo(service="frontend").as_dict(),
            upstream_healthy=True,
            upstream_detail="ok",
            orders=[],
            stats={},
        )
        self.assertIn("unpinned build", rendered)

    def test_orders_are_listed(self) -> None:
        self.stub.orders = [
            {"order_id": "ord-000001", "item": "widget", "quantity": 2, "status": "completed"}
        ]
        body = self.app.handle("GET", "/", {}).body.decode()
        self.assertIn("ord-000001", body)
        self.assertIn("widget", body)

    def test_an_empty_order_list_renders_a_placeholder(self) -> None:
        self.assertIn("No orders yet", self.app.handle("GET", "/", {}).body.decode())


class DegradedBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app, self.stub = make_app()

    def test_the_page_still_renders_when_the_api_is_down(self) -> None:
        # Serving the page that explains the outage is more useful than
        # failing with a connection error.
        self.stub.available = False
        response = self.app.handle("GET", "/", {})
        self.assertEqual(response.status, 503)
        self.assertIn("unreachable", response.body.decode())

    def test_the_frontend_stays_ready_when_the_api_is_down(self) -> None:
        self.stub.available = False
        self.app.handle("GET", "/", {})
        self.assertEqual(self.app.handle("GET", "/readyz", {}).status, 200)

    def test_upstream_failures_are_counted_by_operation(self) -> None:
        self.stub.available = False
        self.app.handle("GET", "/", {})
        rendered = self.app.handle("GET", "/metrics", {}).body.decode()
        self.assertIn("frontend_upstream_failures_total", rendered)
        self.assertIn('operation="list_orders"', rendered)

    def test_summary_reports_the_upstream_state_as_json(self) -> None:
        self.stub.available = False
        response = self.app.handle("GET", "/api/summary", {})
        self.assertEqual(response.status, 503)
        self.assertFalse(json.loads(response.body)["upstream_healthy"])


class ProxyTests(unittest.TestCase):
    def test_order_creation_is_forwarded_and_the_status_preserved(self) -> None:
        app, stub = make_app()
        response = app.handle(
            "POST", "/orders", {}, json.dumps({"item": "widget"}).encode()
        )
        self.assertEqual(response.status, 201)
        self.assertIn(("POST", "http://api:8080/api/orders"), stub.calls)

    def test_an_upstream_rejection_is_passed_through_not_masked(self) -> None:
        app, stub = make_app()
        stub.create_response = (400, {"error": "item is required"})
        response = app.handle("POST", "/orders", {}, json.dumps({}).encode())
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)["error"], "item is required")


class EscapingTests(unittest.TestCase):
    def test_order_fields_are_html_escaped(self) -> None:
        # Order items come from request bodies. Rendering them unescaped would
        # make the demo frontend a stored-XSS example.
        rendered = page.render(
            build=BUILD.as_dict(),
            upstream_healthy=True,
            upstream_detail="ok",
            orders=[
                {
                    "order_id": "ord-1",
                    "item": "<script>alert('xss')</script>",
                    "quantity": 1,
                    "status": "pending",
                }
            ],
            stats={},
        )
        self.assertNotIn("<script>alert", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_the_upstream_detail_string_is_escaped(self) -> None:
        rendered = page.render(
            build=BUILD.as_dict(),
            upstream_healthy=False,
            upstream_detail="<img src=x onerror=alert(1)>",
            orders=[],
            stats={},
        )
        self.assertNotIn("<img src=x", rendered)

    def test_an_unknown_status_does_not_inject_a_css_class(self) -> None:
        rendered = page.render(
            build=BUILD.as_dict(),
            upstream_healthy=True,
            upstream_detail="ok",
            orders=[{"order_id": "o", "item": "i", "quantity": 1, "status": "weird"}],
            stats={},
        )
        self.assertIn("pill warn", rendered)


if __name__ == "__main__":
    unittest.main()
