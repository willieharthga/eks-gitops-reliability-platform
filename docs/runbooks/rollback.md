# Runbook: Roll back a promoted digest

Rollback is a promotion of a previously recorded digest. It is a Git change.
Argo CD reconciles it. Rebuilding old source is not required and is not
equivalent: a rebuild may not reproduce the same digest.

## When to use this

- A promoted digest is failing in an environment and a previous digest is
  known-good in `environments/<env>/promotion-history.json`.
- A promotion pull request merged and must be undone without reverting
  unrelated commits that landed in the same window.

## Preconditions

- The environment has at least two history entries. The first promotion cannot
  roll back to the bootstrap tag; that tag is not a release.
- The previous digest is still in `gitops-registry`. Registry content is
  disposable locally; if `make down` ran, rebuild from the recorded revision
  instead of rolling back.
- Production rollback requires the same confirmation as production promotion.

## Procedure

1. Inspect the current and previous pins:

   ```sh
   make promote-status ENV=dev
   cat environments/dev/promotion-history.json
   ```

2. Restore the previous digest set:

   ```sh
   make rollback ENV=dev
   ```

   For production:

   ```sh
   make rollback ENV=production CONFIRM=production
   ```

3. Confirm the overlay matches the previous history entry:

   ```sh
   git diff environments/
   ```

4. Commit, push, and open a pull request. For production, include:

   ```
   PROMOTION-CONFIRM: production
   ```

5. Wait for Argo CD to report Synced. `make gitops-wait` blocks until that is
   true. Do not `kubectl rollout undo`; that would move the cluster away from
   Git and the drift test exists to forbid it.

## If rollback itself fails

- `no previous promotion to restore`: promote a known-good digest forward from
  `artifacts/images.json` (dev) or from the environment that still has it.
- Registry miss: check out the recorded `source_revision` and run `make push`.
- Argo CD still serving the bad digest: see
  [application out of sync](application-out-of-sync.md).

## Evidence to keep

Retain the rollback commit SHA, the restored digests, Argo CD sync status, and
the history file. Do not commit kubeconfig or registry credentials.
