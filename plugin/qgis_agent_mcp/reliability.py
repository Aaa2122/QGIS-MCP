from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path


class IdempotencyConflict(ValueError):
    pass


class MutationGuard:
    """Bounded TTL cache for replay-safe mutation results."""

    def __init__(
        self,
        max_entries=512,
        ttl_seconds=3600,
        path=None,
        max_storage_bytes=8 * 1024 * 1024,
        max_result_bytes=256 * 1024,
    ):
        self.max_entries = int(max_entries)
        self.ttl_seconds = float(ttl_seconds)
        self.path = Path(path) if path else None
        self.max_storage_bytes = int(max_storage_bytes)
        self.max_result_bytes = int(max_result_bytes)
        self._entries = OrderedDict()
        self._load()
        self.prune()

    @staticmethod
    def fingerprint(method, params):
        payload = json.dumps(
            {"method": method, "params": params},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def lookup(self, key, method, params):
        self.prune()
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        fingerprint = self.fingerprint(method, params)
        if entry["fingerprint"] != fingerprint:
            raise IdempotencyConflict(
                "Idempotency key was already used with different arguments"
            )
        self._entries.move_to_end(key)
        return True, entry["result"]

    def remember(self, key, method, params, result):
        self.prune()
        self._entries[key] = {
            "created": time.time(),
            "fingerprint": self.fingerprint(method, params),
            "result": result,
        }
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        self._save()

    def prune(self):
        now = time.time()
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry["created"] > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
        if expired:
            self._save()

    def _load(self):
        if self.path is None or not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            entries = payload.get("entries", [])
            for key, entry in entries:
                if (
                    isinstance(key, str)
                    and isinstance(entry, dict)
                    and isinstance(entry.get("fingerprint"), str)
                    and isinstance(entry.get("created"), (int, float))
                ):
                    self._entries[key] = entry
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self._entries.clear()

    def _save(self):
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        retained = []
        estimated_size = 32
        for key, entry in reversed(self._entries.items()):
            persisted = dict(entry)
            encoded_result = json.dumps(
                persisted.get("result"),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            if len(encoded_result) > self.max_result_bytes:
                persisted["result"] = {
                    "idempotency_replayed": True,
                    "result_unavailable_after_restart": True,
                    "original_result_bytes": len(encoded_result),
                }
            encoded_entry = json.dumps(
                [key, persisted],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            if retained and estimated_size + len(encoded_entry) > self.max_storage_bytes:
                break
            retained.append([key, persisted])
            estimated_size += len(encoded_entry)
        retained.reverse()
        payload = json.dumps(
            {"version": 1, "entries": retained},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
