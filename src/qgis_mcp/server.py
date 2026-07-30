from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from . import __version__
from .bridge import BridgeClient
from .errors import INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, RpcError
from .tool_registry import DISCOVERY_TOOL_NAMES, ToolRegistry

LOGGER = logging.getLogger(__name__)
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


class McpServer:
    """Small MCP stdio server with no dependency on a particular MCP SDK."""

    def __init__(self, bridge: BridgeClient | Any, *, tool_mode: str | None = None) -> None:
        self.bridge = bridge
        self.tools = ToolRegistry(tool_mode)
        self.initialized = False
        self.client_info: dict[str, Any] = {}
        self.protocol_version = SUPPORTED_PROTOCOLS[0]
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
        try:
            result = await self._call(method, params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            return self._error(request_id, exc)
        except Exception as exc:
            LOGGER.exception("Unhandled MCP method failure: %s", method)
            return self._error(
                request_id,
                RpcError(INTERNAL_ERROR, "Internal server error", {"cause": str(exc)}),
            )

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools.visible_tools()}
        if method == "tools/call":
            return await self._tool_call(params)
        if method == "resources/list":
            return await self._resources_list()
        if method == "resources/read":
            return await self._resource_read(params)
        if method in {"resources/subscribe", "resources/unsubscribe", "logging/setLevel"}:
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
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {"subscribe": True, "listChanged": True},
                "logging": {},
            },
            "serverInfo": {"name": "qgis-agent-mcp", "version": __version__},
            "instructions": (
                "Operate on the live QGIS session. Start with qgis_session_snapshot. Use "
                "qgis_tools to discover specialist tools and qgis_tool_call to invoke a "
                "hidden specialist without loading the full catalog. Prefer structured tools, "
                "discover capabilities before invoking them, and use QGIS Processing for "
                "provider algorithms. Large data "
                "stays in QGIS and is returned through summaries, pages, or handles."
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
            return self._structured_result(result)
        if name == "qgis_tool_call":
            nested_name = arguments.get("tool")
            nested_arguments = arguments.get("arguments") or {}
            if not isinstance(nested_name, str) or not self.tools.has_tool(nested_name):
                raise RpcError(INVALID_PARAMS, f"Unknown specialist tool: {nested_name}")
            if not isinstance(nested_arguments, dict):
                raise RpcError(INVALID_PARAMS, "arguments must be an object")
            return await self._execute_tool(nested_name, nested_arguments)
        return await self._execute_tool(str(name), arguments)

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        timeout = 120.0
        if name == "qgis_batch":
            timeout = 305.0
        result = await self.bridge.request(
            self.tools.method_for(name), arguments, timeout=timeout
        )
        if name in {"qgis_screenshot", "qgis_visual_review"} and isinstance(
            result, dict
        ) and result.get("data"):
            try:
                base64.b64decode(result["data"], validate=True)
            except Exception as exc:
                raise RpcError(INTERNAL_ERROR, "QGIS returned an invalid screenshot") from exc
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

    @staticmethod
    def _structured_result(result: Any) -> dict[str, Any]:
        serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        return {
            "content": [{"type": "text", "text": serialized}],
            "structuredContent": result if isinstance(result, dict) else {"result": result},
        }

    async def _resources_list(self) -> dict[str, Any]:
        try:
            dynamic = await self.bridge.request("resources.list", {})
        except RpcError:
            dynamic = []
        resources = [
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
        if isinstance(dynamic, list):
            resources.extend(dynamic)
        by_uri = {resource["uri"]: resource for resource in resources}
        return {"resources": list(by_uri.values())}

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
                    "text": json.dumps(result, ensure_ascii=False, indent=2, default=str),
                }
            ]
        }

    async def _cancel_notification(self, params: dict[str, Any]) -> None:
        operation_id = params.get("operationId")
        if operation_id:
            try:
                await self.bridge.request(
                    "operation.control",
                    {"operation_id": str(operation_id), "action": "cancel"},
                    timeout=5,
                )
            except Exception:
                LOGGER.debug("Could not cancel bridge operation", exc_info=True)

    async def _on_bridge_event(self, event: dict[str, Any]) -> None:
        if self._notification_sink is None:
            return
        event_type = event.get("type")
        if event_type == "resource.updated":
            notification = {
                "jsonrpc": "2.0",
                "method": "notifications/resources/updated",
                "params": {"uri": event.get("uri", "qgis://session")},
            }
        elif event_type == "resources.changed":
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
    def __init__(self, server: McpServer, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self.server = server
        self.stdin = stdin
        self.stdout = stdout
        self._write_lock = asyncio.Lock()
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
            task = asyncio.create_task(self._handle(request))
            pending.add(task)
            task.add_done_callback(pending.discard)
        if pending:
            await asyncio.gather(*pending)

    async def _handle(self, request: dict[str, Any]) -> None:
        response = await self.server.dispatch(request)
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
