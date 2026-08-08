from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

CONTROL_METHODS = {
    "bridge.cancel",
    "bridge.hello",
    "operation.control",
    "runtime.tasks",
}


@dataclass
class ScheduledBridgeRequest:
    socket: object
    request: dict
    category: str
    enqueued_at: float
    deadline_at: float

    def expired(self, now=None):
        return (time.monotonic() if now is None else float(now)) >= self.deadline_at


class BridgeScheduler:
    """Bounded, priority-aware queue for calls entering QGIS's main thread."""

    _ORDER = ("control", "read", "mutation")

    def __init__(
        self,
        mutation_methods=(),
        *,
        max_pending=50,
        control_reserve=5,
        deadline_seconds=120.0,
    ):
        self.mutation_methods = set(mutation_methods)
        self.max_pending = max(1, int(max_pending))
        self.control_reserve = max(
            0, min(int(control_reserve), self.max_pending - 1)
        )
        self.deadline_seconds = max(0.1, float(deadline_seconds))
        self._queues = {name: deque() for name in self._ORDER}
        self._stats = {
            "enqueued": 0,
            "processed": 0,
            "rejected": 0,
            "expired": 0,
            "cancelled": 0,
            "max_depth": 0,
            "queue_wait_ms_total": 0.0,
            "queue_wait_ms_max": 0.0,
        }
        self._reads_since_mutation = 0
        self.read_weight = 3

    @property
    def pending(self):
        return sum(len(queue) for queue in self._queues.values())

    def classify(self, request):
        method = request.get("method") if isinstance(request, dict) else None
        if method in CONTROL_METHODS:
            return "control"
        if method in self.mutation_methods:
            return "mutation"
        return "read"

    def enqueue(self, socket, request, now=None):
        now = time.monotonic() if now is None else float(now)
        category = self.classify(request)
        normal_limit = self.max_pending - self.control_reserve
        limit = self.max_pending if category == "control" else normal_limit
        if self.pending >= limit:
            self._stats["rejected"] += 1
            return False
        self._queues[category].append(
            ScheduledBridgeRequest(
                socket=socket,
                request=request,
                category=category,
                enqueued_at=now,
                deadline_at=now + self.deadline_seconds,
            )
        )
        self._stats["enqueued"] += 1
        self._stats["max_depth"] = max(self._stats["max_depth"], self.pending)
        return True

    def pop_next(self):
        if self._queues["control"]:
            return self._queues["control"].popleft()
        if self._queues["mutation"] and (
            not self._queues["read"] or self._reads_since_mutation >= self.read_weight
        ):
            self._reads_since_mutation = 0
            return self._queues["mutation"].popleft()
        if self._queues["read"]:
            self._reads_since_mutation += 1
            return self._queues["read"].popleft()
        if self._queues["mutation"]:
            self._reads_since_mutation = 0
            return self._queues["mutation"].popleft()
        return None

    def cancel_request(self, socket, request_id):
        for category in self._ORDER:
            queue = self._queues[category]
            for item in tuple(queue):
                if item.socket is socket and item.request.get("id") == request_id:
                    queue.remove(item)
                    self._stats["cancelled"] += 1
                    return True
        return False

    def discard_socket(self, socket):
        removed = 0
        for category in self._ORDER:
            queue = self._queues[category]
            retained = deque(item for item in queue if item.socket is not socket)
            removed += len(queue) - len(retained)
            self._queues[category] = retained
        return removed

    def record(self, queue_wait_ms, *, expired=False):
        wait = max(0.0, float(queue_wait_ms))
        self._stats["processed"] += 1
        self._stats["queue_wait_ms_total"] += wait
        self._stats["queue_wait_ms_max"] = max(
            self._stats["queue_wait_ms_max"], wait
        )
        if expired:
            self._stats["expired"] += 1

    def clear(self):
        for queue in self._queues.values():
            queue.clear()

    def snapshot(self):
        processed = self._stats["processed"]
        return {
            "pending": self.pending,
            "queues": {
                category: len(self._queues[category]) for category in self._ORDER
            },
            "max_pending": self.max_pending,
            "control_reserve": self.control_reserve,
            "read_weight": self.read_weight,
            "deadline_seconds": self.deadline_seconds,
            **{
                key: round(value, 3) if isinstance(value, float) else value
                for key, value in self._stats.items()
            },
            "queue_wait_ms_mean": round(
                self._stats["queue_wait_ms_total"] / processed, 3
            )
            if processed
            else 0.0,
        }
