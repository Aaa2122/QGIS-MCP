from __future__ import annotations

import time
import uuid
from collections import deque


class EventLog:
    def __init__(self, capacity=2000):
        self._items = deque(maxlen=capacity)
        self._sequence = 0

    @property
    def sequence(self):
        return self._sequence

    def add(self, category, message, level="info", data=None):
        self._sequence += 1
        item = {
            "sequence": self._sequence,
            "time": time.time(),
            "category": category,
            "level": level,
            "message": str(message),
        }
        if data is not None:
            item["data"] = data
        self._items.append(item)
        return item

    def read(self, after=0, level=None, limit=100):
        values = [
            item
            for item in self._items
            if item["sequence"] > after and (level is None or item["level"] == level)
        ]
        return {
            "events": values[:limit],
            "latest": self._sequence,
            "has_more": len(values) > limit,
        }


class HandleStore:
    def __init__(self, ttl_seconds=900, max_handles=100):
        self.ttl_seconds = ttl_seconds
        self.max_handles = max_handles
        self._values = {}

    def put(self, value, kind="items", metadata=None):
        self.prune()
        if len(self._values) >= self.max_handles:
            oldest = min(self._values, key=lambda key: self._values[key]["created"])
            self._values.pop(oldest, None)
        handle = "h_" + uuid.uuid4().hex
        self._values[handle] = {
            "created": time.monotonic(),
            "kind": kind,
            "value": value,
            "metadata": metadata or {},
        }
        length = len(value) if hasattr(value, "__len__") else None
        return {"handle": handle, "kind": kind, "count": length, "ttl_seconds": self.ttl_seconds}

    def read(self, handle, offset=0, limit=100):
        self.prune()
        entry = self._values.get(handle)
        if entry is None:
            raise KeyError("Unknown or expired handle")
        value = entry["value"]
        if isinstance(value, (list, tuple)):
            page = value[offset : offset + limit]
            return {
                "handle": handle,
                "kind": entry["kind"],
                "offset": offset,
                "items": page,
                "count": len(value),
                "has_more": offset + len(page) < len(value),
                "metadata": entry["metadata"],
            }
        if isinstance(value, dict):
            items = list(value.items())
            page = items[offset : offset + limit]
            return {
                "handle": handle,
                "kind": entry["kind"],
                "offset": offset,
                "items": [{"key": key, "value": item} for key, item in page],
                "count": len(items),
                "has_more": offset + len(page) < len(items),
                "metadata": entry["metadata"],
            }
        return {"handle": handle, "kind": entry["kind"], "value": value}

    def prune(self):
        now = time.monotonic()
        expired = [
            key
            for key, item in self._values.items()
            if now - item["created"] > self.ttl_seconds
        ]
        for key in expired:
            self._values.pop(key, None)

