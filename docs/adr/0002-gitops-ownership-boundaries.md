# ADR 0002: Define GitOps Ownership Boundaries

- Status: Accepted; implemented in M3
- Date: 2026-09-02

## Context

Terraform, cluster lifecycle automation, Argo CD, application delivery
pipelines, and human operators can all modify Kubernetes-related state. If
their ownership overlaps, they can continuously overwrite one another, bypass
review, or make recovery ambiguous.

This ADR defines the contract. It was written before any Argo CD installation
existed; M3 implements it, and
[ADR 0005](0005-argo-cd-manages-everything-except-itself.md) settles the one
question left open here — whether Argo CD's own installation falls inside the
boundary. It does not.

## Decision

Ownership will be divided as follows:

| Owner | Owns | Must not own |
|---|---|---|
| Terraform or local lifecycle automation | Cluster infrastructure, networking prerequisites, registry/bootstrap prerequisites, and the initial GitOps bootstrap seam | Application releases or continuously reconciled workload manifests |
| Argo CD | Declared in-cluster platform add-ons, policies, namespaces, and workloads after bootstrap | Cloud infrastructure or artifact builds |
| CI | Build/test/scan/sign artifacts and propose reviewed configuration changes | Direct production deployment with kubectl |
| Environment configuration | Desired artifact digest and environment-specific values | Building or mutating the artifact |
| Human operator | Reviewed changes and documented break-glass recovery | Routine out-of-band mutation of managed resources |

Git will be the declared source of truth for resources owned by Argo CD.
Unauthorized manual changes to those resources must be detected and reconciled,
and M3 must include an automated test proving that behavior.

For the local M3 topology, Argo CD is expected to run in the development cluster
and manage both `gitops-dev` and `gitops-prod`. This is a convenience for a
single-host demo and creates an explicit coupling: loss of the development
cluster also removes the local GitOps control plane. It is not the recommended
production management topology. EKS design work must reassess control-plane
placement and recovery independently.

## Bootstrap and break-glass rules

- Bootstrap credentials are narrowly scoped and are not stored in Git.
- CI may render and validate manifests but does not call kubectl against the
  production environment as its delivery mechanism.
- Emergency manual mutation requires an incident record, expiry/owner, and a
  follow-up Git change or reconciliation plan.
- If telemetry needed for a rollout decision is unavailable, progressive
  delivery must not silently interpret missing data as success.
- Destruction of a cluster remains an infrastructure/lifecycle action, never an
  Argo application action.

## Consequences

- Desired state and drift responsibility are unambiguous.
- Promotion is auditable as a configuration review rather than an imperative
  deployment.
- Initial bootstrap needs a deliberately small exception before Argo can
  reconcile itself and other resources.
- Platform changes must coordinate infrastructure prerequisites and GitOps
  application ordering without assigning the same resource to both systems.
- Local Argo placement is a shared failure domain and must be described honestly
  in demonstrations.

## Alternatives considered

- **CI deploys with kubectl:** rejected because it bypasses continuous
  reconciliation and concentrates standing cluster credentials in CI.
- **Terraform owns every Kubernetes object:** rejected because it couples cloud
  infrastructure lifecycle to frequent application delivery.
- **Argo owns cloud and cluster infrastructure:** rejected for this project
  because cluster bootstrap and recovery would depend on the cluster being
  healthy.

## Validation required in M3

M3 is not complete until repository boundaries are documented, Argo projects
and credentials are scoped, bootstrap can be repeated, and a test shows that a
manual mutation of an Argo-owned resource is detected and restored.
