# ADR 0004: Build the Sample Services on the Python Standard Library

- Status: Accepted and implemented in M2
- Date: 2026-09-02

## Context

M2 introduces four synthetic services whose purpose is to exercise the
platform: artifact promotion, canary analysis, telemetry correlation, admission
policy, and autoscaling. They are test equipment, not a product.

The obvious implementation uses a web framework plus the OpenTelemetry SDK and
a Prometheus client library. That choice has real costs in this specific
repository:

- Every added dependency is a vulnerability-scanning surface for images whose
  business logic is a few hundred lines. The security gate would then mostly
  report findings in code the platform demonstration does not need.
- The credential-free `make verify` path is the repository's central promise.
  Third-party packages would make it depend on a working package index, which
  is a network dependency a reviewer on a clean machine may not have.
- Telemetry emitted by a vendored SDK is difficult to assert precisely. The M6
  and M7 acceptance criteria require proving that a trace links an HTTP request
  to queued work and that logs carry trace identifiers. Owning the propagation
  code makes those assertions direct.

The competing concern is credibility: reimplementing an SDK badly would be
worse than depending on a good one, and nobody should reimplement OpenTelemetry.

## Decision

The four services and their shared `platform_common` package depend only on the
Python standard library.

The scope of what is implemented in-repo is deliberately narrow, and is limited
to the wire formats the platform actually depends on:

1. Prometheus text exposition for counters, gauges, and histograms.
2. W3C Trace Context `traceparent` parsing, generation, and propagation.
3. The OTLP/HTTP JSON span encoding accepted by the OpenTelemetry Collector.
4. A small HTTP router with health, readiness, metrics, and build endpoints.

These are published, stable, versioned specifications, not vendor internals.
Each is covered by tests that assert the encoded output directly, including the
cases that are easy to get wrong: cumulative histogram buckets, `le` boundary
inclusion, and the OTLP/JSON rule that `traceId` and `spanId` are hex strings
while other `bytes` fields are base64.

Telemetry export is best effort and bounded. A full export queue drops spans
and increments a counter; it never blocks a request. Losing observability must
degrade observability, not availability.

## Consequences

- Service images contain no third-party Python packages, so a scan finding is
  almost always about the base image and is fixed by rebasing.
- `make verify` runs the real service code offline, with no installation step.
- The platform, not the application, remains the subject of the repository.
- The implemented surface is small on purpose. Sampling strategies, metric
  exemplars, baggage, log/metric OTLP signals, and gRPC transport are **not**
  implemented, and no documentation may imply otherwise.
- If a future milestone needs a capability that is genuinely hard to implement
  correctly, the right response is to adopt the upstream SDK for that service
  and record a superseding ADR, not to grow a partial imitation of it.

## Alternatives considered

- **OpenTelemetry SDK plus a web framework.** Rejected for this repository's
  purpose, not on technical merit. It is the correct choice for a production
  service and the documentation says so.
- **Go services.** Rejected because the repository's tooling, verification
  path, and reviewer prerequisites are already Python, and a second toolchain
  would add build complexity without demonstrating anything new about the
  platform.
- **No instrumentation until M6.** Rejected because retrofitting trace context
  through an async queue boundary is exactly the part that tends to be done
  wrong, and it is cheaper to design it in at M2.

## Validation required

- M2 asserts the exposition format, trace propagation, and OTLP encoding in
  unit tests that run with no network.
- M6 must show a single trace spanning frontend, API, and worker, and log lines
  carrying the matching trace identifier, against a real collector. Until that
  evidence exists, the repository claims only that the encoding is correct, not
  that the pipeline is proven end to end.
