"""Traffic shaping.

Two properties matter for this generator to be useful as test equipment:

* **Deterministic composition.** The sequence of operations must be identical
  across runs so a canary comparison measures the deployment, not the traffic.
* **Paced arrival.** Requests are scheduled against a fixed start time rather
  than by sleeping a fixed gap after each response, so a slow upstream causes
  the generator to catch up instead of silently reducing the offered rate and
  hiding the regression it was meant to expose.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from platform_common import config

CATALOG: Sequence[str] = (
    "widget",
    "gadget",
    "sprocket",
    "flange",
    "bearing",
    "gasket",
)


@dataclass(frozen=True)
class Operation:
    """One planned request."""

    kind: str
    payload: dict | None = None


@dataclass(frozen=True)
class TrafficProfile:
    """Requested traffic shape."""

    requests_per_second: float = 5.0
    read_weight: int = 3
    write_weight: int = 1
    invalid_ratio: float = 0.0
    max_quantity: int = 5

    @classmethod
    def from_environment(
        cls, *, environ: config.Environ | None = None
    ) -> TrafficProfile:
        return cls(
            requests_per_second=config.get_float(
                "LOAD_RPS", 5.0, minimum=0.0, maximum=500.0, environ=environ
            ),
            read_weight=config.get_int(
                "LOAD_READ_WEIGHT", 3, minimum=0, maximum=1000, environ=environ
            ),
            write_weight=config.get_int(
                "LOAD_WRITE_WEIGHT", 1, minimum=0, maximum=1000, environ=environ
            ),
            invalid_ratio=config.get_float(
                "LOAD_INVALID_RATIO", 0.0, minimum=0.0, maximum=1.0, environ=environ
            ),
            max_quantity=config.get_int(
                "LOAD_MAX_QUANTITY", 5, minimum=1, maximum=100, environ=environ
            ),
        )

    def __post_init__(self) -> None:
        if self.read_weight + self.write_weight <= 0:
            raise ValueError("read_weight + write_weight must be greater than zero")


class TrafficPlan:
    """Produces the deterministic operation sequence."""

    def __init__(self, profile: TrafficProfile) -> None:
        self.profile = profile
        self._counter = itertools.count()

    def _is_invalid(self, index: int) -> bool:
        ratio = self.profile.invalid_ratio
        if ratio <= 0:
            return False
        return int((index + 1) * ratio) > int(index * ratio)

    def next_operation(self) -> Operation:
        index = next(self._counter)
        cycle = self.profile.read_weight + self.profile.write_weight
        position = index % cycle

        if position < self.profile.write_weight:
            if self._is_invalid(index):
                # A deliberately malformed body: exercises the API's 4xx path so
                # dashboards can distinguish client error from server error.
                return Operation("create_invalid", {"item": "", "quantity": 0})
            item = CATALOG[index % len(CATALOG)]
            quantity = (index % self.profile.max_quantity) + 1
            return Operation("create", {"item": item, "quantity": quantity})
        return Operation("list")

    def schedule(self, start: float, count: int | None = None) -> Iterator[float]:
        """Yield absolute send times for a fixed offered rate."""
        rate = self.profile.requests_per_second
        if rate <= 0:
            return
        interval = 1.0 / rate
        index = 0
        while count is None or index < count:
            yield start + index * interval
            index += 1


def sleep_until(deadline: float, *, now=time.monotonic, sleep=time.sleep) -> float:
    """Sleep until ``deadline``; return how far behind schedule we already are."""
    remaining = deadline - now()
    if remaining > 0:
        sleep(remaining)
        return 0.0
    return -remaining
