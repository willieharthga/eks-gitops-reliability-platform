# One Dockerfile builds all four services.
#
# Four near-identical Dockerfiles drift: one of them eventually keeps root, or
# misses PYTHONDONTWRITEBYTECODE and then crashes under a read-only root
# filesystem. A single parameterised build makes the hardening contract
# impossible to apply inconsistently, and lets the container-contract test
# assert it exactly once.
#
# The service package is copied to the fixed path /app/service, so the run
# command is identical for every image and needs no shell to expand a variable.

# hadolint ignore=DL3006
FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS build

ARG SERVICE_DIR
ARG SERVICE_PACKAGE

WORKDIR /build
COPY apps/shared/platform_common ./platform_common
COPY apps/${SERVICE_DIR}/${SERVICE_PACKAGE} ./service

# Ship bytecode so the read-only runtime layer never needs to write .pyc files.
RUN python -m compileall -q ./platform_common ./service


FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285 AS runtime

ARG SERVICE_NAME
ARG SERVICE_VERSION=unknown
ARG SERVICE_REVISION=unknown
ARG BUILD_CREATED=unknown
ARG SOURCE_URL=https://github.com/example/eks-gitops-reliability-platform

LABEL org.opencontainers.image.title="${SERVICE_NAME}" \
      org.opencontainers.image.description="Synthetic ${SERVICE_NAME} service for the EKS GitOps Reliability Platform." \
      org.opencontainers.image.version="${SERVICE_VERSION}" \
      org.opencontainers.image.revision="${SERVICE_REVISION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="docker.io/library/python:3.13-slim"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    SERVICE_NAME="${SERVICE_NAME}" \
    SERVICE_VERSION="${SERVICE_VERSION}" \
    SERVICE_REVISION="${SERVICE_REVISION}" \
    HTTP_PORT=8080

# A fixed numeric UID lets Kubernetes enforce runAsNonRoot without resolving a
# name, and keeps file ownership stable across rebuilds.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=build --chown=root:root --chmod=0555 /build/platform_common ./platform_common
COPY --from=build --chown=root:root --chmod=0555 /build/service ./service

USER 10001:10001
EXPOSE 8080

# No HEALTHCHECK: Kubernetes probes /healthz and /readyz, and a second,
# separately-configured liveness definition inside the image is a source of
# disagreement rather than of safety.
ENTRYPOINT ["python", "-m", "service.main"]
