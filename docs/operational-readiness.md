# Operational Readiness

## Current readiness statement

M1 is ready only as a local development and reviewer foundation when the checks
below pass. M4 promotion is ready as a Git contract: the tool, tests, and CI
gate pass without a cluster. The combined system is not approved for production
workloads, sensitive data, public exposure, or availability claims. Development
and production are modeled environments on the same workstation.

## M1 service inventory

| Resource | Identifier | Owner | Recovery model |
|---|---|---|---|
| Development kind cluster | `gitops-dev` | Local operator | Recreate from repository configuration |
| Production kind cluster | `gitops-prod` | Local operator | Recreate from repository configuration |
| Local registry | `gitops-registry` | Local operator | Recreate; M1 content is disposable |

The corresponding kubectl contexts are `kind-gitops-dev` and
`kind-gitops-prod`.

## Preconditions

- Docker daemon is running and usable by the current operator.
- GNU Make, Bash, Python 3.13, kind, kubectl, and curl are available at
  supported/pinned versions for the Make/Bash path. Native PowerShell 7 can run
  the lifecycle script on Windows; Python 3.13 remains required for verification.
- The host has sufficient CPU, memory, disk, and available networking.
- No production credential or sensitive data is present in the environment.
- The intended resource names do not collide with resources owned by another
  local project.

## Readiness checklist

- [ ] `make verify` passes without Docker, a cluster, or cloud credentials.
- [ ] `make doctor` reports usable runtime prerequisites and no reserved-name
      conflicts.
- [ ] `make up` completes from a clean project state.
- [ ] A second `make up` completes without duplicate resources.
- [ ] `make status` reports the expected registry and both clusters.
- [ ] `make smoke` confirms both API endpoints and Ready nodes.
- [ ] Both clusters contain the expected local-registry discovery data.
- [ ] No M1 service is intentionally exposed to an untrusted network.
- [ ] The operator can identify the exact resources `make down` will remove.
- [ ] `make down` completes successfully twice.
- [ ] `python scripts/promote.py status --environment dev` reports bootstrap
      or digest pins, never a mix.
- [ ] Production promotion has been rehearsed with `--confirm production` and
      refused without it.
- [ ] Any retained evidence is sanitized.

## Operational objectives

M1 does not define a production SLO. For local evidence, record rather than
promise:

- Bootstrap success or failure.
- Bootstrap duration.
- Smoke-check duration and result.
- Cleanup duration and result.
- Idempotent retry result.

Measurements from one workstation must not be presented as general
availability, latency, or scale guarantees.

## Monitoring and diagnosis

M1 has no Prometheus, logging pipeline, tracing backend, or alert manager.
Operators use `make status`, `make smoke`, kind/kubectl inspection, and scoped
Docker inspection. Observability and SLO alerting are planned for M6-M7.

Start diagnosis with the
[local bootstrap failure runbook](runbooks/local-bootstrap-failure.md). Do not
run Docker-wide prune commands as a project recovery step.

## Backup, state, and recovery

The repository is the source for recreating the M1 topology. Cluster and
registry state are disposable; there is no M1 backup guarantee. Kubeconfig
material is local operational state and must never be committed. Cleanup and
recreate is an acceptable recovery only for the exact project resources.

Future GitOps configuration will become the declared source of truth for
in-cluster state. Future Terraform remote state, application data, and
observability retention require separate backup and recovery designs before
those modes are considered ready.

## Change and rollback

Changes to lifecycle scripts, names, port mappings, kind configuration, or
cleanup selection require static checks and the full local lifecycle scenario.
Changes to promotion rules require `make verify` and must not introduce a
cluster-mutation path. For M1, rollback of the topology means returning to the
prior reviewed repository revision and recreating the disposable clusters. For
M4, rollback of a release means `make rollback` of a previously recorded
digest. It does not mean rolling back persistent application data.

## Escalation triggers

Stop automated recovery and review manually if:

- A command targets a cluster or container other than the documented names.
- Cleanup would affect an unknown Docker network, volume, image, or kind cluster.
- A port collision belongs to an unrelated process.
- Repeated convergence changes already-healthy resources.
- Verification exposes a credential or sensitive host detail.

## Exit criteria for later milestones

Each later feature needs its own failure mode, monitoring signal, acceptance
test, rollback, security review, cost impact, and runbook. Merely installing a
controller or dashboard does not satisfy operational readiness.
