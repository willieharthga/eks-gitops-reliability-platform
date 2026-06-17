"""Shared runtime for the synthetic platform services.

The package deliberately depends only on the Python standard library. Every
service image therefore has a small dependency surface, and the repository
verification path can exercise real service behavior without installing
packages, reaching the network, or starting Docker.
"""

from __future__ import annotations

__all__ = [
    "config",
    "httpapp",
    "logging",
    "metrics",
    "runtime",
    "tracing",
    "version",
]
