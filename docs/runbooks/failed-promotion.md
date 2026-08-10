# Runbook: Failed promotion

A promotion is a Git change. If the cluster is running the wrong digest, Git
is wrong, Argo CD has not synced, or the registry does not have the bytes.
Do not `kubectl set image`.

## Symptoms

- `make promote` exits non-zero.
- The `promote` GitHub check fails on a pull request.
- Argo CD reports Synced but the running pod image is not the digest in Git.
- Pods are `ImagePullBackOff` after a promotion merge.

## Immediate actions

1. Read the command or check output. The promotion tool prints the rule that
   failed (missing confirmation, bootstrap source, malformed digest).
2. Do not retry with a broader flag. `--confirm production` is the only extra
   token production accepts; it is not a force override of the other rules.
3. Do not patch the live Deployment.

## Diagnosis

### The command refused to write the overlay

| Message fragment | Cause | Fix |
|---|---|---|
| `production cannot be promoted from a build manifest` | `ENV=production` used `--from` | Use `FROM_ENV=dev CONFIRM=production` |
| `requires --confirm production` | Confirmation omitted or misspelled | Type the environment name exactly |
| `missing pin(s)` | `artifacts/images.json` is incomplete | Rebuild with `make push` |
| `is not a sha256 digest` | Tag-only or truncated digest | Rebuild; do not edit the digest by hand |
| `cannot copy bootstrap tags` | `dev` has not been promoted yet | `make promote ENV=dev` first |
| `digest manifest not found` | `make images`/`make push` was not run | Produce `artifacts/images.json` |

### The pull request check failed

The `promote` workflow requires `PROMOTION-CONFIRM: production` in the pull
request body when `environments/production/kustomization.yaml` image pins
change. Add the trailer on its own line and push no further commits if the
pins are already correct.

### The overlay changed but pods cannot pull

The digest is not in `gitops-registry`. `make push` writes to
`localhost:5001`, which is the host alias for that registry. `make images`
without `--push` does not. Re-run `make push` for the same revision; a digest
is content-addressed, so a clean rebuild of the same tree should match. If it
does not, the working tree was dirty — rebuild from the reviewed commit.

### Argo CD has not picked up the commit

Follow [application out of sync](application-out-of-sync.md). Confirm the
Application is watching `main` and that the commit is on that branch. A UI
sync of `platform-production` is not a substitute for merging the promotion
pull request.

## Recovery

- If Git is wrong, roll back with the [rollback runbook](rollback.md).
- If Git is right and the registry is empty, push the digest again.
- If Git is right, the registry has the digest, and Argo CD is Synced, inspect
  the ReplicaSet image. A stale ReplicaSet is still GitOps-owned and will
  terminate once the new ReplicaSet is ready.

## What not to do

- Do not `kubectl set image` or `kubectl apply` a hand-edited Deployment.
- Do not retag `latest` onto the previous digest.
- Do not promote production from a CI artifact produced against an ephemeral
  registry. Those bytes are gone when the job ends.
