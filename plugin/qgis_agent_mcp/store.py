from __future__ import annotations

import base64
import hashlib
import mimetypes
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path


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


class ArtifactStore:
    """TTL/LRU registry with hard per-item, total-size, and read bounds."""

    def __init__(
        self,
        ttl_seconds=900,
        max_items=128,
        max_item_bytes=32 * 1024 * 1024,
        max_total_bytes=64 * 1024 * 1024,
        max_read_bytes=1024 * 1024,
    ):
        self.ttl_seconds = int(ttl_seconds)
        self.max_items = int(max_items)
        self.max_item_bytes = int(max_item_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.max_read_bytes = int(max_read_bytes)
        self._values = OrderedDict()
        self._total_bytes = 0

    def put_bytes(self, value, mime_type="application/octet-stream", name=None, metadata=None):
        payload = bytes(value)
        if len(payload) > self.max_item_bytes:
            raise ValueError("Artifact exceeds the per-item size limit")
        self.prune()
        while self._values and (
            len(self._values) >= self.max_items
            or self._total_bytes + len(payload) > self.max_total_bytes
        ):
            self._evict_oldest()
        if len(payload) > self.max_total_bytes:
            raise ValueError("Artifact exceeds the registry size limit")
        artifact_id = "a_" + uuid.uuid4().hex
        entry = {
            "created": time.monotonic(),
            "data": payload,
            "mime_type": mime_type or "application/octet-stream",
            "name": name,
            "metadata": metadata or {},
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        self._values[artifact_id] = entry
        self._total_bytes += len(payload)
        return self._descriptor(artifact_id, entry)

    def put_file(self, path, mime_type=None, name=None, metadata=None):
        source = Path(path)
        size = source.stat().st_size
        if size > self.max_item_bytes:
            raise ValueError("Artifact exceeds the per-item size limit")
        return self.put_bytes(
            source.read_bytes(),
            mime_type or mimetypes.guess_type(source.name)[0],
            name or source.name,
            {**(metadata or {}), "source": str(source)},
        )

    def read(self, artifact_id, offset=0, length=None):
        self.prune()
        entry = self._values.get(artifact_id)
        if entry is None:
            raise KeyError("Unknown or expired artifact")
        offset = max(0, int(offset))
        requested = self.max_read_bytes if length is None else int(length)
        length = max(0, min(requested, self.max_read_bytes))
        data = entry["data"]
        chunk = data[offset : offset + length]
        self._values.move_to_end(artifact_id)
        return {
            **self._descriptor(artifact_id, entry),
            "offset": offset,
            "length": len(chunk),
            "data": base64.b64encode(chunk).decode("ascii"),
            "encoding": "base64",
            "eof": offset + len(chunk) >= len(data),
        }

    def list(self):
        self.prune()
        return [self._descriptor(key, value) for key, value in self._values.items()]

    def release(self, artifact_id):
        entry = self._values.pop(artifact_id, None)
        if entry is None:
            return False
        self._total_bytes -= len(entry["data"])
        return True

    def prune(self):
        now = time.monotonic()
        for key in list(self._values):
            if now - self._values[key]["created"] > self.ttl_seconds:
                self.release(key)

    def _evict_oldest(self):
        key = next(iter(self._values))
        self.release(key)

    def _descriptor(self, artifact_id, entry):
        return {
            "artifact_id": artifact_id,
            "name": entry["name"],
            "mime_type": entry["mime_type"],
            "size": len(entry["data"]),
            "sha256": entry["sha256"],
            "ttl_seconds": self.ttl_seconds,
            "metadata": entry["metadata"],
        }
