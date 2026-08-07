from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from . import __version__
from .bridge import BridgeClient
from .errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SERVER_BUSY,
    RpcError,
)
from .tool_registry import DISCOVERY_TOOL_NAMES, ToolRegistry

LOGGER = logging.getLogger(__name__)
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
TOOL_PAGE_SIZE = 100
RESOURCE_PAGE_SIZE = 100


class McpServer:
    """Small MCP stdio server with no dependency on a particular MCP SDK."""

    def __init__(self, bridge: BridgeClient | Any, *, tool_mode: str | None = None) -> None:
        self.bridge = bridge
        self.tools = ToolRegistry(tool_mode)
        self.initialized = False
        self.client_info: dict[str, Any] = {}
        self.protocol_version = SUPPORTED_PROTOCOLS[0]
        self._subscriptions: set[str] = set()
        self._resource_notification_revisions: dict[str, Any] = {}
        self._last_list_change_revision: Any = None
        self._in_flight_requests: dict[Any, asyncio.Task[Any]] = {}
        self._notification_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        if hasattr(bridge, "add_event_handler"):
            bridge.add_event_handler(self._on_bridge_event)

    def set_notification_sink(
        self, sink: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        self._notification_sink = sink

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            if request_id is None:
                return None
            return self._error(request_id, RpcError(-32600, "Invalid JSON-RPC request"))
        method = request["method"]
        params = request.get("params") or {}
        if request_id is None:
            if method == "notifications/initialized":
                self.initialized = True
            elif method == "notifications/cancelled":
                await self._cancel_notification(params)
            return None
        current_task = asyncio.current_task()
        if current_task is not None:
            self._in_flight_requests[request_id] = current_task
        try:
            result = await self._call(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except asyncio.CancelledError:
            raise
        except RpcError as exc:
            return self._error(request_id, exc)
        except Exception as exc:
            LOGGER.exception("Unhandled MCP method failure: %s", method)
            return self._error(
                request_id,
                RpcError(INTERNAL_ERROR, "Internal server error", {"cause": str(exc)}),
            )
        finally:
            if self._in_flight_requests.get(request_id) is current_task:
                self._in_flight_requests.pop(request_id, None)

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return _paginate(
                self.tools.visible_tools(),
                params.get("cursor"),
                prefix="qgis-tools",
                page_size=TOOL_PAGE_SIZE,
                result_key="tools",
            )
        if method == "tools/call":
            return await self._tool_call(params)
        if method == "resources/list":
            return await self._resources_list(params)
        if method == "resources/templates/list":
            return self._resource_templates_list()
        if method == "resources/read":
            return await self._resource_read(params)
        if method == "resources/subscribe":
            return self._subscribe(params)
        if method == "resources/unsubscribe":
            return self._unsubscribe(params)
        if method == "logging/setLevel":
            return {}
        if method == "prompts/list":
            return {"prompts": []}
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion", SUPPORTED_PROTOCOLS[0]))
        self.protocol_version = (
            requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        )
        self.client_info = params.get("clientInfo") or {}
        self._subscriptions.clear()
        self._resource_notification_revisions.clear()
        self._last_list_change_revision = None
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True, "listChanged": True},
                "logging": {},
            },
            "serverInfo": {"name": "qgis-agent-mcp", "version": __version__},
            "instructions": (
                "Operate on the live QGIS session. Start with qgis_session_snapshot at summary "
                "detail, then pass its revision as since_revision for compact deltas. Use "
                "qgis_tools to discover specialist tools; search results include schemas and "
                "examples, plus live Processing matches when relevant. Invoke hidden tools with "
                "qgis_tool_call. Keep large data in QGIS and request summaries, pages, handles, "
                "or resource links instead of raw datasets."
            ),
        }

    async def _tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if name not in DISCOVERY_TOOL_NAMES and not self.tools.has_tool(str(name)):
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "Tool arguments must be an object")
        if name == "qgis_tools":
            before = tuple(tool["name"] for tool in self.tools.visible_tools())
            try:
                result = self.tools.command(arguments)
            except (TypeError, ValueError) as exc:
                raise RpcError(INVALID_PARAMS, str(exc)) from exc
            after = tuple(tool["name"] for tool in self.tools.visible_tools())
            if before != after and self._notification_sink is not None:
                await self._notification_sink(
                    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
                )
            if str(arguments.get("action", "search")) == "search":
                await self._add_runtime_discovery(result, arguments)
            return self._structured_result(result)
        if name == "qgis_tool_call":
            nested_name = arguments.get("tool")
            nested_arguments = arguments.get("arguments") or {}
            if not isinstance(nested_name, str) or not self.tools.has_tool(nested_name):
                raise RpcError(INVALID_PARAMS, f"Unknown specialist tool: {nested_name}")
            if not isinstance(nested_arguments, dict):
                raise RpcError(INVALID_PARAMS, "arguments must be an object")
            validation = self.tools.validate_arguments(nested_name, nested_arguments)
            if validation:
                return self._tool_error_result(
                    nested_name,
                    RpcError(
                        INVALID_PARAMS,
                        "Specialist tool arguments failed schema validation",
                        {"validation_errors": validation},
                    ),
                )
            return await self._execute_tool(nested_name, nested_arguments)
        validation = self.tools.validate_arguments(str(name), arguments)
        if validation:
            return self._tool_error_result(
                str(name),
                RpcError(
                    INVALID_PARAMS,
                    "Tool arguments failed schema validation",
                    {"validation_errors": validation},
                ),
            )
        return await self._execute_tool(str(name), arguments)

    async def _add_runtime_discovery(
        self, result: dict[str, Any], arguments: dict[str, Any]
    ) -> None:
        query = str(arguments.get("query", "")).strip()
        mode = str(arguments.get("runtime_mode", "auto"))
        should_search = mode == "include" or (
            mode == "auto" and bool(result.get("runtime_discovery_recommended"))
        )
        if not query or mode == "skip" or not should_search:
            return
        try:
            live = await self.bridge.request(
                "capabilities.search",
                {
                    "query": query,
                    "kinds": ["processing"],
                    "limit": max(1, min(int(arguments.get("limit", 5)), 10)),
                },
                timeout=10,
            )
            runtime_matches = list((live or {}).get("results") or []) if isinstance(live, dict) else []
            for match in runtime_matches:
                if match.get("kind") == "processing" and match.get("id"):
                    match["call"] = {
                        "tool": "qgis_processing_start",
                        "arguments": {"algorithm": match["id"], "parameters": {}},
                    }
            result["runtime_matches"] = runtime_matches
            if runtime_matches and bool(arguments.get("include_schema", True)):
                first = runtime_matches[0]
                if first.get("kind") == "processing" and first.get("id"):
                    try:
                        first["schema"] = await self.bridge.request(
                            "capabilities.describe",
                            {"kind": "processing", "id": first["id"]},
                            timeout=10,
                        )
                    except RpcError as exc:
                        result["runtime_schema_warning"] = {
                            "code": exc.code,
                            "message": exc.message,
                        }
            if runtime_matches:
                result["runtime_usage"] = (
                    "Use qgis_processing_start with the returned algorithm ID and schema."
                )
        except RpcError as exc:
            result["runtime_discovery_warning"] = {
                "code": exc.code,
                "message": exc.message,
            }

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        timeout = 120.0
        if name == "qgis_batch":
            timeout = 305.0
        try:
            result = await self.bridge.request(
                self.tools.method_for(name), arguments, timeout=timeout
            )
        except RpcError as exc:
            return self._tool_error_result(name, exc)
        if name in {"qgis_screenshot", "qgis_visual_review"} and isinstance(
            result, dict
        ) and result.get("data"):
            try:
                base64.b64decode(result["data"], validate=True)
            except Exception as exc:
                return self._tool_error_result(
                    name,
                    RpcError(
                        INTERNAL_ERROR,
                        "QGIS returned an invalid screenshot",
                        {"cause": str(exc)},
                    ),
                )
            return {
                "content": [
                    {
                        "type": "image",
                        "data": result["data"],
                        "mimeType": result.get("mime_type", "image/png"),
                    },
                    {
                        "type": "text",
                        "text": json.dumps(
                            {key: value for key, value in result.items() if key != "data"},
                            ensure_ascii=False,
                        ),
                    },
                ],
                "structuredContent": {
                    key: value for key, value in result.items() if key != "data"
                },
            }
        return self._structured_result(result)

    def _structured_result(self, result: Any) -> dict[str, Any]:
        legacy = self.protocol_version not in {"2025-11-25", "2025-06-18"}
        if legacy:
            # Older protocol revisions do not expose structuredContent, so their
            # text block remains the complete backwards-compatible result.
            text = json.dumps(
                result, ensure_ascii=False, separators=(",", ":"), default=str
            )
        else:
            structured = result if isinstance(result, dict) else {"result": result}
            keys = ", ".join(sorted(str(key) for key in structured)[:20]) or "none"
            text = (
                "Structured result available in structuredContent "
                "(top-level keys: {}).".format(keys)
            )
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": result if isinstance(result, dict) else {"result": result},
        }

    def _tool_error_result(self, name: str, error: RpcError) -> dict[str, Any]:
        structured = {
            "tool": name,
            "error": error.as_dict(),
            "retryable": error.code in {SERVER_BUSY, -32001, -32002},
        }
        result = self._structured_result(structured)
        result["isError"] = True
        return result

    async def _resources_list(self, params: dict[str, Any]) -> dict[str, Any]:
        cursor = params.get("cursor")
        try:
            dynamic = await self.bridge.request(
                "resources.list",
                {"cursor": cursor, "limit": RESOURCE_PAGE_SIZE},
            )
        except RpcError:
            dynamic = []
        canonical = [
            {
                "uri": "qgis://session",
                "name": "Current QGIS session",
                "description": "Revisioned summary of the live QGIS session",
                "mimeType": "application/json",
            },
            {
                "uri": "qgis://project",
                "name": "Current QGIS project",
                "description": "Project metadata and layer tree",
                "mimeType": "application/json",
            },
            {
                "uri": "qgis://capabilities",
                "name": "QGIS capability index",
                "description": "Summary counts and providers for discoverable capabilities",
                "mimeType": "application/json",
            },
            {
                "uri": "qgis://logs",
                "name": "Recent QGIS MCP events",
                "mimeType": "application/json",
            },
        ]
        descriptions = {resource["uri"]: resource for resource in canonical}
        if isinstance(dynamic, dict):
            resources = list(dynamic.get("resources") or [])
            next_cursor = dynamic.get("next_cursor") or dynamic.get("nextCursor")
            for resource in resources:
                resource.update(
                    {
                        key: value
                        for key, value in descriptions.get(resource.get("uri"), {}).items()
                        if key not in resource
                    }
                )
            result = {"resources": resources}
            if next_cursor:
                result["nextCursor"] = next_cursor
            return result
        resources = canonical + (dynamic if isinstance(dynamic, list) else [])
        by_uri = {resource["uri"]: resource for resource in resources}
        return _paginate(
            list(by_uri.values()),
            cursor,
            prefix="qgis-resources",
            page_size=RESOURCE_PAGE_SIZE,
            result_key="resources",
        )

    @staticmethod
    def _resource_templates_list() -> dict[str, Any]:
        return {
            "resourceTemplates": [
                {
                    "uriTemplate": "qgis://layers/{layer_id}",
                    "name": "QGIS layer",
                    "title": "QGIS layer summary",
                    "description": "Read one live layer by stable QGIS layer ID.",
                    "mimeType": "application/json",
                },
                {
                    "uriTemplate": "qgis://layers/{layer_id}/{view}",
                    "name": "QGIS layer view",
                    "title": "QGIS layer schema or selection",
                    "description": "Read a layer view where view is schema or selection.",
                    "mimeType": "application/json",
                },
                {
                    "uriTemplate": "qgis://operations/{operation_id}",
                    "name": "QGIS operation",
                    "title": "Managed QGIS operation status",
                    "description": "Read progress and results for a managed QGIS operation.",
                    "mimeType": "application/json",
                },
            ]
        }

    async def _resource_read(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith("qgis://"):
            raise RpcError(INVALID_PARAMS, "A qgis:// resource URI is required")
        result = await self.bridge.request("resources.read", {"uri": uri})
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(
                        result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                }
            ]
        }

    def _subscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith("qgis://"):
            raise RpcError(INVALID_PARAMS, "A qgis:// resource URI is required")
        self._subscriptions.add(uri.rstrip("/"))
        self._resource_notification_revisions.pop(uri.rstrip("/"), None)
        return {}

    def _unsubscribe(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri.startswith("qgis://"):
            raise RpcError(INVALID_PARAMS, "A qgis:// resource URI is required")
        normalized = uri.rstrip("/")
        self._subscriptions.discard(normalized)
        for notified_uri in list(self._resource_notification_revisions):
            if notified_uri == normalized or notified_uri.startswith(normalized + "/"):
                self._resource_notification_revisions.pop(notified_uri, None)
        return {}

    def _is_subscribed(self, uri: str) -> bool:
        normalized = uri.rstrip("/")
        return any(
            normalized == subscribed or normalized.startswith(subscribed + "/")
            for subscribed in self._subscriptions
        )

    async def _cancel_notification(self, params: dict[str, Any]) -> None:
        request_id = params.get("requestId")
        if request_id is None:
            return
        task = self._in_flight_requests.get(request_id)
        if task is None or task.done():
            return
        reason = str(params.get("reason", "MCP peer cancelled the request"))
        LOGGER.info("Cancelling MCP request %r: %s", request_id, reason)
        task.cancel(reason)

    async def _on_bridge_event(self, event: dict[str, Any]) -> None:
        if self._notification_sink is None or not self.initialized:
            return
        event_type = event.get("type")
        if event_type == "resource.updated":
            uri = str(event.get("uri", "qgis://session")).rstrip("/")
            if not self._is_subscribed(uri):
                return
            revision = event.get("revision")
            if (
                revision is not None
                and self._resource_notification_revisions.get(uri) == revision
            ):
                return
            if revision is not None:
                self._resource_notification_revisions[uri] = revision
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": uri},
            }
        elif event_type == "resources.changed":
            revision = event.get("revision")
            if revision is not None and revision == self._last_list_change_revision:
                return
            self._last_list_change_revision = revision
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/resources/list_changed",
            }
        else:
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {
                    "level": event.get("level", "info"),
                    "logger": "qgis",
                    "data": event,
                },
            }
        await self._notification_sink(notification)

    @staticmethod
    def _error(request_id: Any, error: RpcError) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": error.as_dict()}


class StdioTransport:
    def __init__(
        self,
        server: McpServer,
        stdin: BinaryIO,
        stdout: BinaryIO,
        *,
        max_in_flight: int | None = None,
    ) -> None:
        self.server = server
        self.stdin = stdin
        self.stdout = stdout
        self._write_lock = asyncio.Lock()
        configured = os.environ.get("QGIS_MCP_MAX_IN_FLIGHT", "64")
        self.max_in_flight = max(
            2, int(configured) if max_in_flight is None else int(max_in_flight)
        )
        self.control_reserve = max(1, min(8, self.max_in_flight // 4))
        server.set_notification_sink(self.write)

    async def run(self) -> None:
        pending: set[asyncio.Task[None]] = set()
        while True:
            line = await asyncio.to_thread(self.stdin.readline)
            if not line:
                break
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("request is not an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                await self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error", "data": str(exc)},
                    }
                )
                continue
            control = self._is_control_request(request)
            capacity = self.max_in_flight if control else self.max_in_flight - self.control_reserve
            if len(pending) >= capacity:
                request_id = request.get("id")
                if request_id is not None:
                    await self.write(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": RpcError(
                                SERVER_BUSY,
                                "MCP server is busy",
                                {
                                    "in_flight": len(pending),
                                    "max_in_flight": self.max_in_flight,
                                },
                            ).as_dict(),
                        }
                    )
                continue
            task = asyncio.create_task(self._handle(request))
            pending.add(task)
            task.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending)

    @staticmethod
    def _is_control_request(request: dict[str, Any]) -> bool:
        return request.get("method") in {
            "initialize",
            "ping",
            "notifications/cancelled",
            "notifications/initialized",
        }

    async def _handle(self, request: dict[str, Any]) -> None:
        try:
            response = await self.server.dispatch(request)
        except asyncio.CancelledError:
            return
        if response is not None:
            await self.write(response)

    async def write(self, message: dict[str, Any]) -> None:
        payload = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        async with self._write_lock:
            self.stdout.write(payload)
            self.stdout.flush()


async def run_stdio() -> None:
    bridge = BridgeClient()
    server = McpServer(bridge)
    transport = StdioTransport(server, sys.stdin.buffer, sys.stdout.buffer)
    try:
        await transport.run()
    finally:
        await bridge.close()


def _paginate(
    items: list[dict[str, Any]],
    cursor: Any,
    *,
    prefix: str,
    page_size: int,
    result_key: str,
) -> dict[str, Any]:
    offset = 0
    if cursor is not None:
        match = re.fullmatch(r"{}:([0-9]+)".format(re.escape(prefix)), str(cursor))
        if match is None:
            raise RpcError(INVALID_PARAMS, "Invalid or stale pagination cursor")
        offset = int(match.group(1))
        if offset > len(items):
            raise RpcError(INVALID_PARAMS, "Pagination cursor is outside the result set")
    page = items[offset : offset + page_size]
    result: dict[str, Any] = {result_key: page}
    if offset + len(page) < len(items):
        result["nextCursor"] = "{}:{}".format(prefix, offset + len(page))
    return result
