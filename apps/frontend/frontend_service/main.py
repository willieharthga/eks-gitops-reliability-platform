"""Frontend service."""

from __future__ import annotations

import sys

from platform_common import config, httpapp, runtime, tracing, version

from . import page

SERVICE = "frontend"
UPSTREAM = "api"


def build_application(
    *,
    api_base_url: str = "http://api:8080",
    build: version.BuildInfo | None = None,
    tracer: tracing.Tracer | None = None,
    environ: config.Environ | None = None,
) -> httpapp.Application:
    build = build or version.from_environment(SERVICE, environ=environ)
    app = httpapp.Application(build, tracer=tracer)
    base = api_base_url.rstrip("/")

    upstream_failures = app.registry.counter(
        "frontend_upstream_failures_total",
        "Upstream API calls that did not return usable data.",
        ("operation",),
    )
    page_renders = app.registry.counter(
        "frontend_page_renders_total", "Status pages rendered, by upstream state.", ("upstream",)
    )

    def fetch(operation: str, path: str) -> tuple[bool, object]:
        try:
            status, body = app.call_json(UPSTREAM, "GET", f"{base}{path}", timeout=3.0)
        except httpapp.HTTPError:
            upstream_failures.inc(operation=operation)
            return False, None
        if status != 200:
            upstream_failures.inc(operation=operation)
            return False, body
        return True, body

    # The frontend stays ready when the API is down so that it can serve the
    # page that reports the API is down. Removing it from service would hide
    # the outage behind a connection error instead of explaining it.
    app.readiness.add("self", lambda: True)

    @app.route("GET", "/")
    def index(request: httpapp.Request) -> httpapp.Response:
        orders_ok, orders_body = fetch("list_orders", "/api/orders?limit=15")
        stats_ok, stats_body = fetch("stats", "/api/stats")

        healthy = orders_ok and stats_ok
        orders = []
        if isinstance(orders_body, dict):
            raw = orders_body.get("orders")
            if isinstance(raw, list):
                orders = [entry for entry in raw if isinstance(entry, dict)]
        stats = stats_body if isinstance(stats_body, dict) else {}
        detail = (
            f"{base} responded normally"
            if healthy
            else f"{base} did not return usable data"
        )
        page_renders.inc(upstream="healthy" if healthy else "degraded")
        if request.span is not None:
            request.span.set_attribute("upstream.healthy", healthy)

        return httpapp.Response.html(
            page.render(
                build=app.build.as_dict(),
                upstream_healthy=healthy,
                upstream_detail=detail,
                orders=orders,
                stats=stats,
            ),
            status=200 if healthy else 503,
        )

    @app.route("GET", "/api/summary")
    def summary(request: httpapp.Request) -> httpapp.Response:
        stats_ok, stats_body = fetch("stats", "/api/stats")
        return httpapp.Response.json(
            {
                "build": app.build.as_dict(),
                "upstream_healthy": stats_ok,
                "stats": stats_body if isinstance(stats_body, dict) else {},
            },
            status=200 if stats_ok else 503,
        )

    @app.route("POST", "/orders")
    def create_order(request: httpapp.Request) -> httpapp.Response:
        payload = request.json()
        status, body = app.call_json(
            UPSTREAM, "POST", f"{base}/api/orders", payload, timeout=5.0
        )
        if status >= 400:
            upstream_failures.inc(operation="create_order")
        return httpapp.Response.json(body if body is not None else {}, status=status)

    return app


def main() -> int:
    build, logger, tracer = runtime.configure_service(SERVICE)
    app = build_application(
        api_base_url=config.get_url("API_BASE_URL", "http://api:8080"),
        build=build,
        tracer=tracer,
    )
    return runtime.serve_http(app, logger, tracer)


if __name__ == "__main__":
    sys.exit(main())
