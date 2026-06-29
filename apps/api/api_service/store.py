"""In-memory order and task state.

Deliberately not durable. The platform is what this repository demonstrates;
introducing a database would add operational surface that proves nothing about
GitOps, progressive delivery, or SLOs. The bounded sizes below exist so a
long-running load test cannot exhaust node memory and turn a delivery demo into
an unrelated out-of-memory incident.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_ORDERS = 5000
MAX_QUEUE_DEPTH = 2000
CLAIM_TIMEOUT_SECONDS = 30.0


class OrderStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Order:
    order_id: str
    item: str
    quantity: int
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    trace_id: str = ""
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "item": self.item,
            "quantity": self.quantity,
            "status": str(self.status),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "trace_id": self.trace_id,
            "detail": self.detail,
        }


@dataclass
class Task:
    """A unit of queued work.

    ``traceparent`` is stored on the message itself. Without it the worker span
    would start a new trace and the queued half of the request would be
    invisible when reading the trace for a slow order.
    """

    task_id: str
    order_id: str
    item: str
    quantity: int
    traceparent: str = ""
    enqueued_at: float = field(default_factory=time.time)
    claimed_at: float | None = None
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "order_id": self.order_id,
            "item": self.item,
            "quantity": self.quantity,
            "traceparent": self.traceparent,
            "enqueued_at": self.enqueued_at,
            "attempts": self.attempts,
        }


class QueueFull(RuntimeError):
    """Raised when the pending queue is at its bounded depth."""


class OrderStore:
    """Thread-safe order records plus the task queue that drives the worker."""

    def __init__(
        self,
        *,
        max_orders: int = MAX_ORDERS,
        max_queue_depth: int = MAX_QUEUE_DEPTH,
        claim_timeout: float = CLAIM_TIMEOUT_SECONDS,
        clock=time.time,
    ) -> None:
        self._lock = threading.Lock()
        self._orders: dict[str, Order] = {}
        self._order_sequence = itertools.count(1)
        self._task_sequence = itertools.count(1)
        self._pending: deque[Task] = deque()
        self._inflight: dict[str, Task] = {}
        self._max_orders = max_orders
        self._max_queue_depth = max_queue_depth
        self._claim_timeout = claim_timeout
        self._clock = clock
        self.completed_count = 0
        self.failed_count = 0
        self.requeued_count = 0

    def create_order(
        self, item: str, quantity: int, *, traceparent: str = "", trace_id: str = ""
    ) -> tuple[Order, Task]:
        with self._lock:
            if len(self._pending) >= self._max_queue_depth:
                raise QueueFull(
                    f"pending queue is at its bounded depth of {self._max_queue_depth}"
                )
            order_id = f"ord-{next(self._order_sequence):06d}"
            order = Order(
                order_id=order_id, item=item, quantity=quantity, trace_id=trace_id
            )
            self._orders[order_id] = order
            self._evict_locked()
            task = Task(
                task_id=f"tsk-{next(self._task_sequence):06d}",
                order_id=order_id,
                item=item,
                quantity=quantity,
                traceparent=traceparent,
                enqueued_at=self._clock(),
            )
            self._pending.append(task)
            return order, task

    def _evict_locked(self) -> None:
        """Drop the oldest terminal orders once the bound is exceeded."""
        overflow = len(self._orders) - self._max_orders
        if overflow <= 0:
            return
        removable = [
            order_id
            for order_id, order in self._orders.items()
            if order.status in (OrderStatus.COMPLETED, OrderStatus.FAILED)
        ]
        for order_id in removable[:overflow]:
            del self._orders[order_id]

    def get_order(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def list_orders(self, limit: int = 50) -> list[Order]:
        with self._lock:
            orders = sorted(
                self._orders.values(), key=lambda order: order.created_at, reverse=True
            )
            return orders[:limit]

    def claim_task(self) -> Task | None:
        """Hand the next pending task to a worker, recovering abandoned claims first."""
        with self._lock:
            self._requeue_expired_locked()
            if not self._pending:
                return None
            task = self._pending.popleft()
            task.claimed_at = self._clock()
            task.attempts += 1
            self._inflight[task.task_id] = task
            order = self._orders.get(task.order_id)
            if order is not None:
                order.status = OrderStatus.PROCESSING
                order.updated_at = self._clock()
            return task

    def _requeue_expired_locked(self) -> None:
        """Return tasks to the queue when a worker died holding them.

        Without this, a worker evicted mid-task would strand the order in
        ``processing`` forever and the queue-depth metric would understate the
        real backlog.
        """
        now = self._clock()
        expired = [
            task
            for task in self._inflight.values()
            if task.claimed_at is not None
            and now - task.claimed_at > self._claim_timeout
        ]
        for task in expired:
            del self._inflight[task.task_id]
            task.claimed_at = None
            self._pending.appendleft(task)
            self.requeued_count += 1

    def complete_task(
        self, task_id: str, *, succeeded: bool = True, detail: str = ""
    ) -> Order | None:
        with self._lock:
            task = self._inflight.pop(task_id, None)
            if task is None:
                return None
            order = self._orders.get(task.order_id)
            if order is not None:
                order.status = (
                    OrderStatus.COMPLETED if succeeded else OrderStatus.FAILED
                )
                order.detail = detail
                order.updated_at = self._clock()
            if succeeded:
                self.completed_count += 1
            else:
                self.failed_count += 1
            return order

    def stats(self) -> dict[str, int]:
        with self._lock:
            by_status = {status: 0 for status in OrderStatus}
            for order in self._orders.values():
                by_status[order.status] += 1
            return {
                "orders": len(self._orders),
                "pending_tasks": len(self._pending),
                "inflight_tasks": len(self._inflight),
                "completed": self.completed_count,
                "failed": self.failed_count,
                "requeued": self.requeued_count,
                **{f"orders_{status}": count for status, count in by_status.items()},
            }
