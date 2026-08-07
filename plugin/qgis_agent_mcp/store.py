from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path


class PersistentDiagnostics:
    """Crash-resilient record of bridge calls without parameter values."""

    def __init__(self, path=None, history_limit=100, flush_interval_seconds=1.0):
        self.path = Path(
            path or Path.home() / ".qgis-mcp" / "diagnostics.json"
        ).expanduser()
        self.history_limit = max(10, int(history_limit))
        self.flush_interval_seconds = max(0.1, float(flush_interval_seconds))
        self._last_save = time.monotonic()
        self._dirty = False
        self._write_count = 0
        self._state = self._load()
        self._previous_interruption = list(self._state.get("active") or [])
        if self._previous_interruption:
            self._state.setdefault("history", []).append(
                {
                    "status": "interrupted",
                    "detected_at": time.time(),
                    "call_stack": self._previous_interruption,
                }
            )
            self._state["active"] = []
            self._save()

    @property
    def previous_interruption(self):
        return list(self._previous_interruption)

    def begin(self, method, params, durable=True):
        token = uuid.uuid4().hex
        entry = {
            "id": token,
            "method": str(method),
            "parameter_names": sorted(str(key) for key in (params or {})),
            "started_at": time.time(),
            "_durable": bool(durable),
        }
        self._state.setdefault("active", []).append(entry)
        self._dirty = True
        if durable:
            self._save()
        return token

    def finish(self, token, status, exception=None, durable=None):
        active = self._state.setdefault("active", [])
        entry = next((item for item in active if item.get("id") == token), None)
        if entry is None:
            return
        active.remove(entry)
        was_durable = bool(entry.pop("_durable", False))
        durable = was_durable if durable is None else bool(durable)
        finished = {
            **entry,
            "status": str(status),
            "finished_at": time.time(),
        }
        finished["duration_seconds"] = round(
            finished["finished_at"] - float(entry["started_at"]), 3
        )
        if exception:
            finished["exception"] = str(exception)
        history = self._state.setdefault("history", [])
        history.append(finished)
        self._state["history"] = history[-self.history_limit :]
        self._dirty = True
        if durable:
            self._save()
        else:
            self._maybe_save()

    def snapshot(self, history_limit=20):
        self.flush()
        limit = max(1, min(int(history_limit), self.history_limit))
        return {
            "path": str(self.path),
            "active": [
                {key: value for key, value in item.items() if key != "_durable"}
                for item in self._state.get("active") or []
            ],
            "previous_interruption": self.previous_interruption,
            "history": list(self._state.get("history") or [])[-limit:],
            "parameter_values_persisted": False,
            "batched_writes": True,
            "write_count": self._write_count,
        }

    def flush(self):
        if self._dirty:
            self._save()

    def _maybe_save(self):
        if time.monotonic() - self._last_save >= self.flush_interval_seconds:
            self._save()

    def _load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.setdefault("version", 1)
                value.setdefault("active", [])
                value.setdefault("history", [])
                return value
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {"version": 1, "active": [], "history": []}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=self.path.name + ".", dir=str(self.path.parent)
            )
            try:
                persisted = {
                    **self._state,
                    "active": [
                        {key: value for key, value in item.items() if key != "_durable"}
                        for item in self._state.get("active", [])
                        if item.get("_durable")
                    ],
                }
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(persisted, stream, ensure_ascii=False, separators=(",", ":"))
                os.replace(temporary, self.path)
                self._dirty = False
                self._last_save = time.monotonic()
                self._write_count += 1
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            # Diagnostics must never make a QGIS operation fail.
            return


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
    def __init__(
        self,
        ttl_seconds=900,
        max_handles=100,
        max_read_bytes=48 * 1024,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_handles = max_handles
        self.max_read_bytes = max(1024, int(max_read_bytes))
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
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 1000))
        value = entry["value"]
        if isinstance(value, (list, tuple)):
            page, next_offset = self._bounded_page(
                list(value), offset, limit, handle, entry, keyed=False
            )
            return {
                "handle": handle,
                "kind": entry["kind"],
                "offset": offset,
                "items": page,
                "count": len(value),
                "has_more": next_offset < len(value),
                "next_offset": next_offset if next_offset < len(value) else None,
                "metadata": entry["metadata"],
            }
        if isinstance(value, dict):
            items = list(value.items())
            page, next_offset = self._bounded_page(
                items, offset, limit, handle, entry, keyed=True
            )
            return {
                "handle": handle,
                "kind": entry["kind"],
                "offset": offset,
                "items": page,
                "count": len(items),
                "has_more": next_offset < len(items),
                "next_offset": next_offset if next_offset < len(items) else None,
                "metadata": entry["metadata"],
            }
        if isinstance(value, str):
            # A character window of one quarter the byte budget is valid UTF-8
            # and cannot exceed the configured transport budget.
            end = min(len(value), offset + self.max_read_bytes // 4)
            return {
                "handle": handle,
                "kind": entry["kind"],
                "offset": offset,
                "value": value[offset:end],
                "count": len(value),
                "has_more": end < len(value),
                "next_offset": end if end < len(value) else None,
            }
        return {"handle": handle, "kind": entry["kind"], "value": value}

    def _bounded_page(self, values, offset, limit, parent_handle, entry, *, keyed):
        page = []
        used_bytes = 0
        next_offset = offset
        for index in range(offset, min(len(values), offset + limit)):
            if keyed:
                key, raw_value = values[index]
                candidate = {"key": str(key), "value": raw_value}
            else:
                raw_value = values[index]
                candidate = raw_value
            encoded_size = len(
                json.dumps(
                    candidate,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            if encoded_size > self.max_read_bytes:
                descriptor = self.put(
                    raw_value,
                    kind="{}.nested".format(entry["kind"]),
                    metadata={
                        "parent_handle": parent_handle,
                        "parent_offset": index,
                    },
                )
                descriptor.update(
                    {
                        "truncated": True,
                        "message": "Read this nested handle for the oversized value.",
                    }
                )
                candidate = (
                    {"key": str(values[index][0]), "value": descriptor}
                    if keyed
                    else descriptor
                )
                encoded_size = len(
                    json.dumps(candidate, separators=(",", ":")).encode("utf-8")
                )
            if page and used_bytes + encoded_size > self.max_read_bytes:
                break
            page.append(candidate)
            used_bytes += encoded_size
            next_offset = index + 1
        return page, next_offset

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
