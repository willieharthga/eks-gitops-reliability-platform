# Runbook: Application Out Of Sync Or Unhealthy

## Scope

Use this runbook when `make gitops-status` reports an Application that is not
both `Synced` and `Healthy`, when `make drift-test` fails, or when a commit to
`environments/` does not appear in a cluster.

Applications in scope: `root`, `platform-dev`, `platform-production`.
Clusters in scope: `gitops-dev` (control plane and dev environment) and
`gitops-prod`.

## Safety rules

- Do not fix an Application by editing the cluster. A `kubectl edit` that makes
  the symptom disappear is reverted on the next sync, and it destroys the
  evidence needed to find the cause.
- Do not disable `selfHeal` or `prune` to make an error stop. That converts a
  reported failure into an unreported one.
- Do not delete an Application to force a resync. Deletion runs the resources
  finalizer and removes the managed workloads. Use `argocd app sync` or wait
  for the 30-second reconciliation.
- Read-only diagnosis first. Every command in **Triage** below is safe.

## Triage

```sh
make gitops-status
kubectl --context kind-gitops-dev --namespace argocd get applications
kubectl --context kind-gitops-dev --namespace argocd \
  get application platform-dev --output yaml
```

The useful fields are `status.conditions`, `status.operationState.message`, and
`status.sync.revision`. Record the revision: half of all "the commit did not
deploy" reports are the controller correctly reconciling an older commit.

Controller and repository-server logs:

```sh
kubectl --context kind-gitops-dev --namespace argocd \
  logs statefulset/argocd-application-controller --tail 100
kubectl --context kind-gitops-dev --namespace argocd \
  logs deployment/argocd-repo-server --tail 100
```

Before diagnosing anything cluster-side, rule out the repository:

```sh
make verify
```

The manifest contract tests catch a renamed file, a kind missing from an
AppProject whitelist, an unregistered destination cluster, and an overlay that
lost a setting — without a cluster and in under a second.

## Symptoms

### `ComparisonError`: repository not accessible

The repository URL is wrong, unreachable, or private with no credentials.

```sh
grep -r REPLACE_ME_REPO_URL platform environments
```

If anything matches, the URL was never configured. Run
`make configure REPO_URL=…`, commit, and push. If nothing matches, confirm the
URL resolves from inside the cluster and that the branch in `targetRevision`
(`main`) exists on the remote. A URL that works from your laptop over SSH will
not work from the repo-server, which has no key.

### `ComparisonError`: kustomize build failed

Reproduce it locally against the same path the Application uses:

```sh
kubectl kustomize environments/dev
kubectl kustomize platform/argocd/root
```

A missing file reference is the common cause and is what
`test_kustomizations_reference_files_that_exist` exists to catch before the
commit lands.

### `SyncError`: resource is not permitted in project

The AppProject whitelist does not include a kind the overlay renders. Add the
kind to `platform/argocd/root/projects/workloads.yaml`, deliberately — the
whitelist is a guardrail, and widening it is the change under review, not an
obstacle to route around. `test_the_workloads_project_permits_every_kind_the_base_renders`
then keeps the two in step.

### `SyncError`: cluster is not registered

`platform-production` targets the cluster named `gitops-prod`, which exists
only as a Secret written at bootstrap.

```sh
kubectl --context kind-gitops-dev --namespace argocd \
  get secrets --selector argocd.argoproj.io/secret-type=cluster
```

If the Secret is absent, or `gitops-prod` was recreated since bootstrap and its
client certificate no longer matches, re-run `make bootstrap`. It rewrites the
Secret from the current kubeconfig.

### `Progressing` or `Degraded`: pods will not start

The Application is synced; the workload is the problem.

```sh
kubectl --context kind-gitops-dev --namespace platform get pods
kubectl --context kind-gitops-dev --namespace platform \
  describe pod <name>
kubectl --context kind-gitops-dev --namespace platform \
  get events --sort-by .lastTimestamp
```

Two causes dominate in this topology:

- **`ImagePullBackOff`.** The overlays reference
  `gitops-registry:5000/<service>:<tag>`. Confirm the tag was pushed with
  `make push`, and that the registry is reachable from the node. `make smoke`
  checks the second part.
- **Rejected by Pod Security admission.** The `platform` namespace enforces the
  `restricted` profile. The event names the offending field. Fix the manifest,
  never the namespace label; `test_pod_specs_satisfy_the_restricted_profile`
  asserts the base already complies, so a failure here means a local edit.

### Application is `Synced` but the cluster is wrong

This is the case the drift test exists for, and it means Argo CD does not
consider the resource its own.

```sh
kubectl --context kind-gitops-dev --namespace platform \
  get deployment frontend \
  --output jsonpath='{.metadata.annotations.argocd\.argoproj\.io/tracking-id}'
```

An empty result means the resource was created outside Argo CD and happens to
share a name. Delete the unmanaged copy after confirming what it is, and let
the Application recreate it.

### `make drift-test` reports `DRIFT PERSISTED`

Self-heal did not act. In order of likelihood:

1. `syncPolicy.automated.selfHeal` is not `true` on the Application. Check the
   live object, not the file — something may have edited it.
2. An `ignoreDifferences` rule covers the field. For the mutation case this
   would be `.spec.replicas`; the repository deliberately has no such rule
   until M9, and `test_replica_drift_is_not_ignored` asserts it.
3. The controller is not running or is wedged. Check the logs above.
4. The test timed out on a slow host. Re-run with
   `python scripts/drift.py --timeout 300`.

The script reverts its own mutation when self-heal does not, but a failed
deletion case leaves the resource missing. Restore it with a manual sync once
the cause is fixed.

## Recovery

Forcing a reconciliation without deleting anything:

```sh
kubectl --context kind-gitops-dev --namespace argocd \
  annotate application platform-dev \
  argocd.argoproj.io/refresh=hard --overwrite
```

Re-running the bootstrap is safe and is the correct response to a damaged
installation, a rotated `gitops-prod` credential, or an Argo CD upgrade:

```sh
make bootstrap
make gitops-wait
```

Full rebuild, only after confirming the topology is disposable:

```sh
make gitops-uninstall
make down
make up
make bootstrap
make gitops-wait
make drift-test
```

## Validate recovery

```sh
make gitops-status   # every Application Synced and Healthy
make drift-test      # both cases healed
make verify          # the repository still satisfies its contract
```

## Escalate when

- The same Application fails after a documented recovery has been applied twice.
- A fix would require disabling `selfHeal`, `prune`, or a project guardrail.
- Argo CD reports a resource it should not own, or owns one in a namespace
  outside its AppProject destinations.
- A cluster credential appears in the working tree or in a commit.
- Recovery would delete state whose owner is unknown.

Record the repository revision, the Application's `status.sync.revision`, the
failing condition verbatim, the recovery action, and the result. Add a new
symptom section here when a failure mode recurs.
