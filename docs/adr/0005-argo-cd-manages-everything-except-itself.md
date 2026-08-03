# ADR 0005: Argo CD Manages Everything Except Its Own Installation

- Status: Accepted
- Date: 2026-09-02

## Context

ADR 0002 assigned Argo CD ownership of in-cluster state after bootstrap but
left one question open: does "in-cluster state" include the Argo CD
installation itself?

Self-management is a common pattern and it is genuinely attractive. It makes an
Argo CD upgrade a reviewed commit like any other change, and it removes a
category of undocumented cluster state that only exists because somebody once
ran a command.

It also has a specific failure mode. When Argo CD reconciles its own
Deployment, the controller that would detect and revert a bad upgrade is the
controller being upgraded. If the new version cannot start, crashes on the
existing CRDs, or loses access to its repository credentials, nothing is left
running that can put the previous version back. Recovery is a manual `kubectl
apply` of the old manifests — the exact operation the installation was supposed
to eliminate.

The same reasoning applies with less force to CRDs. An Argo CD upgrade that
removes or narrows a CRD field can invalidate the very Application objects that
describe the desired state.

A second, smaller problem is ordering. An Application whose `project` does not
yet exist is rejected, and the AppProjects live in the root Application's own
path. Something has to break that cycle.

## Decision

The repository draws the line between installation and configuration:

| Layer | Path | Applied by | Reconciled by Argo CD |
|---|---|---|---|
| Argo CD installation and CRDs | `platform/argocd/install` | `make bootstrap`, once per cluster | No |
| AppProjects | `platform/argocd/root/projects` | `make bootstrap`, then adopted | Yes |
| Root Application | `platform/argocd/root/root-application.yaml` | `make bootstrap`, then adopted | Yes |
| ApplicationSets | `platform/argocd/root` | Argo CD | Yes |
| Environment workloads | `environments/<name>` | Argo CD | Yes |
| Platform add-ons | added in M5–M8 | Argo CD | Yes |

Concretely:

- The installation is a Kustomize overlay on a **pinned upstream release tag**.
  Upgrading Argo CD is still a reviewed commit; it is applied by an operator
  running `make bootstrap` rather than by Argo CD reconciling itself. The
  review property is kept, the self-destruction property is not.
- `make bootstrap` is idempotent and safe to re-run. It is the upgrade path.
- The AppProjects and the root Application are applied once by the bootstrap to
  break the ordering cycle, and are then adopted by the root Application. After
  the first sync they behave like any other managed resource: an edit made with
  `kubectl` is reverted.
- The root Application's source path contains the root Application's own
  manifest, so Git remains the only way to change it.
- Cluster credentials for `gitops-prod` are generated at bootstrap from the
  local kubeconfig and applied as a Secret directly to the cluster. They are
  never written into the working tree, so they cannot be committed.

## Consequences

- A failed Argo CD upgrade leaves a working controller, because the previous
  version is only replaced by an explicit operator action that can be reverted
  the same way.
- Argo CD's installed version is not visible as an Application in the UI. It is
  visible in `platform/argocd/install/kustomization.yaml`, and a reviewer must
  know to look there. `docs/gitops.md` says so.
- Bootstrap is not a pure `kubectl apply -f` of one file. It is a script, and
  the script is a supported interface with its own tests.
- Drift in the Argo CD installation itself is not detected. This is accepted:
  the installation is not modified between upgrades, and the alternative trades
  a detectable problem for an unrecoverable one.
- Two clusters means two bootstraps if the control plane is ever split. Today
  there is one control plane, in `gitops-dev`, as ADR 0002 records.

## Alternatives considered

- **Full self-management, including the installation.** Rejected for the
  upgrade failure mode above. It would be reconsidered with a second, separate
  Argo CD instance whose only job is to manage the first — worthwhile at
  organisational scale, disproportionate here, and it merely moves the question
  to the second instance.
- **Helm chart with `--atomic` for the installation.** Rejected because it adds
  a second packaging tool and a release-state Secret for one component, and
  because atomic rollback still requires a healthy client at the moment of
  failure.
- **Committing the rendered upstream manifests instead of a remote ref.**
  Tempting, and it removes a network dependency from bootstrap. Rejected
  because the resulting diff on upgrade is thousands of lines that nobody
  reviews, which is worse than a one-line version bump that a reviewer can
  actually read. The pinned tag plus a checksum-free but immutable Git ref is
  the honest trade.
- **`project: default` for the root Application** to avoid the ordering cycle.
  Rejected because the default project permits every resource in every
  namespace on every cluster, which would make the most privileged Application
  in the repository the least constrained one.

## Validation

- `tests/unit/test_gitops_contract.py` asserts the installation pins an exact
  release, that the root Application's path contains its own manifest, that no
  Application uses the default project, and that self-heal and prune are on.
- `make bootstrap` run twice against the same cluster must succeed both times.
- `make drift-test` must show a manual mutation and a manual deletion being
  reverted.
