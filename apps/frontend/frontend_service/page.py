"""HTML rendering for the status page.

Every interpolated value is escaped. The page renders order items that
originate from request bodies, so treating them as trusted would make the demo
frontend a stored-XSS example in a repository that claims security guardrails.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from html import escape
from typing import Any

STYLES = """
:root {
  color-scheme: light dark;
  --bg: #0f1117;
  --panel: #181b24;
  --line: #262a36;
  --text: #e8eaf0;
  --muted: #9aa3b2;
  --ok: #3fb950;
  --warn: #d29922;
  --bad: #f85149;
  --accent: #58a6ff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 2rem 1.25rem 3rem;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 62rem; margin: 0 auto; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
.sub { color: var(--muted); margin: 0 0 1.75rem; font-size: .92rem; }
.grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); }
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: .6rem;
  padding: 1rem 1.1rem;
}
.card h2 {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .09em;
  color: var(--muted); margin: 0 0 .6rem; font-weight: 600;
}
dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: .3rem .9rem; }
dt { color: var(--muted); font-size: .85rem; }
dd { margin: 0; font-size: .85rem; word-break: break-all; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .82rem; }
.pill {
  display: inline-block; padding: .1rem .5rem; border-radius: 999px;
  font-size: .75rem; font-weight: 600; border: 1px solid transparent;
}
.pill.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 40%, transparent); }
.pill.bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 40%, transparent); }
.pill.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, transparent); }
table { width: 100%; border-collapse: collapse; margin-top: .35rem; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid var(--line); font-size: .84rem; }
th { color: var(--muted); font-weight: 600; font-size: .72rem; text-transform: uppercase; letter-spacing: .07em; }
tr:last-child td { border-bottom: none; }
.section { margin-top: 1.75rem; }
.empty { color: var(--muted); font-size: .86rem; padding: .6rem .5rem; }
.note { color: var(--muted); font-size: .8rem; margin-top: 1.75rem; border-top: 1px solid var(--line); padding-top: .9rem; }
"""

_STATUS_CLASS = {
    "completed": "ok",
    "pending": "warn",
    "processing": "warn",
    "failed": "bad",
}


def _definition_list(rows: Sequence[tuple[str, str]]) -> str:
    cells = "".join(
        f"<dt>{escape(label)}</dt><dd class='mono'>{escape(str(value))}</dd>"
        for label, value in rows
    )
    return f"<dl>{cells}</dl>"


def _orders_table(orders: Sequence[Mapping[str, Any]]) -> str:
    if not orders:
        return "<p class='empty'>No orders yet.</p>"
    rows = []
    for order in orders:
        status = str(order.get("status", "unknown"))
        pill = _STATUS_CLASS.get(status, "warn")
        rows.append(
            "<tr>"
            f"<td class='mono'>{escape(str(order.get('order_id', '')))}</td>"
            f"<td>{escape(str(order.get('item', '')))}</td>"
            f"<td>{escape(str(order.get('quantity', '')))}</td>"
            f"<td><span class='pill {pill}'>{escape(status)}</span></td>"
            f"<td class='mono'>{escape(str(order.get('trace_id', ''))[:16])}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Order</th><th>Item</th><th>Qty</th>"
        "<th>Status</th><th>Trace</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def render(
    *,
    build: Mapping[str, Any],
    upstream_healthy: bool,
    upstream_detail: str,
    orders: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
) -> str:
    """Render the full status page."""
    health_pill = (
        "<span class='pill ok'>reachable</span>"
        if upstream_healthy
        else "<span class='pill bad'>unreachable</span>"
    )
    traceable = bool(build.get("traceable"))
    build_pill = (
        "<span class='pill ok'>digest-pinned</span>"
        if traceable
        else "<span class='pill warn'>unpinned build</span>"
    )

    build_rows = [
        ("Service", build.get("service", "unknown")),
        ("Version", build.get("version", "unknown")),
        ("Environment", build.get("environment", "unknown")),
        ("Revision", str(build.get("revision", "unknown"))[:12]),
        ("Digest", str(build.get("image_digest", "unknown"))[:23]),
        ("Instance", build.get("instance", "unknown")),
    ]
    stats_rows = [
        ("Orders", stats.get("orders", 0)),
        ("Pending tasks", stats.get("pending_tasks", 0)),
        ("In flight", stats.get("inflight_tasks", 0)),
        ("Completed", stats.get("completed", 0)),
        ("Failed", stats.get("failed", 0)),
        ("Requeued", stats.get("requeued", 0)),
    ]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Platform status - {escape(str(build.get('environment', 'unknown')))}</title>
<style>{STYLES}</style>
</head>
<body>
<main>
  <h1>EKS GitOps Reliability Platform</h1>
  <p class="sub">Synthetic workload status page. All data on this page is generated by the platform's own load, not by real users.</p>

  <div class="grid">
    <section class="card">
      <h2>This deployment {build_pill}</h2>
      {_definition_list(build_rows)}
    </section>
    <section class="card">
      <h2>Upstream API {health_pill}</h2>
      <p class="mono" style="margin:.2rem 0 0;font-size:.82rem">{escape(upstream_detail)}</p>
    </section>
    <section class="card">
      <h2>Queue</h2>
      {_definition_list([(label, str(value)) for label, value in stats_rows])}
    </section>
  </div>

  <section class="section card">
    <h2>Recent orders</h2>
    {_orders_table(orders)}
  </section>

  <p class="note">
    Liveness at <code>/healthz</code>, readiness at <code>/readyz</code>,
    metrics at <code>/metrics</code>, build identity at <code>/buildz</code>.
  </p>
</main>
</body>
</html>
"""
