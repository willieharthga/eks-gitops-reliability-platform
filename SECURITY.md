# Security Policy

## Supported scope

The current supported scope is the M1 local foundation on the default branch:
two disposable kind clusters and one local registry. Pre-release snapshots and
older milestone revisions receive fixes only when maintainers explicitly choose
to backport them.

M1 is not a production service or a security boundary for sensitive workloads.
It must use synthetic data and non-production credentials only.

## Reporting a vulnerability

Do not disclose a suspected vulnerability, credential, exploit, or sensitive
host detail in a public issue, discussion, test fixture, or pull request.

If this repository is hosted on GitHub and private vulnerability reporting is
enabled, use the repository's **Security > Report a vulnerability** workflow.
Otherwise, contact the repository owner privately through the account that
publishes the repository and ask for a private reporting channel before sharing
details. There is intentionally no placeholder email address in this file.

Include, when safe:

- Affected revision and component.
- Reproduction steps using synthetic data.
- Expected and observed behavior.
- Security impact and required preconditions.
- A minimal proof of concept without live secrets.
- Any known workaround.

Maintainers should acknowledge receipt, validate scope, coordinate a fix and
disclosure plan, and credit the reporter if requested. No fixed response SLA is
claimed for this portfolio repository.

## Never commit

- AWS, registry, GitHub, or other access tokens and private keys.
- Kubeconfig files or embedded client certificates.
- Real account IDs, customer records, private domains, or proprietary code.
- Production logs, payloads, or vulnerability details that expose another
  system.
- Terraform state or plan artifacts containing sensitive values.

If a secret is committed, removing the line is not sufficient. Revoke or rotate
the credential first, preserve only safe incident evidence, and follow the
hosting provider's history-remediation process with repository-owner approval.

## M1 security boundary

- Access to the Docker daemon is privileged and effectively controls the kind
  nodes and local registry.
- Contexts `kind-gitops-dev` and `kind-gitops-prod` contain local administrative
  cluster access and must remain off Git.
- Registry `gitops-registry` is a local development convenience. Host endpoint
  `localhost:5001` and in-kind endpoint `gitops-registry:5000` are not a
  production authentication or transport-security design.
- The two clusters share a workstation and Docker daemon; they are not separate
  physical security zones.
- `make down` must select only `gitops-dev`, `gitops-prod`, and
  `gitops-registry`; broad Docker cleanup is prohibited.
- No M1 path should request cloud credentials or make cloud API calls.

The full threat analysis is in
[docs/threat-model.md](docs/threat-model.md).

## Dependency and supply-chain expectations

Tool versions should be pinned or managed by an explicit update process. Review
changes to download URLs, image references, action versions, lifecycle scripts,
port exposure, cleanup selection, and kubeconfig handling as security-sensitive.
Do not describe an artifact as signed, verified, or provenance-backed until the
corresponding enforcement and test evidence exist.

Future workload milestones must add SBOM generation, vulnerability policy,
immutable digests, and a signing/provenance design. Future GitOps, policy, and
EKS milestones require dedicated credential, least-privilege, network, state,
and break-glass reviews before they are considered supported.

## Safe local operation

- Run the project on a maintained, non-production workstation.
- Keep Docker and required CLIs updated within the documented version policy.
- Inspect unexpected resource-name or port collisions; do not remove resources
  belonging to another project.
- Run `make verify` before lifecycle changes and `make smoke` after bootstrap.
- Use [the bootstrap runbook](docs/runbooks/local-bootstrap-failure.md) rather
  than host-wide cleanup commands.
- Sanitize logs and command output before publishing evidence.
