"""Environment configuration with explicit validation.

Services must fail fast and loudly on malformed configuration rather than
silently substituting a default that hides a broken deployment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


class ConfigError(ValueError):
    """Raised when an environment value is present but unusable."""


Environ = Mapping[str, str]


def _source(environ: Environ | None) -> Environ:
    return os.environ if environ is None else environ


def _present(environ: Environ, name: str) -> str | None:
    raw = environ.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def get_str(name: str, default: str, *, environ: Environ | None = None) -> str:
    """Return a trimmed string value, falling back to ``default`` when unset."""
    value = _present(_source(environ), name)
    return default if value is None else value


def require_str(name: str, *, environ: Environ | None = None) -> str:
    """Return a required string value or raise :class:`ConfigError`."""
    value = _present(_source(environ), name)
    if value is None:
        raise ConfigError(f"{name} is required but was not set")
    return value


def get_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    environ: Environ | None = None,
) -> int:
    """Return an integer value inside the documented inclusive bounds."""
    raw = _present(_source(environ), name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw, 10)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def get_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    environ: Environ | None = None,
) -> float:
    """Return a float value inside the documented inclusive bounds."""
    raw = _present(_source(environ), name)
    if raw is None:
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value != value:  # NaN compares unequal to itself and breaks bounds checks.
        raise ConfigError(f"{name} must be a real number, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def get_bool(name: str, default: bool, *, environ: Environ | None = None) -> bool:
    """Return a boolean value using an explicit accepted-token list."""
    raw = _present(_source(environ), name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(
        f"{name} must be one of {sorted(_TRUE | _FALSE)}, got {raw!r}"
    )


def get_url(name: str, default: str, *, environ: Environ | None = None) -> str:
    """Return an ``http``/``https`` URL, rejecting other schemes."""
    value = get_str(name, default, environ=environ)
    if value and not value.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must be an http or https URL, got {value!r}")
    return value
