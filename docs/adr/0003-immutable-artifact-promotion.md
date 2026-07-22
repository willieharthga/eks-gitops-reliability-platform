# ADR 0003: Promote Immutable Artifact Digests

- Status: Accepted; implemented in M4
- Date: 2026-09-02

## Context

Rebuilding an application separately for each environment or promoting a
mutable image tag makes it impossible to prove that production received the
artifact tested earlier. Mutable references can change without a corresponding
configuration review and make rollback evidence ambiguous.

M1 contains no sample workload, build pipeline, or promotion automation. This
ADR constrains those implementations. M2 produces the digest, M3 establishes
GitOps reconciliation, and M4 implements promotion and rollback.

## Decision

Future application delivery will:

1. Build an artifact once for a source revision.
2. Run tests and security checks and produce SBOM/provenance evidence.
3. Publish the artifact under an immutable content digest.
4. Promote the same digest by changing environment configuration through a pull
   request.
5. Require explicit approval for the production configuration change.
6. Let Argo CD reconcile the approved desired state.
7. Roll back by restoring a previously known digest, not by rebuilding old
   source or retagging an image.

Human-facing semantic or commit tags may aid discovery, but Kubernetes workload
configuration must resolve to a digest for promoted releases. A pipeline may
open or update a configuration pull request; it must not push a deployment
directly to production with kubectl.

## Provenance boundary

Promotion metadata must link the source revision, build run, test result,
vulnerability decision, SBOM/provenance, digest, configuration change, and
environment. Signing and verification details are implemented in later
milestones and must be documented before a signed-image claim is made.

The M1 registry is unauthenticated, local, and disposable. Its existence alone
does not satisfy immutable promotion, signing, provenance, or production
registry requirements.

## Consequences

- Development and production can be shown to run identical artifact bytes.
- Promotion and rollback become reviewable Git changes.
- Builds must be reproducible enough to generate reliable evidence, but a
  rollback does not depend on reproducing an earlier build.
- Registry retention must preserve digests referenced by active and rollback
  configurations.
- Emergency fixes still follow the same build-once and reviewed-promotion path.
- Environment-specific behavior belongs in configuration, not rebuilt images.

## Alternatives considered

- **Promote mutable tags such as `latest`:** rejected because the referenced
  bytes can change without review.
- **Rebuild per environment:** rejected because artifact identity is lost.
- **Have CI deploy the digest imperatively:** rejected because Git would no
  longer be the complete declared state and the GitOps boundary would be
  bypassed.

## Validation required

- M2 produces a versioned artifact and evidence without claiming promotion.
- M3 establishes GitOps reconciliation.
- M4 proves digest-only environment configuration, reviewed production
  promotion, and rollback to a previously known digest. The proof is
  `scripts/promote.py` plus `tests/unit/test_promote.py`; CI holds no cluster
  credentials and is scanned for cluster-mutation verbs.
- M5 must prove a failed canary never reaches full traffic and the prior digest
  remains available.
