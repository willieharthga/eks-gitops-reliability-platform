"""Make the service packages importable without installing anything.

The services are deployed as container images, not as published Python
distributions, so there is no package to `pip install` before testing. The
search path is derived from apps/services.json rather than hard-coded, which
means adding a service to the catalogue cannot leave its tests silently
unable to import it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APPS = REPOSITORY_ROOT / "apps"
CATALOG_PATH = APPS / "services.json"

CATALOG = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
SERVICES = CATALOG["services"]


def _install_paths() -> list[Path]:
    paths = [APPS / "shared"]
    paths.extend(APPS / service["directory"] for service in SERVICES)
    return paths


for _path in _install_paths():
    _entry = str(_path)
    if _entry not in sys.path:
        sys.path.insert(0, _entry)


def service_entry(name: str) -> dict:
    """Return one catalogue entry by service name."""
    for service in SERVICES:
        if service["name"] == name:
            return service
    raise KeyError(f"service {name!r} is not in {CATALOG_PATH.name}")
