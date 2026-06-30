"""Deterministic fault injection.

Canary analysis is only a trustworthy test if the "bad" version fails the same
way on every run. Random failure at a target ratio produces a binomial spread
that occasionally passes analysis, which would make a rollback test flaky and
therefore worthless as evidence.

This injector instead spreads failures evenly across a request sequence: for
ratio ``r`` and request ``n`` it fails when ``floor(n * r) > floor((n - 1) * r)``.
That yields exactly ``floor(n * r)`` failures after ``n`` requests, with no
randomness, so a canary either fails analysis every time or never does.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from platform_common import config


@dataclass(frozen=True)
class FaultProfile:
    """Fault behavior requested by configuration."""

    error_ratio: float = 0.0
    latency_seconds: float = 0.0
    latency_ratio: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.error_ratio > 0 or (
            self.latency_seconds > 0 and self.latency_ratio > 0
        )

    @classmethod
    def from_environment(cls, *, environ: config.Environ | None = None) -> FaultProfile:
        role = config.get_str("POD_ROLE", "stable", environ=environ).strip().lower() or "stable"
        base_error = config.get_float(
            "FAULT_ERROR_RATIO", 0.0, minimum=0.0, maximum=1.0, environ=environ
        )
        canary_error = config.get_float(
            "CANARY_FAULT_ERROR_RATIO", 0.0, minimum=0.0, maximum=1.0, environ=environ
        )
        # Canary-only faults leave the stable ReplicaSet honest. A shared
        # FAULT_ERROR_RATIO would fail both sides and make rollback look like
        # an outage of the good version.
        error_ratio = canary_error if role == "canary" and canary_error > 0 else base_error
        return cls(
            error_ratio=error_ratio,
            latency_seconds=config.get_float(
                "FAULT_LATENCY_SECONDS", 0.0, minimum=0.0, maximum=30.0, environ=environ
            ),
            latency_ratio=config.get_float(
                "FAULT_LATENCY_RATIO", 0.0, minimum=0.0, maximum=1.0, environ=environ
            ),
        )


class FaultInjector:
    """Applies a :class:`FaultProfile` deterministically and thread-safely."""

    def __init__(self, profile: FaultProfile, *, sleep=time.sleep) -> None:
        self.profile = profile
        self._sleep = sleep
        self._lock = threading.Lock()
        self._error_calls = 0
        self._latency_calls = 0
        self.injected_errors = 0
        self.injected_latencies = 0

    @staticmethod
    def _crosses(previous: int, current: int, ratio: float) -> bool:
        return math.floor(current * ratio) > math.floor(previous * ratio)

    def should_fail(self) -> bool:
        """Return whether this call is the next scheduled failure."""
        if self.profile.error_ratio <= 0:
            return False
        with self._lock:
            previous = self._error_calls
            self._error_calls += 1
            failing = self._crosses(previous, self._error_calls, self.profile.error_ratio)
            if failing:
                self.injected_errors += 1
            return failing

    def apply_latency(self) -> float:
        """Sleep if this call is the next scheduled slow one; return seconds added."""
        if self.profile.latency_seconds <= 0 or self.profile.latency_ratio <= 0:
            return 0.0
        with self._lock:
            previous = self._latency_calls
            self._latency_calls += 1
            slow = self._crosses(
                previous, self._latency_calls, self.profile.latency_ratio
            )
            if slow:
                self.injected_latencies += 1
        if not slow:
            return 0.0
        self._sleep(self.profile.latency_seconds)
        return self.profile.latency_seconds
