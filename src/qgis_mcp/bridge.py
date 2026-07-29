from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .config import ConnectionInfo, load_connection_info
from .errors import BRIDGE_ERROR, BRIDGE_UNAVAILABLE, RpcError

EventHandler = Callable[[dict[str, Any]], Awaitable[None] | None]
MAX_BRIDGE_RESPONSE_BYTES = 32 * 1024 * 1024
RETRYABLE_METHODS = {
    "session.snapshot",
    "project.inspect",
    "layer.inspect",
    "feature.query",
    "capabilities.search",
    "capabilities.describe",
    "ui.search",
    "ui.screenshot",
    "logs.read",
    "handle.read",
    "artifact.read",
    "artifact.list",
    "resources.list",
    "resources.read",
}


class BridgeClient:
    """Persistent, multiplexed JSON-lines client for the in-QGIS bridge."""

    def __init__(
        self,
        info: ConnectionInfo | None = None,
        *,
        reconnect_timeout: float | None = None,
        reconnect_initial_delay: float = 0.1,
        reconnect_max_delay: float = 5.0,
    ) -> None:
        self._info = info
        self._fixed_info = info is not None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._sequence = 0
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._event_handlers: list[EventHandler] = []
        configured_timeout = os.environ.get("QGIS_MCP_RECONNECT_TIMEOUT_SECONDS", "60")
        self._reconnect_timeout = max(
            0.0,
            float(configured_timeout) if reconnect_timeout is None else float(reconnect_timeout),
        )
        self._reconnect_initial_delay = max(0.01, float(reconnect_initial_delay))
        self._reconnect_max_delay = max(
            self._reconnect_initial_delay, float(reconnect_max_delay)
        )
        self._session_identity: tuple[int | None, int, str] | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        async with self._connect_lock:
            if self.connected:
                return
            try:
                info = self._info if self._fixed_info else load_connection_info()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise RpcError(
                    BRIDGE_UNAVAILABLE,
                    "Waiting for the QGIS plugin bridge",
                    {"cause": str(exc)},
                ) from exc
            if info is None:
                raise RpcError(BRIDGE_UNAVAILABLE, "QGIS bridge connection is unavailable")
            self._info = info
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    info.host, info.port, limit=MAX_BRIDGE_RESPONSE_BYTES
                )
            except (OSError, asyncio.TimeoutError) as exc:
                raise RpcError(
                    BRIDGE_UNAVAILABLE,
                    "Cannot connect to the QGIS plugin bridge",
                    {"host": info.host, "port": info.port, "cause": str(exc)},
                ) from exc
            assert self._reader is not None and self._writer is not None
            self._reader_task = asyncio.create_task(
                self._read_loop(self._reader, self._writer)
            )
            try:
                hello = await self.request(
                    "bridge.hello",
                    {
                        "token": info.token,
                        "protocol": info.protocol,
                        "client": "qgis-agent-mcp",
                    },
                    timeout=5,
                    _connect=False,
                )
            except Exception:
                await self.close()
                raise
            if not isinstance(hello, dict) or not hello.get("authenticated"):
                await self.close()
                raise RpcError(BRIDGE_UNAVAILABLE, "QGIS bridge authentication failed")
            identity = (info.pid, info.port, info.token)
            previous = self._session_identity
            self._session_identity = identity
            if previous is not None and previous != identity:
                self._emit_event(
                    {
                        "type": "bridge.reconnected",
                        "previous_pid": previous[0],
                        "pid": info.pid,
                        "qgis_version": info.qgis_version,
                    }
                )

    async def close(self) -> None:
        writer, task = self._writer, self._reader_task
        self._writer = None
        self._reader = None
        self._reader_task = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        current = asyncio.current_task()
        if task is not None and task is not current:
            task.cancel()
        self._fail_pending(
            RpcError(BRIDGE_UNAVAILABLE, "Connection to the QGIS bridge closed")
        )

    def add_event_handler(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 120,
        _connect: bool = True,
        _retry: bool = True,
    ) -> Any:
        retryable = (
            method in RETRYABLE_METHODS
            or (method == "operation.control" and (params or {}).get("action", "status") == "status")
            or bool((params or {}).get("idempotency_key"))
        )
        try:
            return await self._request_once(method, params, timeout=timeout, connect=_connect)
        except RpcError as exc:
            if not _retry or not _connect or not retryable or exc.code != BRIDGE_UNAVAILABLE:
                raise
            return await self._request_after_restart(
                method,
                params,
                timeout=timeout,
                first_error=exc,
            )

    async def _request_after_restart(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float,
        first_error: RpcError,
    ) -> Any:
        if self._fixed_info or self._reconnect_timeout <= 0:
            raise first_error
        deadline = time.monotonic() + min(timeout, self._reconnect_timeout)
        delay = self._reconnect_initial_delay
        attempts = 0
        last_error = first_error
        while True:
            attempts += 1
            await self.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return await self._request_once(
                    method,
                    params,
                    timeout=max(0.1, min(timeout, remaining)),
                    connect=True,
                )
            except RpcError as exc:
                if exc.code != BRIDGE_UNAVAILABLE:
                    raise
                last_error = exc
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(delay, remaining))
            delay = min(self._reconnect_max_delay, delay * 2)
        raise RpcError(
            BRIDGE_UNAVAILABLE,
            "QGIS did not reconnect before the recovery deadline",
            {
                "method": method,
                "attempts": attempts,
                "timeout_seconds": min(timeout, self._reconnect_timeout),
                "last_error": last_error.message,
            },
        ) from last_error

    async def _request_once(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout: float,
        connect: bool,
    ) -> Any:
        if connect and not self.connected:
            await self.connect()
        if self._writer is None:
            raise RpcError(BRIDGE_UNAVAILABLE, "QGIS bridge is not connected")
        self._sequence += 1
        request_id = self._sequence
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        try:
            async with self._write_lock:
                self._writer.write(encoded)
                await self._writer.drain()
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise RpcError(
                BRIDGE_ERROR,
                f"QGIS bridge request timed out: {method}",
                {"method": method, "timeout_seconds": timeout},
            ) from exc

    async def _read_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("QGIS bridge closed the connection")
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(
                            RpcError(
                                int(error.get("code", BRIDGE_ERROR)),
                                str(error.get("message", "QGIS bridge error")),
                                error.get("data"),
                            )
                        )
                    else:
                        future.set_result(message.get("result"))
                elif message.get("method") == "bridge.event":
                    self._emit_event(message.get("params", {}))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._fail_pending(
                RpcError(BRIDGE_UNAVAILABLE, "QGIS bridge connection failed", str(exc))
            )
            writer.close()
            if self._writer is writer:
                self._writer = None
                self._reader = None
                self._reader_task = None

    def _fail_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)

    def _emit_event(self, event: dict[str, Any]) -> None:
        for handler in tuple(self._event_handlers):
            result = handler(event)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
