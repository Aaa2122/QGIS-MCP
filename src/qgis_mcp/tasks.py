from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import INTERNAL_ERROR, INVALID_PARAMS, RpcError

TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}


class TaskManager:
    """Small durable implementation of the MCP Tasks extension."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root or Path.home() / ".qgis-mcp" / "tasks").expanduser()
        self._records: dict[str, dict[str, Any]] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._load()

    def start(
        self,
        run: Callable[[], Awaitable[dict[str, Any]]],
        *,
        status_message: str,
        ttl_ms: int = 3_600_000,
        poll_interval_ms: int = 500,
    ) -> dict[str, Any]:
        self._purge_expired()
        task_id = "qt_" + uuid.uuid4().hex
        now = _timestamp()
        record: dict[str, Any] = {
            "taskId": task_id,
            "status": "working",
            "statusMessage": str(status_message),
            "createdAt": now,
            "lastUpdatedAt": now,
            "ttlMs": max(60_000, min(int(ttl_ms), 86_400_000)),
            "pollIntervalMs": max(100, min(int(poll_interval_ms), 10_000)),
        }
        self._records[task_id] = record
        self._save(record)
        self._running[task_id] = asyncio.create_task(self._run(task_id, run))
        return {"resultType": "task", **self._public(record)}

    def get(self, task_id: str) -> dict[str, Any]:
        self._purge_expired()
        record = self._records.get(str(task_id))
        if record is None:
            raise RpcError(INVALID_PARAMS, "Unknown or expired task", {"taskId": task_id})
        return self._public(record, detailed=True)

    def cancel(self, task_id: str) -> None:
        record = self._records.get(str(task_id))
        if record is None:
            raise RpcError(INVALID_PARAMS, "Unknown or expired task", {"taskId": task_id})
        if record.get("status") in TERMINAL_TASK_STATES:
            return
        running = self._running.get(str(task_id))
        if running is not None:
            running.cancel("MCP task cancellation requested")
        self._set_status(record, "cancelled", "Cancellation requested")

    def update(self, task_id: str, input_responses: Any) -> None:
        record = self._records.get(str(task_id))
        if record is None:
            raise RpcError(INVALID_PARAMS, "Unknown or expired task", {"taskId": task_id})
        if not isinstance(input_responses, dict):
            raise RpcError(INVALID_PARAMS, "inputResponses must be an object")
        # QGIS tasks currently do not pause for MRTR input. Accepting an empty
        # update keeps the extension interoperable without inventing requests.
        if input_responses and record.get("status") != "input_required":
            raise RpcError(INVALID_PARAMS, "Task is not waiting for input")

    async def _run(
        self,
        task_id: str,
        run: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        record = self._records[task_id]
        try:
            result = await run()
            if record.get("status") != "cancelled":
                record["result"] = result
                self._set_status(record, "completed", "QGIS task completed")
        except asyncio.CancelledError:
            self._set_status(record, "cancelled", "QGIS task cancelled")
        except RpcError as exc:
            record["error"] = exc.as_dict()
            self._set_status(record, "failed", exc.message)
        except Exception as exc:
            record["error"] = {
                "code": INTERNAL_ERROR,
                "message": "Task execution failed",
                "data": {"exception": type(exc).__name__, "cause": str(exc)},
            }
            self._set_status(record, "failed", "Task execution failed")
        finally:
            self._running.pop(task_id, None)

    def _set_status(self, record: dict[str, Any], status: str, message: str) -> None:
        record["status"] = status
        record["statusMessage"] = str(message)
        record["lastUpdatedAt"] = _timestamp()
        self._save(record)

    def _load(self) -> None:
        if not self.root.is_dir():
            return
        for path in self.root.glob("qt_*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or not record.get("taskId"):
                continue
            if record.get("status") not in TERMINAL_TASK_STATES:
                record["error"] = {
                    "code": INTERNAL_ERROR,
                    "message": "Server restarted before task completion",
                }
                record["status"] = "failed"
                record["statusMessage"] = "Interrupted by server restart"
                record["lastUpdatedAt"] = _timestamp()
                self._save(record)
            self._records[str(record["taskId"])] = record
        self._purge_expired()

    def _purge_expired(self) -> None:
        now_ms = time.time() * 1000
        for task_id, record in list(self._records.items()):
            created = _parse_timestamp(record.get("createdAt"))
            ttl_ms = record.get("ttlMs")
            if created is None or ttl_ms is None or now_ms <= created * 1000 + int(ttl_ms):
                continue
            self._records.pop(task_id, None)
            try:
                self._path(task_id).unlink()
            except OSError:
                pass

    def _save(self, record: dict[str, Any]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(str(record["taskId"]))
            descriptor, temporary = tempfile.mkstemp(
                prefix=path.name + ".", dir=str(self.root)
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            # A task remains usable in-process if persistence is unavailable.
            return

    def _path(self, task_id: str) -> Path:
        if not task_id.startswith("qt_") or not task_id[3:].isalnum():
            raise RpcError(INVALID_PARAMS, "Invalid task ID")
        return self.root / (task_id + ".json")

    @staticmethod
    def _public(record: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
        fields = {
            "taskId",
            "status",
            "statusMessage",
            "createdAt",
            "lastUpdatedAt",
            "ttlMs",
            "pollIntervalMs",
        }
        if detailed:
            fields.update({"result", "error", "inputRequests"})
        return {key: value for key, value in record.items() if key in fields}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
