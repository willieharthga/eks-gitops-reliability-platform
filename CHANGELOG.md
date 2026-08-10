# Changelog

All notable changes to this project will be documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project intends to use [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- M4 digest-only environment promotion: `scripts/promote.py` writes overlay
  pins from a build manifest or from another environment, records history, and
  never talks to a cluster.
- M4 production isolation. Production cannot be promoted from a build
  manifest, cannot copy bootstrap tags, and requires `--confirm production`.
- M4 rollback to a previously recorded digest set, with the same production
  confirmation rule.
- M4 pull-request gate: CI fails when production pins change unless the body
  contains `PROMOTION-CONFIRM: production`.
- M4 contract tests covering happy paths, ten-plus rejection cases, workflow
  isolation from kubectl, and overlay digest-or-bootstrap homogeneity.
- M4 documentation: promotion guide, failed-promotion and rollback runbooks,
  and a pull request template that carries the confirmation trailer.
- M3 declarative Argo CD delivery: a pinned installation, an app-of-apps root
  Application that manages its own manifest, and an ApplicationSet generating
  one Application per environment.
- M3 scoped AppProjects. `workloads` is confined to one namespace and the seven
  kinds an application owns, blacklists `Secret`, and grants no roles;
  `platform` holds the privileged, cluster-scoped path and only the root.
- M3 Kustomize base and `dev`/`production` overlays for the four services, with
  restricted-profile pod hardening and settings delivered through a generated
  ConfigMap so a configuration change rolls the pods.
- M3 `scripts/gitops.py`: `configure`, idempotent `bootstrap`, `status`,
  `wait`, and `uninstall`. Bootstrap registers `gitops-prod` by generating a
  cluster Secret from the local kubeconfig, which never reaches the working
  tree.
- M3 `scripts/drift.py` and `make drift-test`, the acceptance test for the
  GitOps claim: it scales a Deployment and deletes a Service out of band and
  fails unless Argo CD reverts both, recording measured heal times.
- M3 manifest contract tests covering kustomization integrity, project
  guardrails, cluster references, overlay parity, and pod hardening.
- M3 documentation: a GitOps delivery guide, an out-of-sync runbook, and
  ADR 0005 on why the Argo CD installation is not self-managed.
- M2 synthetic workload: `frontend`, `api`, `worker`, and `load-generator`,
  built on the Python standard library (ADR 0004).
- M2 shared service runtime providing Prometheus exposition, W3C trace context
  propagation across the queue boundary, OTLP/JSON span export, structured
  logs correlated with traces, and separated liveness/readiness semantics.
- M2 deterministic fault injection, which gives the planned M5 canary rollback
  test a reproducible failure schedule instead of a random one.
- M2 single hardened, digest-pinned, non-root container build shared by all
  four services, plus `scripts/build.py` recording immutable digests.
- M2 supply-chain workflow producing SBOM and provenance attestations and
  failing when they are missing.
- M2 vulnerability gate (`scripts/scan.py`) that blocks fixable HIGH and
  CRITICAL findings, records unfixed findings for review, and fails closed when
  the scanner is unavailable.
- M1 local-first repository foundation.
- Idempotent lifecycle automation for two kind clusters and one local registry.
- Credential-free repository verification, pre-commit checks, and GitHub Actions.
- Architecture, threat model, operational readiness, runbook, and three initial
  architecture decision records.

### Changed

- ADR 0003 moves from "accepted for planned M2-M4 implementation" to
  implemented; promotion and rollback are Git changes, and CI cannot apply
  them.
- ADR 0002 moves from "accepted for planned M3 implementation" to implemented;
  its ownership boundary is now enforced by the projects and bootstrap above.
- `requirements-dev.txt` adds PyYAML, used only by the manifest contract tests.
  The services themselves still import nothing outside the standard library.
