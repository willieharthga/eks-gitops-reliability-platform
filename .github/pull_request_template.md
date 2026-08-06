## Goal

<!-- What problem does this change solve? -->

## Scope

<!-- Files and behaviours in this pull request. -->

## Tests

<!-- Commands you ran, and which failure case you covered. -->

```sh
make verify
```

## Security and cost

<!-- Credentials, exposure, cloud cost, or "none". -->

## Documentation

<!-- Which docs, ADRs, or runbooks changed. -->

## Non-goals

<!-- What this pull request deliberately does not do. -->

## Production promotion

If this pull request changes image pins under `environments/production/`, it
must include the following trailer on its own line. CI rejects a production
digest change that omits it. The trailer is not a substitute for review.

```
PROMOTION-CONFIRM: production
```

Leave this section unchanged when production pins are not part of the change.
