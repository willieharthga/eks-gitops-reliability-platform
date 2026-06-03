# Contributing

Thank you for improving the EKS GitOps Reliability Platform. Contributions must
preserve an honest boundary between implemented behavior and the roadmap.

## Current scope

M1 implements two kind clusters (`gitops-dev` and `gitops-prod`) and one local
registry (`gitops-registry`). M2 implements the synthetic workload and
supply chain. M3 implements Argo CD delivery and the drift test. M4 implements
digest-only promotion and rollback through Git. Progressive rollout,
observability, SLOs, policy enforcement, autoscaling, resilience game days,
Terraform, and EKS are planned unless repository evidence for their milestone
has been merged.

Discuss changes that cross milestone or ownership boundaries before building
them. Prefer one reviewable concern per pull request.

## Development prerequisites

- Docker Engine or Docker Desktop.
- GNU Make and Bash.
- Python 3.13, kind, kubectl, and curl.
- Any lint/test tools reported by `make doctor` for the files being changed.

On Windows, either use WSL2 with Docker Desktop integration for the documented
GNU Make/Bash lifecycle or call `scripts/platform.ps1` with PowerShell 7 for
local lifecycle actions. Avoid mixing Windows and WSL kubeconfig locations
within a single run.

## Local workflow

1. Create a focused branch from the current default branch.
2. Make the smallest change that satisfies the stated acceptance criteria.
3. Update documentation, tests, threat assumptions, and runbooks affected by
   the change.
4. Run credential-free checks:

   ```sh
   make verify
   ```

5. For lifecycle or cluster configuration changes, run the complete local
   scenario. It creates project resources and cleans them up:

   ```sh
   make verify-local
   ```

6. Review the diff for credentials, host details, generated kubeconfigs,
   unsupported claims, and unrelated changes.
7. Open a pull request describing the goal, scope, tests, security/cost impact,
   failure behavior, documentation changes, and explicit non-goals.

If `make verify-local` is unsuitable for an unrelated documentation-only change,
state why it was not run. Do not report a skipped check as passing.

## Lifecycle contract

- `make doctor` checks prerequisites.
- `make up` converges exactly the registry and two named clusters and is
  idempotent.
- `make status` reports state without modifying it.
- `make smoke` verifies the host registry endpoint and both cluster nodes.
- `make down` removes only the named project resources and succeeds if they are
  already absent.
- `make verify` runs static/unit checks without Docker, clusters, cloud
  credentials, or runtime mutation.
- `make verify-local` exercises repeated create, smoke, and repeated cleanup,
  with cleanup attempted after failure.

Do not broaden cleanup selection or silently make AWS a prerequisite.

## Tests and evidence

Every behavior change needs a positive test and, where meaningful, a safe
negative or failure test. Follow
[docs/testing-strategy.md](docs/testing-strategy.md). Tests must be deterministic,
must use synthetic inputs, and must leave the project in a documented state.

Do not commit kubeconfig content, credentials, private registry details, real
account identifiers, or unsanitized host output as evidence. Screenshots do not
replace repeatable checks.

## Documentation rules

- Mark future capability as **planned** until its acceptance evidence exists.
- Keep commands copyable and relative links valid.
- Update architecture, threat model, cost model, operational readiness, and the
  relevant runbook when their assumptions change.
- Add an ADR for a durable decision with meaningful alternatives or operational
  consequences. Do not rewrite an accepted ADR to conceal history; supersede it.
- Never imply that this reference implementation is proprietary AWS,
  Cloudflare, or GitHub code or that local kind tests prove production scale.

## Security

Read [SECURITY.md](SECURITY.md) before handling a security-sensitive change. Do
not put vulnerability details or secrets in a public pull request. Changes to
tool downloads, Docker exposure, kubeconfig handling, cleanup behavior,
credentials, signing, policies, or cloud access require explicit security
review.

## Pull request acceptance

A pull request is ready when:

- Its implemented behavior matches its documentation.
- Required checks pass from a clean state.
- Failure behavior and cleanup are tested or explicitly justified.
- Security and cost changes are bounded and documented.
- No secret or sensitive identifier is present.
- Implemented and planned features remain clearly separated.
- Third-party licenses and attribution are preserved.
