# Runbook: Local Bootstrap Failure

## Scope

Use this runbook when M1 cannot create or verify:

- kind cluster `gitops-dev` / context `kind-gitops-dev`;
- kind cluster `gitops-prod` / context `kind-gitops-prod`; or
- registry container `gitops-registry`, exposed to the host at
  `localhost:5001` and to kind nodes at `gitops-registry:5000`.

This topology is disposable and contains synthetic data only. If that statement
is not true for your environment, stop: this runbook does not authorize
deleting it.

## Safety rules

- Never use `docker system prune`, broad name patterns, or Docker-wide cleanup.
- Never delete an unknown cluster, container, network, volume, context, or
  process to resolve a collision.
- Before any manual deletion, confirm the exact target is one of the three
  project resource names above.
- Prefer `make up` for convergence and `make down` for project cleanup.
- Preserve unrelated local resources.

## Symptoms

- `make doctor` reports a missing or unusable prerequisite.
- `make up` exits before all three resources are ready.
- Port `5001` is unavailable.
- `make smoke` cannot reach the registry or a cluster API.
- A kind node is not Ready.
- Registry discovery is missing from one cluster.
- A retry reports an existing resource instead of converging.

## Initial triage

Run the non-destructive checks first:

```sh
make doctor
make status
kind get clusters
docker ps -a --filter name=gitops-registry
kubectl --context kind-gitops-dev get nodes
kubectl --context kind-gitops-prod get nodes
```

Some commands will fail when their resource is absent; that failure is useful
diagnostic evidence. Confirm that Docker is running and record the first failing
command, its exit status, and sanitized output.

For registry-specific diagnosis:

```sh
docker inspect gitops-registry
docker logs --tail 100 gitops-registry
```

Inspection output can contain host and network details. Sanitize it before
sharing or committing evidence.

## Recovery procedures

### Prerequisite or Docker failure

1. Correct only the prerequisite reported by `make doctor`.
2. Start or restore the Docker daemon using the host's normal administration
   path.
3. Rerun `make doctor`.
4. Rerun `make up`; it must safely converge a partial topology.
5. Run `make smoke`.

On Windows, either use WSL2 with Docker Desktop integration for the GNU
Make/Bash path or invoke `scripts/platform.ps1` with PowerShell 7 for lifecycle
actions. Do not mix Windows and WSL kubeconfig paths during one lifecycle run.

### Port 5001 collision

1. Identify which local process or container owns host port `5001`.
2. If it is `gitops-registry`, use `make up` to reconcile it.
3. If another project owns it, do not stop or delete that resource
   automatically. Coordinate with its owner, or propose a reviewed change that
   updates the registry endpoint consistently in automation, kind mirror
   configuration, tests, and documentation.
4. Rerun `make up` and `make smoke` after the collision is resolved.

### Partial bootstrap or missing resource

1. Use `make status` to confirm exactly which project resources exist.
2. Run `make up` again. It is the supported recovery for a partial creation.
3. Run `make smoke`.
4. If the same step fails again, capture the scoped Docker/kubectl diagnostics
   and investigate the underlying error instead of looping indefinitely.

### Unhealthy cluster node

Inspect the affected named cluster only:

```sh
kubectl --context kind-gitops-dev get nodes -o wide
kubectl --context kind-gitops-dev get events -A
kubectl --context kind-gitops-prod get nodes -o wide
kubectl --context kind-gitops-prod get events -A
```

If local capacity or Docker health is the cause, correct it and rerun
`make up`. If the disposable project topology needs a full rebuild, first
confirm the scope, then run:

```sh
make down
make up
make smoke
```

### Registry discovery mismatch

Do not patch the cluster manually and call the incident resolved. Compare the
running discovery configuration with the reviewed repository contract, rerun
`make up`, and then run `make smoke`. If convergence does not correct it, treat
the lifecycle configuration as the defect and submit the smallest tested fix.

## Validate recovery

```sh
make status
make smoke
make up
make smoke
```

Both smoke runs must pass, and the repeated `make up` must not duplicate or
replace healthy resources unexpectedly.

## Clean-state fallback

Only after confirming the topology is disposable and the exact resource names
belong to this project:

```sh
make down
make down
make up
make smoke
```

The repeated cleanup should succeed when resources are absent. Never substitute
a broad Docker or kind cleanup command.

## Escalate when

- A lifecycle command selects an undocumented name.
- Cleanup proposes changing an unrelated resource.
- A collision belongs to another user or project.
- Sensitive data or production credentials were introduced.
- Retry repeatedly mutates healthy resources.
- Recovery would require host-wide Docker changes or data loss.

Record the repository revision, host platform, tool versions, failing stage,
sanitized output, recovery action, and result. Update this runbook when a new
repeatable failure mode is discovered.
