# ADR 0001: Establish the Platform on kind Before EKS

- Status: Accepted
- Date: 2026-09-02

## Context

The platform needs two environments in which later milestones can prove GitOps
reconciliation, immutable promotion, policy, telemetry, rollback, and failure
behavior. Starting with EKS would introduce cloud credentials, cost, quotas,
network dependencies, longer feedback loops, and teardown risk before these
contracts are stable.

A local environment cannot prove EKS control-plane behavior, AWS IAM and
network integration, regional failure handling, or production scale. It can
provide a fast and reviewable substrate for most platform workflow tests.

## Decision

M1 uses two single-node kind clusters named `gitops-dev` and `gitops-prod` and a
local registry named `gitops-registry`.

- The host reaches the registry at `localhost:5001`.
- kind nodes reach it at `gitops-registry:5000` on the Docker network.
- Lifecycle commands are `make up`, `make status`, `make smoke`, and
  `make down`.
- `make verify` remains credential-free and does not require Docker or create
  runtime resources.
- Local lifecycle integration exercises create twice, smoke, and cleanup twice.
- The local path remains supported after optional EKS infrastructure is added.

M1 uses no AWS credentials and creates no cloud resources.

## Consequences

### Benefits

- Reviewers get a short feedback loop and bounded cleanup.
- CI can exercise cluster lifecycle without an AWS account.
- Development and production are separate Kubernetes API/reconciliation
  domains.
- Later features can stabilize their interfaces before cloud-specific work.

### Costs and limitations

- Both clusters and the registry share one workstation and Docker daemon.
- Logical environment separation does not demonstrate physical, account, or
  regional isolation.
- kind networking, storage, load balancing, IAM, and upgrades differ from EKS.
- Local performance measurements cannot be generalized to AWS or production.
- The workstation is a single failure and trust domain.

These limitations must remain visible in the
[architecture](../architecture.md) and root [README](../../README.md).

## Alternatives considered

- **EKS first:** rejected for M1 because it makes credentials, cost, and cleanup
  prerequisites for the basic reviewer path.
- **One local cluster with namespaces:** rejected because separate clusters make
  the environment and reconciliation boundary explicit.
- **k3d or minikube:** viable, but kind is sufficiently small, scriptable, and
  aligned with disposable multi-cluster testing.
- **vcluster from the start:** deferred; it adds a control layer that M1 does not
  need. It may be evaluated for later scale experiments.

## Revisit when

Revisit the decision if a required behavior cannot be represented honestly on
kind, local lifecycle time becomes impractical, or the EKS milestone identifies
a platform contract that must differ. Such a change must preserve a
credential-free verification path or explicitly supersede this ADR.
