from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import logging
import os
import re
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from . import __version__
from .bridge import BridgeClient
from .context_pack import build_context_pack
from .errors import (
    BRIDGE_ERROR,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    SERVER_BUSY,
    RpcError,
)
from .plugin_advisor import PluginAdvisor
from .tasks import TaskManager
from .tool_registry import DISCOVERY_TOOL_NAMES, ToolRegistry

LOGGER = logging.getLogger(__name__)
MODERN_PROTOCOL = "2026-07-28"
SUPPORTED_PROTOCOLS = (
    MODERN_PROTOCOL,
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
LEGACY_PROTOCOLS = SUPPORTED_PROTOCOLS[1:]
UNSUPPORTED_PROTOCOL_VERSION = -32022
TOOL_PAGE_SIZE = 100
RESOURCE_PAGE_SIZE = 100
TASK_ELIGIBLE_TOOLS = {
    "qgis_batch",
    "qgis_project_verify",
    "qgis_visual_review",
    "qgis_workflow",
}


class McpServer:
    """Small MCP stdio server with no dependency on a particular MCP SDK."""

    def __init__(
        self,
        bridge: BridgeClient | Any,
        *,
        tool_mode: str | None = None,
        plugin_advisor: PluginAdvisor | Any | None = None,
        task_manager: TaskManager | Any | None = None,
    ) -> None:
        self.bridge = bridge
        self.tools = ToolRegistry(tool_mode)
        self.plugin_advisor = plugin_advisor or PluginAdvisor()
        self.task_manager = task_manager or TaskManager(
            os.environ.get("QGIS_MCP_TASK_DIR")
        )
        self.initialized = False
        self.client_info: dict[str, Any] = {}
        # No-metadata requests are treated as legacy until a modern request
        # explicitly declares its per-request protocol version.
        self.protocol_version = LEGACY_PROTOCOLS[0]
        self._request_protocol: contextvars.ContextVar[str] = contextvars.ContextVar(
            "qgis_mcp_request_protocol", default="2025-11-25"
        )
        self._request_capabilities: contextvars.ContextVar[dict[str, Any]] = (
            contextvars.ContextVar("qgis_mcp_request_capabilities", default={})
        )
        self._subscriptions: set[str] = set()
        self._resource_notification_revisions: dict[str, Any] = {}
        self._last_list_change_revision: Any = None
        self._recent_tool_errors: deque[dict[str, Any]] = deque(maxlen=20)
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
        protocol_token = None
        capability_token = None
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
            protocol, capabilities = self._request_context(method, params)
            protocol_token = self._request_protocol.set(protocol)
            capability_token = self._request_capabilities.set(capabilities)
            result = await self._call(method, params)
            if protocol == MODERN_PROTOCOL:
                result = self._modern_result(result)
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
            if capability_token is not None:
                self._request_capabilities.reset(capability_token)
            if protocol_token is not None:
                self._request_protocol.reset(protocol_token)
            if self._in_flight_requests.get(request_id) is current_task:
                self._in_flight_requests.pop(request_id, None)

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "server/discover":
            return self._discover()
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            result = _paginate(
                self.tools.visible_tools(
                    include_active=self._request_protocol.get() != MODERN_PROTOCOL
                ),
                params.get("cursor"),
                prefix="qgis-tools",
                page_size=TOOL_PAGE_SIZE,
                result_key="tools",
            )
            return self._cacheable(result, ttl_ms=300_000)
        if method == "tools/call":
            return await self._tool_call(params)
        if method == "tasks/get":
            return self.task_manager.get(str(params.get("taskId", "")))
        if method == "tasks/cancel":
            self.task_manager.cancel(str(params.get("taskId", "")))
            return {}
        if method == "tasks/update":
            self.task_manager.update(
                str(params.get("taskId", "")), params.get("inputResponses") or {}
            )
            return {}
        if method == "resources/list":
            return await self._resources_list(params)
        if method == "resources/templates/list":
            return self._cacheable(self._resource_templates_list(), ttl_ms=3_600_000)
        if method == "resources/read":
            return await self._resource_read(params)
        if method == "resources/subscribe":
            return self._subscribe(params)
        if method == "resources/unsubscribe":
            return self._unsubscribe(params)
        if method == "logging/setLevel":
            return {}
        if method == "prompts/list":
            return self._cacheable({"prompts": []}, ttl_ms=3_600_000)
        raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = str(params.get("protocolVersion", LEGACY_PROTOCOLS[0]))
        self.protocol_version = (
            requested if requested in LEGACY_PROTOCOLS else LEGACY_PROTOCOLS[0]
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
                self._instructions()
            ),
        }

    def _discover(self) -> dict[str, Any]:
        return {
            "supportedVersions": list(SUPPORTED_PROTOCOLS),
            "capabilities": self._modern_capabilities(),
            "instructions": self._instructions(),
            "ttlMs": 3_600_000,
            "cacheScope": "public",
        }

    @staticmethod
    def _instructions() -> str:
        return (
            "Operate on the live QGIS session. Start complex jobs with qgis_context: it returns "
            "a task-conditioned snapshot, the most relevant schemas, live Processing matches, "
            "and revision guards under a strict byte budget. Invoke hidden specialist tools with "
            "qgis_tool_call. Use qgis_tools detail=summary or names for broader exploration and "
            "request full schemas only when needed. When a capability is missing, discover "
            "qgis_plugin_advisor and never install without explicit user confirmation. Keep large "
            "data and intermediate workflow values in QGIS through projections and handles."
        )

    @staticmethod
    def _modern_capabilities() -> dict[str, Any]:
        return {
            "tools": {},
            "resources": {},
            "prompts": {},
            "extensions": {"io.modelcontextprotocol/tasks": {}},
        }

    async def _tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if name not in DISCOVERY_TOOL_NAMES and not self.tools.has_tool(str(name)):
            raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RpcError(INVALID_PARAMS, "Tool arguments must be an object")
        discovery_validation = (
            self.tools.validate_arguments(str(name), arguments)
            if name in DISCOVERY_TOOL_NAMES
            else []
        )
        if discovery_validation:
            return self._tool_error_result(
                str(name),
                RpcError(
                    INVALID_PARAMS,
                    "Discovery tool arguments failed schema validation",
                    {"validation_errors": discovery_validation},
                ),
            )
        if name == "qgis_context":
            return await self._context_call(arguments)
        if name == "qgis_tools":
            action = str(arguments.get("action", "search"))
            if self._request_protocol.get() == MODERN_PROTOCOL and action in {
                "activate",
                "reset",
            }:
                return self._tool_error_result(
                    "qgis_tools",
                    RpcError(
                        INVALID_PARAMS,
                        "Stateless MCP keeps tools/list stable; call specialists through qgis_tool_call",
                    ),
                )
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
            if action == "search":
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
            if self._should_create_task(nested_name, nested_arguments):
                return self.task_manager.start(
                    lambda: self._execute_task_tool(nested_name, nested_arguments),
                    status_message="Executing {} in QGIS".format(nested_name),
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
        if self._should_create_task(str(name), arguments):
            return self.task_manager.start(
                lambda: self._execute_task_tool(str(name), arguments),
                status_message="Executing {} in QGIS".format(name),
            )
        return await self._execute_tool(str(name), arguments)

    async def _context_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task = str(arguments.get("task", "")).strip()
        if len(task) < 3:
            raise RpcError(INVALID_PARAMS, "qgis_context requires a concrete task")
        budget = max(2048, min(int(arguments.get("budget_bytes", 8192)), 32768))
        limit = max(1, min(int(arguments.get("tool_limit", 5)), 10))
        discovery = self.tools.search(task, limit=limit, detail="adaptive")
        snapshot_params: dict[str, Any] = {
            "detail": str(arguments.get("detail", "summary"))
        }
        if arguments.get("since_revision") is not None:
            snapshot_params["since_revision"] = int(arguments["since_revision"])

        runtime_arguments = {
            "query": task,
            "limit": limit,
            "runtime_mode": str(arguments.get("runtime_mode", "auto")),
            "detail": "adaptive",
        }
        snapshot_request = asyncio.create_task(
            self.bridge.request("session.snapshot", snapshot_params, timeout=15)
        )
        runtime_request = asyncio.create_task(
            self._add_runtime_discovery(discovery, runtime_arguments)
        )
        snapshot: Any
        try:
            snapshot = await snapshot_request
        except RpcError as exc:
            snapshot = {"available": False, "error": exc.as_dict()}
        await runtime_request
        selected_names = {
            str(match.get("name"))
            for match in discovery.get("matches", [])
            if isinstance(match, dict)
        }
        discovery["hints"] = [
            hint
            for hint in reversed(self._recent_tool_errors)
            if hint.get("tool") in selected_names
        ][:3]
        pack = build_context_pack(task, snapshot, discovery, budget_bytes=budget)
        return self._structured_result(pack)

    def _should_create_task(self, name: str, arguments: dict[str, Any]) -> bool:
        if self._request_protocol.get() != MODERN_PROTOCOL:
            return False
        extensions = self._request_capabilities.get().get("extensions") or {}
        if not isinstance(extensions, dict) or "io.modelcontextprotocol/tasks" not in extensions:
            return False
        if name not in TASK_ELIGIBLE_TOOLS:
            return False
        if name == "qgis_workflow":
            return str(arguments.get("action", "")) in {"run", "resume"}
        return True

    async def _execute_task_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._execute_tool(name, arguments)
        return self._modern_result(result)

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
            if name == "qgis_plugin_advisor":
                result = await self._plugin_advisor_call(arguments)
                return self._structured_result(result)
            if name == "qgis_plugins" and arguments.get("action", "list") == "install":
                result = await self._install_plugin_proposal(arguments, timeout=timeout)
                return self._structured_result(result)
            result = await self.bridge.request(
                self.tools.method_for(name), arguments, timeout=timeout
            )
        except RpcError as exc:
            self._remember_tool_error(name, arguments, exc)
            return self._tool_error_result(name, exc)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            code = INVALID_PARAMS if isinstance(exc, (KeyError, TypeError, ValueError)) else BRIDGE_ERROR
            error = RpcError(
                code, str(exc).strip("'"), {"exception": type(exc).__name__}
            )
            self._remember_tool_error(name, arguments, error)
            return self._tool_error_result(
                name,
                error,
            )
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

    def _remember_tool_error(
        self, name: str, arguments: dict[str, Any], error: RpcError
    ) -> None:
        self._recent_tool_errors.append(
            {
                "tool": name,
                "error_code": error.code,
                "message": error.message[:240],
                "parameter_names": sorted(str(key) for key in arguments)[:40],
                "guidance": "Adjust the named parameters or choose the next ranked capability.",
            }
        )

    async def _plugin_advisor_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", "recommend"))
        if action == "status":
            return self.plugin_advisor.status()
        inventory_query = str(
            arguments.get("task")
            or arguments.get("query")
            or arguments.get("plugin")
            or ""
        )
        inventory = await self.bridge.request(
            "ecosystem.plugins",
            {
                "action": "list",
                "query": inventory_query,
                "limit": 50,
                "compact": True,
            },
            timeout=30,
        )
        if not isinstance(inventory, dict):
            raise OSError("QGIS returned an invalid plugin inventory")
        installed = list(inventory.get("plugins") or [])
        qgis_version = str(inventory.get("qgis_version") or "").strip()
        if not qgis_version:
            raise OSError("QGIS did not report its version for plugin compatibility")
        include_experimental = bool(arguments.get("include_experimental", False))
        refresh = bool(arguments.get("refresh", False))
        limit = max(1, min(int(arguments.get("limit", 3)), 20))
        if action == "recommend":
            task = str(arguments.get("task", ""))
            native = self.tools.search(task, limit=8, include_schema=False)["matches"]
            native_matches = [
                {
                    "kind": "mcp_tool",
                    "source": "qgis_mcp",
                    "name": match["name"],
                    "description": match["description"],
                    "relevance": match["relevance"],
                    "matched_terms": match["matched_terms"],
                    "call": match["call"],
                }
                for match in native
                if match["name"] not in {"qgis_plugin_advisor", "qgis_plugins"}
            ]
            capability_warning = None
            try:
                live = await self.bridge.request(
                    "capabilities.search",
                    {
                        "query": task,
                        "kinds": ["processing", "action"],
                        "limit": 8,
                    },
                    timeout=10,
                )
                live_results = list(live.get("results") or []) if isinstance(live, dict) else []
                for match in live_results:
                    kind = str(match.get("kind", ""))
                    capability_id = str(match.get("id", ""))
                    if kind not in {"processing", "action"} or not capability_id:
                        continue
                    item = {
                        "kind": kind,
                        "source": "live_qgis",
                        "id": capability_id,
                        "name": match.get("name") or match.get("text") or capability_id,
                        "description": match.get("group") or match.get("tool_tip") or "",
                        "relevance": int(match.get("relevance", 0)),
                        "matched_terms": list(match.get("matched_terms") or []),
                    }
                    if kind == "processing":
                        item["call"] = {
                            "tool": "qgis_processing_start",
                            "arguments": {"algorithm": capability_id, "parameters": {}},
                        }
                    else:
                        item["call"] = {
                            "tool": "qgis_ui_invoke",
                            "arguments": {"target": capability_id, "action": "trigger"},
                        }
                    native_matches.append(item)
            except RpcError as exc:
                capability_warning = {"code": exc.code, "message": exc.message}
            native_matches.sort(
                key=lambda item: (-int(item.get("relevance", 0)), str(item.get("name", "")))
            )
            result = await asyncio.to_thread(
                self.plugin_advisor.recommend,
                task,
                qgis_version,
                installed,
                native_matches[:3],
                limit=limit,
                include_experimental=include_experimental,
                refresh=refresh,
            )
            if capability_warning:
                result["capability_warning"] = capability_warning
            return result
        if action == "search":
            return await asyncio.to_thread(
                self.plugin_advisor.search,
                str(arguments.get("query", "")),
                qgis_version,
                installed,
                limit=limit,
                include_experimental=include_experimental,
                refresh=refresh,
            )
        if action == "describe":
            return await asyncio.to_thread(
                self.plugin_advisor.describe,
                str(arguments.get("plugin", "")),
                qgis_version,
                installed,
                include_experimental=include_experimental,
                refresh=refresh,
            )
        raise ValueError("Unknown plugin advisor action: {}".format(action))

    async def _install_plugin_proposal(
        self, arguments: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        package = str(arguments.get("plugin", ""))
        proposal_id = str(arguments.get("proposal_id", ""))
        dry_run = bool(arguments.get("dry_run", False))
        idempotency_key = str(arguments.get("idempotency_key", "")).strip() or None
        proposal = self.plugin_advisor.validate_proposal(
            proposal_id,
            package,
            confirm_installation=dry_run
            or bool(arguments.get("confirm_installation", False)),
            confirm_untrusted=dry_run or bool(arguments.get("confirm_untrusted", False)),
            idempotency_key=idempotency_key,
        )
        bridge_arguments = {
            key: value
            for key, value in arguments.items()
            if key not in {"proposal_id", "confirm_installation", "confirm_untrusted"}
        }
        bridge_arguments.update(
            {
                "confirmed": True,
                "expected_version": proposal["version"],
                "experimental": proposal["experimental"],
                "allow_untrusted": not proposal["trusted"],
            }
        )
        result = await self.bridge.request(
            "ecosystem.plugins", bridge_arguments, timeout=max(timeout, 305.0)
        )
        if not dry_run:
            self.plugin_advisor.complete_proposal(proposal_id, idempotency_key)
        return result

    def _request_context(
        self, method: str, params: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if method == "initialize":
            requested = str(params.get("protocolVersion", LEGACY_PROTOCOLS[0]))
            return (
                requested if requested in LEGACY_PROTOCOLS else LEGACY_PROTOCOLS[0]
            ), {}
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict):
            return self.protocol_version, {}
        requested = meta.get("io.modelcontextprotocol/protocolVersion")
        if requested is None:
            return self.protocol_version, {}
        requested = str(requested)
        if requested not in SUPPORTED_PROTOCOLS:
            raise RpcError(
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": list(SUPPORTED_PROTOCOLS), "requested": requested},
            )
        capabilities = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
        if not isinstance(capabilities, dict):
            raise RpcError(INVALID_PARAMS, "Client capabilities must be an object")
        return requested, capabilities

    @staticmethod
    def _modern_result(result: Any) -> dict[str, Any]:
        modern = dict(result) if isinstance(result, dict) else {"value": result}
        modern.setdefault("resultType", "complete")
        meta = modern.setdefault("_meta", {})
        if isinstance(meta, dict):
            meta.setdefault(
                "io.modelcontextprotocol/serverInfo",
                {"name": "qgis-agent-mcp", "version": __version__},
            )
        return modern

    def _cacheable(self, result: dict[str, Any], *, ttl_ms: int) -> dict[str, Any]:
        if self._request_protocol.get() != MODERN_PROTOCOL:
            return result
        return {**result, "ttlMs": max(0, int(ttl_ms)), "cacheScope": "private"}

    def _structured_result(self, result: Any) -> dict[str, Any]:
        protocol = self._request_protocol.get()
        legacy = protocol not in {MODERN_PROTOCOL, "2025-11-25", "2025-06-18"}
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
            return self._cacheable(result, ttl_ms=5_000)
        resources = canonical + (dynamic if isinstance(dynamic, list) else [])
        by_uri = {resource["uri"]: resource for resource in resources}
        return self._cacheable(
            _paginate(
                list(by_uri.values()),
                cursor,
                prefix="qgis-resources",
                page_size=RESOURCE_PAGE_SIZE,
                result_key="resources",
            ),
            ttl_ms=5_000,
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
        return self._cacheable(
            {
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
            },
            ttl_ms=2_000,
        )

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
            "server/discover",
            "initialize",
            "ping",
            "tasks/get",
            "tasks/cancel",
            "tasks/update",
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
