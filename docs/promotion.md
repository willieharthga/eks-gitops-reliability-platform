# Environment promotion

How an immutable digest moves from a build into an environment, how production
differs from development, and how rollback works.

Read [ADR 0003](adr/0003-immutable-artifact-promotion.md) for the decision and
[GitOps delivery](gitops.md) for what happens after the commit lands.

## Contract

1. Build an artifact once. Identify it by digest.
2. Change environment configuration in Git. Do not `kubectl set image`.
3. Development may be promoted from `artifacts/images.json`.
4. Production may only copy digests that already exist in another environment.
5. Production requires `--confirm production` locally and
   `PROMOTION-CONFIRM: production` in the pull request body.
6. Rollback restores a previously recorded digest set. It does not rebuild.

CI proposes and validates configuration. Argo CD applies it. A pipeline that
wrote to production with kubectl would make Git an incomplete source of truth.

## What a pin looks like

Pre-promotion, overlays use an explicit placeholder. It is not a release:

```yaml
images:
  - name: gitops-registry:5000/api
    newTag: 0.0.0-bootstrap
```

After promotion, the same field is a digest and the tag is gone:

```yaml
images:
  - name: gitops-registry:5000/api
    digest: sha256:…
```

Kustomize resolves a digest pin to `name@sha256:…`. A mixed overlay — some
services on the bootstrap tag, others on a digest — is rejected.

The workstation and CI push to `localhost`. Clusters pull from
`gitops-registry:5000`. The promotion tool rewrites the name and keeps the
digest, which is the identity.

## Local walkthrough

Build, push, and pin development:

```sh
make images
make push
make promote ENV=dev
git diff environments/dev
```

`make push` is required before the cluster can pull the digest. `make images`
alone loads the image into the Docker engine; kind nodes pull from the
registry.

Promote the same bytes to production:

```sh
make promote ENV=production FROM_ENV=dev CONFIRM=production
git diff environments/production
```

Commit, push, and let Argo CD reconcile. To inspect pins without changing them:

```sh
make promote-status ENV=dev
make promote-status ENV=production
```

## Rollback

Rollback needs two recorded promotions. The first promotion has nothing
digest-identified to restore to; the bootstrap tag is not a previous release.

```sh
make rollback ENV=dev
make rollback ENV=production CONFIRM=production
```

The previous digest set is rewritten into the overlay and appended to
`environments/<env>/promotion-history.json`. Restore is a Git change like any
other promotion. If Argo CD has not yet synced the bad digest, merging the
rollback commit is enough; if it has, self-heal moves the cluster back.

## What CI does and does not do

The `promote` workflow runs on pull requests that touch environment overlays.
If production image pins changed, the pull request body must contain
`PROMOTION-CONFIRM: production`. The check is identical to
`python scripts/promote.py gate`.

CI does **not** rewrite overlays from images it built during the workflow.
Those images are pushed to an ephemeral `localhost:5000` registry on the
runner. Promoting them would commit digests that neither a workstation nor a
kind node can pull. A durable registry (planned for M11 with ECR) is the
prerequisite for unattended promotion PRs.

Required GitHub repository settings, which this repository cannot apply to a
fork on the author's behalf:

- The default branch rejects direct pushes.
- Pull requests require at least one review before merge.
- The `promote` and `verify` checks are required.
- Administrators are not exempt.

The confirmation trailer is an additional gate, not a replacement for review.

## Failure modes

| Failure | Detection | Response |
|---|---|---|
| Promote production from a build manifest | `scripts/promote.py` refuses | Copy from `dev` instead |
| Promote production without `--confirm` | Local command refuses | Type `--confirm production` |
| Production PR without the trailer | `promote` workflow fails | Add `PROMOTION-CONFIRM: production` |
| Missing or malformed digest | Command refuses | Rebuild and record `artifacts/images.json` |
| Source environment still on bootstrap tags | Command refuses | Promote `dev` first |
| Rollback with only one history entry | Command refuses | There is no previous digest set |
| Cluster running the wrong digest | Argo CD OutOfSync, or `make drift-test` | See the [rollback runbook](runbooks/rollback.md) |

## Evidence

Offline, part of `make verify`:

- Table-driven tests in `tests/unit/test_promote.py` covering happy paths,
  production isolation, rollback, malformed manifests, and the pull request
  gate.
- A scan of GitHub workflows and `scripts/promote.py` that fails if a cluster
  mutation verb appears outside the declared forbidden list.
- Overlay contract tests that every pin is either the bootstrap placeholder or
  a `sha256:` digest, never a mix.

Runtime evidence is a reviewed commit whose overlay pins match
`artifacts/images.json` and whose Argo CD Applications report Synced after
merge. That evidence is produced on a machine with Docker and is not attached
to this repository until it exists.
