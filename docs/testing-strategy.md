# Testing Strategy

## Purpose

Tests establish what the repository actually implements. M1 focuses on static
quality, lifecycle convergence, topology health, and cleanup safety for two kind
clusters and one local registry. Later platform behavior is intentionally
outside the current test claim.

Repository verification requires Python 3.13. The Make/Bash lifecycle path also
requires GNU Make, Bash, Docker, kind, kubectl, and curl. Windows operators can
use PowerShell 7 for lifecycle actions through `scripts/platform.ps1`; Python
3.13 is still used for the repository contract.

## Command contract

| Command | Environment | Purpose | Mutation |
|---|---|---|---|
| `make doctor` | Docker-capable workstation | Check runtime tools, Docker, configuration, and reserved-name conflicts | Read-only |
| `make verify` | Clean checkout; no Docker or cloud credentials | Run repository contract, text/link hygiene, secret-pattern, and unit checks | Must not create runtime or cloud resources |
| `make status` | Local topology optional | Report project resource state | Read-only |
| `make smoke` | M1 topology running | Check the host registry API and both cluster nodes | Read-only except incidental client cache |
| `make verify-local` | Docker-capable workstation | Exercise repeated create, smoke, and repeated cleanup | Creates and removes only project resources |
| `make promote` | Clean checkout; `artifacts/images.json` for `ENV=dev` | Write digest pins into an overlay | Tracked overlay and history files only |
| `make rollback` | Environment with two history entries | Restore the previous digest set | Tracked overlay and history files only |

`make up` and `make down` are lifecycle interfaces, not substitutes for
assertions. The integration path must ensure cleanup is attempted even if a
check fails.

## M1 test layers

### Static and contract checks

These run without a Kubernetes cluster or AWS credentials:

- Shell parsing and linting for lifecycle automation.
- YAML and Markdown validation where their tools are available.
- Validation of kind and registry configuration structure.
- Checks that resource identifiers are explicit rather than broad patterns.
- Documentation link validation.

The exact checks may grow as files are added. `make verify` must not report a
tool or test as present when it is not configured.

### Runtime smoke checks

With M1 running, smoke verification should assert:

- Registry container `gitops-registry` is running and its API responds at
  `http://localhost:5001/v2/`.
- kind clusters `gitops-dev` and `gitops-prod` exist.
- kubectl contexts `kind-gitops-dev` and `kind-gitops-prod` are reachable.
- All M1 kind nodes report Ready.

A smoke check must fail clearly and identify the resource that did not satisfy
the contract.

Static unit tests validate the kind mirror and local-registry discovery
configuration. M1 does not yet perform an image pull from inside both clusters;
that end-to-end registry check remains a known coverage gap until a synthetic
workload is introduced.

### Lifecycle and idempotency checks

The local integration scenario is:

1. Begin with project resources absent.
2. Run `make up`.
3. Run `make up` again; no duplicate cluster or registry may be created.
4. Run the smoke checks.
5. Run `make down`.
6. Run `make down` again; absence is a successful end state.
7. Confirm unrelated Docker and kind resources were not selected for deletion.

The test should capture command results in CI or local evidence without
committing kubeconfigs, host details, or credentials.

## Failure testing

M1 failure tests should cover, where deterministic and safe:

- Docker unavailable during preflight.
- A required CLI absent.
- Registry container stopped after bootstrap.
- One expected cluster absent.
- A non-Ready kind node.
- Partial bootstrap followed by a retry.
- Cleanup when all resources are already absent.

Manual recovery is documented in the
[bootstrap runbook](runbooks/local-bootstrap-failure.md).

## M2 test layers

M2 adds application and supply-chain coverage. All of it runs offline through
`make verify`; none of it requires Docker, a cluster, or a network.

### Service behavior

The services are driven through `Application.handle`, the same dispatch path a
socket server uses. This keeps the routing, metric, and trace behavior under
test while leaving the tests fast and usable on a machine with no network.
Upstream calls are stubbed at the `call_json` seam.

Covered: the order lifecycle from creation through completion, input
validation and its boundaries, queue-full and stale-claim recovery, worker
outcome reporting, frontend degradation when the API is unreachable, and HTML
escaping of order fields.

### Encoding contracts

These are contracts with other systems, so the encoded output is asserted
directly rather than through a library's behavior:

- Prometheus exposition, including cumulative histogram buckets and the
  inclusive `le` boundary.
- W3C `traceparent` parsing, including every rejection case, and the rule that
  an unusable header starts a new trace instead of failing the request.
- OTLP/JSON, including the rule that `traceId` and `spanId` are hex strings
  while other `bytes` fields are base64.

### Determinism

Two components are test equipment for later milestones, so their determinism is
itself tested: the fault injector realises its configured ratio exactly and
identically across instances, and the load generator produces an identical
operation sequence across runs. Without these properties the M5 canary rollback
result and the M9 scaling comparison would not be reproducible.

### Container and catalogue contract

Static assertions that need no Docker: every base image is digest-pinned, the
image runs as a fixed non-root UID, bytecode writing is disabled so a read-only
root filesystem works, the entrypoint is exec form so the process receives
SIGTERM, no package manager runs in the runtime stage, and the build script
does not write environment configuration.

### Supply-chain gates

Run in CI, where a registry and scanner are available:

- The build fails if any image lacks SBOM or provenance attestation manifests.
- The scan fails on fixable HIGH or CRITICAL findings, records unfixed findings
  for review, and fails closed when the scanner is missing, so a scan that did
  not happen can never resemble a scan that found nothing.

### What M2 does not test

The services have never run in a cluster. No collector has received a span, no
dashboard exists, and no image has been pulled from inside a kind node. M2
therefore substantiates the telemetry encodings, not an end-to-end telemetry
pipeline.

## M3 test layers

M3 introduces the first test that needs a running cluster, so its coverage is
split in two: what can be proven from the text, and what can only be proven by
reconciling.

### Manifest contract (offline, part of `make verify`)

Argo CD reports most manifest mistakes as a generic sync error, minutes after
the commit, from inside a cluster. Every property that is knowable from the
repository is therefore asserted in `tests/unit/test_gitops_contract.py`, which
runs in well under a second:

- Every manifest parses, and no `Secret` is committed anywhere.
- Every kustomization resource and patch path exists, and no base manifest is
  orphaned by being present but unreferenced.
- `scripts/gitops.py` knows about exactly the files containing the repository
  URL placeholder, so `configure` cannot leave one behind.
- Every Application and ApplicationSet targets a cluster the bootstrap actually
  registers, by name rather than by container IP, and belongs to a project
  defined in this repository rather than the permissive `default`.
- The `workloads` AppProject whitelist covers every kind the base renders plus
  the generated ConfigMap, blacklists `Secret`, and grants no roles.
- Both overlays declare the same set of configuration keys, pin an image for
  every catalogued service, and set a replica count for every workload.
- Every pod spec satisfies the `restricted` profile the namespace enforces:
  non-root, no privilege escalation, read-only root filesystem with a backing
  volume for every mount, all capabilities dropped, `RuntimeDefault` seccomp.
- Self-heal and prune are enabled, tracking is annotation-based, and no
  `ignoreDifferences` rule hides `.spec.replicas`.

The last item matters more than it looks. It is the one setting that would make
the drift test pass while proving nothing, and it is the obvious thing for a
future change to add.

These tests read declarations; they do not render Kustomize. Rendering is
proven by the bootstrap actually working.

### Drift acceptance test (requires a cluster)

`make drift-test` is the M3 acceptance criterion. It performs the two kinds of
drift an operator actually causes against `environments/dev`:

1. A Deployment scaled by hand must return to the committed replica count.
2. A deleted Service must be recreated, verified by a changed UID so a stale
   read of the object being deleted cannot pass as a heal.

Both cases first assert the resource carries Argo CD's tracking annotation: a
resource the controller does not consider its own would drift silently whatever
the sync policy says. The script refuses to start from an Application that is
not already `Synced` and `Healthy`, measures convergence, writes
`artifacts/drift-test.json`, and reverts its own mutation if self-heal did not.

### What M3 does not test

The drift test has not been executed in this repository; Docker, kind, and
kubectl are unavailable where it was authored, so no `artifacts/drift-test.json`
is attached. The bootstrap's idempotency is likewise argued from the code and
not yet demonstrated. Both are the reader's `make up && make bootstrap &&
make drift-test`.

## M4 test layers

M4 is the first milestone whose acceptance criterion is entirely a Git
contract: a digest in an overlay, a refused production shortcut, and a
pipeline that cannot apply. None of it needs a cluster.

### Promotion behaviour

`tests/unit/test_promote.py` drives `scripts/promote.py` against copies of the
real overlays:

- Development promotion from a build manifest writes a digest for every
  catalogued service and rewrites `localhost` to `gitops-registry:5000`.
- A second identical promotion is a no-op, including history.
- Production copies those digests and refuses a build manifest, a missing or
  wrong confirmation, a bootstrap source, and promotion from itself.
- Rollback restores the previous digest set after two promotions and refuses
  when history is too short.
- Malformed manifests (missing file, missing service, tag-only, bad digest)
  and unknown environments are rejected.
- A mixed overlay is rejected before it can be written as desired state.

### Production pull-request gate

`python scripts/promote.py gate` compares production overlays and requires
`PROMOTION-CONFIRM: production` in the pull request body when pins change.
Unchanged pins pass without the trailer. The `promote` GitHub workflow runs
this on pull requests; it holds `contents: read` and does not write overlays.

### Pipeline isolation

Workflows under `.github/workflows/` and `scripts/promote.py` are scanned for
cluster-mutation verbs (`kubectl apply`, `helm upgrade`, `argocd app sync`,
and neighbours). The promotion tool may mention those verbs only in the
tuple the test counts.

### Overlay homogeneity

Committed overlays may pin the bootstrap placeholder or a digest, not both in
the same environment. A promoted pin has a `sha256:` digest and no `newTag`.

### What M4 does not test

Overlays in Git still carry `0.0.0-bootstrap` until an operator runs
`make promote` and commits the result. This repository therefore proves the
tool, not that a digest has been observed running in a cluster. CI does not
open promotion pull requests from ephemeral build digests; those bytes vanish
with the runner.

## Planned test layers

The following are future acceptance criteria, not current coverage:

- M5: deterministic failed canary analysis using real Prometheus metrics.
- M6-M7: correlated metrics/logs/traces and burn-rate alert tests.
- M8: positive and negative admission-policy, RBAC, and network-policy tests.
- M9: load-driven scaling and utilization evidence.
- M10: repeatable dependency, DNS, telemetry, drain, and disruption game days.
- M11: Terraform validation/policy tests plus bounded EKS creation and teardown.

## Evidence quality

Evidence must state the date, revision, host prerequisites, commands, results,
and known limitations. Screenshots may supplement but not replace machine-
readable output. A failed check must not be deleted from evidence merely to make
the release appear green; record the resolution and rerun.
