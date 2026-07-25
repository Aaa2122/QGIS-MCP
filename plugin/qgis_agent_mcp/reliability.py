from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict


class IdempotencyConflict(ValueError):
    pass


class MutationGuard:
    """Bounded TTL cache for replay-safe mutation results."""

    def __init__(self, max_entries=512, ttl_seconds=3600):
        self.max_entries = int(max_entries)
        self.ttl_seconds = float(ttl_seconds)
        self._entries = OrderedDict()

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
            "created": time.monotonic(),
            "fingerprint": self.fingerprint(method, params),
            "result": result,
        }
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def prune(self):
        now = time.monotonic()
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry["created"] > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
