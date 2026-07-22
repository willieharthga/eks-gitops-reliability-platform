# Demo Script

## Goal

Demonstrate the implemented platform through M4: two kind environments, a
local registry, credential-free verification, GitOps delivery, and
digest-only promotion that never talks to a cluster. Do not claim canary
rollback, observability backends, policy enforcement, autoscaling, or EKS
deployment in this demo.

## Before the demo

- Use a non-production workstation with Docker running.
- Install GNU Make, Bash, Python 3.13, kind, kubectl, and curl as documented in the root
  [README](../README.md).
- Do not load cloud or production credentials into the demo shell.
- If preserving evidence, record the repository revision without publishing
  hostnames, usernames, or kubeconfig credentials.

On Windows, each `make` lifecycle action in this walkthrough can be replaced by
`pwsh -File scripts/platform.ps1 <action>`. Run repository verification as
`python scripts/verify.py`. The combined `make verify-local` scenario currently
uses the GNU Make/Bash path.

Optionally remove an earlier M1 topology so the demo starts clean:

```sh
make down
```

## Walkthrough

### 1. Establish the evidence boundary

Show the implemented/planned table in the [README](../README.md). Explain that
M1 models development and production on one Docker host and does not prove
physical isolation, production scale, or EKS behavior. M4 promotion is a Git
change; Argo CD applies it after merge.

### 2. Run credential-free repository checks

```sh
make doctor
make verify
```

Expected outcome: runtime prerequisites and reserved names are safe, and
configured static/unit checks pass without creating a cluster or requesting
cloud credentials.

### 3. Create the topology

```sh
make up
```

Expected outcome: registry `gitops-registry` (host endpoint
`localhost:5001`) and clusters `gitops-dev` and `gitops-prod` converge to their
intended state. The clusters use `gitops-registry:5000` internally.

### 4. Show both environment boundaries

```sh
kind get clusters
kubectl --context kind-gitops-dev get nodes
kubectl --context kind-gitops-prod get nodes
```

Expected outcome: both named clusters exist and each reports its node Ready.
Other clusters may also appear on the host; their presence is unrelated to this
project.

### 5. Show registry and topology health

```sh
make status
make smoke
```

Expected outcome: the project registry's host API responds and both clusters
report Ready nodes. Static/unit verification covers the mirror and discovery
configuration; M1 does not claim an end-to-end in-cluster image pull.

### 6. Demonstrate convergence

```sh
make up
make smoke
```

Expected outcome: the second bootstrap succeeds without adding a duplicate
registry or cluster.

### 7. Clean up twice

```sh
make down
make down
```

Expected outcome: both commands succeed; the named project resources are absent
and unrelated local Docker/kind resources are untouched.

## M3 and M4 walkthrough (optional, needs Docker)

With the topology still up, after `make configure` and a commit of the
repository URL:

```sh
make push
make bootstrap
make gitops-wait
make drift-test
make promote ENV=dev
make promote ENV=production FROM_ENV=dev CONFIRM=production
git diff environments
make rollback ENV=dev
```

Expected outcome: Argo CD reports Synced, drift is reverted, development pins
become digests, production copies those digests only with confirmation, and
rollback restores the previous digest set once two promotions exist. None of
the promote/rollback commands call kubectl.

If this walkthrough is skipped, say so. Do not report it as passing.

## Evidence to save

For a portfolio release, retain sanitized output for the repository checks,
initial bootstrap, repeated bootstrap, smoke checks, initial cleanup, and
repeated cleanup. Include elapsed times and the repository revision. Do not
commit kubeconfig content, tokens, host identifiers, or raw Docker metadata that
may contain private registry names.

## Failure variant

Stop the demonstration if a command reports an unexpected target or suggests a
broad cleanup. For ordinary readiness or registry failures, use the
[local bootstrap failure runbook](runbooks/local-bootstrap-failure.md), record
the root cause, and rerun from the smallest safe recovery point.
