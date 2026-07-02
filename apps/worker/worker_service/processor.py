"""Task processing rules.

The "work" is a bounded, deterministic sleep so that queue depth, worker
concurrency, and autoscaling behave predictably during load tests. Randomised
durations would make the scaling evidence in M9 impossible to reproduce.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from platform_common import config


@dataclass(frozen=True)
class ProcessingProfile:
    """How long a task takes and when it deliberately fails."""

    base_seconds: float = 0.05
    per_unit_seconds: float = 0.01
    max_seconds: float = 5.0
    fail_on_item: str = ""

    @classmethod
    def from_environment(
        cls, *, environ: config.Environ | None = None
    ) -> ProcessingProfile:
        return cls(
            base_seconds=config.get_float(
                "WORK_BASE_SECONDS", 0.05, minimum=0.0, maximum=10.0, environ=environ
            ),
            per_unit_seconds=config.get_float(
                "WORK_PER_UNIT_SECONDS", 0.01, minimum=0.0, maximum=1.0, environ=environ
            ),
            max_seconds=config.get_float(
                "WORK_MAX_SECONDS", 5.0, minimum=0.01, maximum=60.0, environ=environ
            ),
            fail_on_item=config.get_str("WORK_FAIL_ON_ITEM", "", environ=environ),
        )

    def duration_for(self, quantity: int) -> float:
        return min(
            self.max_seconds, self.base_seconds + self.per_unit_seconds * max(quantity, 0)
        )


@dataclass(frozen=True)
class ProcessingResult:
    succeeded: bool
    detail: str
    duration_seconds: float


class TaskProcessor:
    """Applies a :class:`ProcessingProfile` to one task."""

    def __init__(self, profile: ProcessingProfile, *, sleep=time.sleep) -> None:
        self.profile = profile
        self._sleep = sleep

    def process(self, item: str, quantity: int) -> ProcessingResult:
        duration = self.profile.duration_for(quantity)
        self._sleep(duration)
        if self.profile.fail_on_item and item == self.profile.fail_on_item:
            return ProcessingResult(
                succeeded=False,
                detail=f"item {item!r} is configured to fail",
                duration_seconds=duration,
            )
        return ProcessingResult(
            succeeded=True,
            detail=f"processed {quantity} x {item}",
            duration_seconds=duration,
        )
