"""Build identity reported by every service.

The values are injected at container build time. They are the link between a
running pod, the artifact digest that produced it, and the source revision that
was reviewed, so they must never be guessed at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config

UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildInfo:
    """Immutable description of the running artifact."""

    service: str
    version: str = UNKNOWN
    revision: str = UNKNOWN
    image_digest: str = UNKNOWN
    environment: str = UNKNOWN
    instance: str = UNKNOWN
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def is_traceable(self) -> bool:
        """Report whether this build can be traced back to a reviewed artifact."""
        return UNKNOWN not in (self.version, self.revision, self.image_digest)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "service": self.service,
            "version": self.version,
            "revision": self.revision,
            "image_digest": self.image_digest,
            "environment": self.environment,
            "instance": self.instance,
            "traceable": self.is_traceable,
        }
        payload.update(self.extra)
        return payload

    def as_labels(self) -> dict[str, str]:
        """Return the low-cardinality subset safe to use as metric labels."""
        return {
            "service": self.service,
            "version": self.version,
            "environment": self.environment,
        }


def from_environment(service: str, *, environ: config.Environ | None = None) -> BuildInfo:
    """Read build identity from the conventional platform environment variables."""
    return BuildInfo(
        service=config.get_str("SERVICE_NAME", service, environ=environ),
        version=config.get_str("SERVICE_VERSION", UNKNOWN, environ=environ),
        revision=config.get_str("SERVICE_REVISION", UNKNOWN, environ=environ),
        image_digest=config.get_str("SERVICE_IMAGE_DIGEST", UNKNOWN, environ=environ),
        environment=config.get_str("PLATFORM_ENVIRONMENT", UNKNOWN, environ=environ),
        instance=config.get_str("POD_NAME", UNKNOWN, environ=environ),
    )
