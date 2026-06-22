"""A small, thread-safe Prometheus text-exposition implementation.

Only the three metric types this platform actually scrapes are implemented.
Keeping the exposition logic in-repo removes a runtime dependency and makes the
output directly assertable in offline unit tests.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Final

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"

# Chosen for HTTP request latency in a local kind cluster: sub-millisecond
# resolution is noise, and anything above ten seconds is a timeout, not a
# latency signal.
DEFAULT_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_NAME_FIRST = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_:")
_NAME_REST = _NAME_FIRST | frozenset("0123456789")
_LABEL_FIRST = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_LABEL_REST = _LABEL_FIRST | frozenset("0123456789")


def _validate_metric_name(name: str) -> str:
    if not name or name[0] not in _NAME_FIRST or any(c not in _NAME_REST for c in name):
        raise ValueError(f"invalid metric name: {name!r}")
    return name


def _validate_label_name(name: str) -> str:
    if (
        not name
        or name[0] not in _LABEL_FIRST
        or any(c not in _LABEL_REST for c in name)
        or name.startswith("__")
    ):
        raise ValueError(f"invalid label name: {name!r}")
    return name


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def _format_number(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _render_labels(pairs: Sequence[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    body = ",".join(f'{name}="{_escape_label_value(value)}"' for name, value in pairs)
    return "{" + body + "}"


class _Family:
    """Common storage for one metric family and its label permutations."""

    kind = "untyped"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        constant_labels: Mapping[str, str] | None = None,
    ) -> None:
        self.name = _validate_metric_name(name)
        self.documentation = documentation
        self.labelnames = tuple(_validate_label_name(label) for label in labelnames)
        if len(set(self.labelnames)) != len(self.labelnames):
            raise ValueError(f"duplicate label names for {name!r}: {labelnames!r}")
        constant = dict(constant_labels or {})
        for label in constant:
            _validate_label_name(label)
        overlap = set(constant) & set(self.labelnames)
        if overlap:
            raise ValueError(f"constant labels collide with variable labels: {sorted(overlap)}")
        self._constant = tuple(sorted(constant.items()))
        self._lock = threading.Lock()

    def _key(self, labels: Mapping[str, str] | None) -> tuple[str, ...]:
        provided = dict(labels or {})
        missing = set(self.labelnames) - set(provided)
        unexpected = set(provided) - set(self.labelnames)
        if missing or unexpected:
            raise ValueError(
                f"{self.name} expects labels {list(self.labelnames)}, "
                f"missing={sorted(missing)} unexpected={sorted(unexpected)}"
            )
        return tuple(str(provided[label]) for label in self.labelnames)

    def _pairs(self, key: Sequence[str]) -> list[tuple[str, str]]:
        pairs = list(self._constant)
        pairs.extend(zip(self.labelnames, key, strict=True))
        return sorted(pairs)

    def _header(self) -> list[str]:
        return [
            f"# HELP {self.name} {_escape_help(self.documentation)}",
            f"# TYPE {self.name} {self.kind}",
        ]

    def render(self) -> list[str]:  # pragma: no cover - overridden by subclasses
        raise NotImplementedError


class Counter(_Family):
    """A monotonically increasing value."""

    kind = "counter"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError(f"{self.name} is a counter and cannot decrease")
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        key = self._key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def series(self) -> list[tuple[dict[str, str], float]]:
        """Return every labelled series. Used by SLI snapshots, not by scrapes."""
        with self._lock:
            return [
                (dict(zip(self.labelnames, key, strict=True)), value)
                for key, value in self._values.items()
            ]

    def render(self) -> list[str]:
        with self._lock:
            snapshot = sorted(self._values.items())
        lines = self._header()
        for key, value in snapshot:
            lines.append(
                f"{self.name}{_render_labels(self._pairs(key))} {_format_number(value)}"
            )
        return lines


class Gauge(_Family):
    """A value that may move in either direction."""

    kind = "gauge"

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        key = self._key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> list[str]:
        with self._lock:
            snapshot = sorted(self._values.items())
        lines = self._header()
        for key, value in snapshot:
            lines.append(
                f"{self.name}{_render_labels(self._pairs(key))} {_format_number(value)}"
            )
        return lines


class Histogram(_Family):
    """Cumulative bucket counts plus sum and count, as Prometheus expects."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        constant_labels: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(
            name, documentation, labelnames, constant_labels=constant_labels
        )
        if "le" in self.labelnames:
            raise ValueError("'le' is reserved for histogram buckets")
        bounds = tuple(float(bucket) for bucket in buckets)
        if not bounds:
            raise ValueError(f"{name} requires at least one bucket")
        if list(bounds) != sorted(bounds) or len(set(bounds)) != len(bounds):
            raise ValueError(f"{name} buckets must be sorted and unique: {buckets!r}")
        self.buckets = bounds + (math.inf,)
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = [0] * len(self.buckets)
                self._counts[key] = counts
                self._sums[key] = 0.0
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            self._sums[key] += value

    def count(self, **labels: str) -> int:
        key = self._key(labels)
        with self._lock:
            counts = self._counts.get(key)
            return counts[-1] if counts else 0

    def total(self, **labels: str) -> float:
        key = self._key(labels)
        with self._lock:
            return self._sums.get(key, 0.0)

    def quantile(self, q: float, **labels: str) -> float:
        """Approximate a quantile from cumulative buckets.

        Empty series return 0.0. Callers that need fail-closed behaviour (canary
        analysis with no samples) must treat a zero count as a failure themselves.
        """
        if not 0 < q <= 1:
            raise ValueError(f"quantile must be in (0, 1], got {q}")
        key = self._key(labels)
        with self._lock:
            counts = self._counts.get(key)
            if not counts or counts[-1] == 0:
                return 0.0
            target = q * counts[-1]
            previous = 0
            lower = 0.0
            for bound, cumulative in zip(self.buckets, counts, strict=True):
                if cumulative >= target:
                    span = cumulative - previous
                    frac = (target - previous) / span if span else 1.0
                    if math.isinf(bound):
                        return lower
                    return lower + frac * (bound - lower)
                previous = cumulative
                if not math.isinf(bound):
                    lower = bound
            return lower

    def render(self) -> list[str]:
        with self._lock:
            snapshot = sorted(
                (key, list(counts), self._sums[key])
                for key, counts in self._counts.items()
            )
        lines = self._header()
        for key, counts, total in snapshot:
            base = self._pairs(key)
            for bound, cumulative in zip(self.buckets, counts, strict=True):
                pairs = sorted([*base, ("le", _format_number(bound))])
                lines.append(f"{self.name}_bucket{_render_labels(pairs)} {cumulative}")
            rendered = _render_labels(base)
            lines.append(f"{self.name}_sum{rendered} {_format_number(total)}")
            lines.append(f"{self.name}_count{rendered} {counts[-1]}")
        return lines


class Registry:
    """Holds metric families and renders the scrape response."""

    def __init__(self, *, constant_labels: Mapping[str, str] | None = None) -> None:
        self._constant = dict(constant_labels or {})
        self._families: dict[str, _Family] = {}
        self._lock = threading.Lock()

    def _register(self, family: _Family) -> _Family:
        with self._lock:
            if family.name in self._families:
                raise ValueError(f"metric {family.name!r} is already registered")
            self._families[family.name] = family
        return family

    def counter(
        self, name: str, documentation: str, labelnames: Sequence[str] = ()
    ) -> Counter:
        metric = Counter(
            name, documentation, labelnames, constant_labels=self._constant
        )
        self._register(metric)
        return metric

    def gauge(
        self, name: str, documentation: str, labelnames: Sequence[str] = ()
    ) -> Gauge:
        metric = Gauge(name, documentation, labelnames, constant_labels=self._constant)
        self._register(metric)
        return metric

    def histogram(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str] = (),
        *,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        metric = Histogram(
            name,
            documentation,
            labelnames,
            buckets=buckets,
            constant_labels=self._constant,
        )
        self._register(metric)
        return metric

    def names(self) -> Iterable[str]:
        with self._lock:
            return tuple(sorted(self._families))

    def render(self) -> str:
        with self._lock:
            families = [self._families[name] for name in sorted(self._families)]
        lines: list[str] = []
        for family in families:
            lines.extend(family.render())
        return "\n".join(lines) + "\n" if lines else ""
