# Cost Model

## Current M1 cost boundary

M1 creates no AWS or other cloud resources, requires no cloud account, and
makes no cloud API calls. Its direct cloud cost is therefore **$0**. The local
platform still consumes resources that are not free in an economic sense:

- CPU and memory for two kind control-plane containers.
- Disk for node images, registry layers, logs, and Docker metadata.
- Network transfer when tools and images are downloaded.
- Workstation electricity and operator time.

Those costs vary by host and existing caches, so this repository does not
publish a fabricated dollar estimate for them.

## Resource envelope

The M1 topology is intentionally bounded to two single-node clusters and one
registry container. Operators should measure actual consumption on their host
rather than treating a nominal estimate as a guarantee.

Useful observations include:

```sh
docker stats --no-stream
docker system df
```

These commands report Docker-wide state, including unrelated resources. Do not
attribute all displayed usage to this project. Project evidence should filter
or record the explicitly named M1 resources where possible.

`make down` removes the project-managed clusters and registry. Shared Docker
image caches may remain and must not be pruned automatically because they can be
used by unrelated projects.

## Cost controls

- Local mode is the default and complete M1 path.
- Bootstrap is limited to exactly two named kind clusters and one named
  registry.
- Cleanup is explicit, repeatable, and scoped to project resources.
- No script may silently enable an AWS path or create a billable resource.
- Large optional components must use resource-conscious local profiles when
  introduced.

## Planned AWS cost gate

Terraform-managed EKS is optional and planned for M11. Before it may be merged,
the change must include:

1. A dated estimate based on the selected region, duration, cluster count,
   control planes, compute, storage, load balancers, NAT/data transfer,
   observability, and registry usage.
2. Explicit assumptions and a link to the pricing source or calculator used at
   that time.
3. A budget threshold and alert destination.
4. An owner and maximum lifetime for ephemeral environments.
5. A tested, idempotent teardown command.
6. A post-teardown check for common residual costs such as load balancers,
   volumes, snapshots, public addresses, log ingestion, and NAT gateways.

This document intentionally omits a fixed EKS dollar figure: prices and
architecture choices can change, and no AWS mode exists in M1. A pull request
that adds the AWS mode must replace that uncertainty with a reviewable estimate
before apply is possible.

## Evidence to retain

M1 evidence should record bootstrap duration, cleanup duration, peak local
resource use during the documented demo, and residual project-owned resources
after cleanup. Future cloud evidence must record estimated versus actual spend
for a bounded test window without exposing account identifiers.
