from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest

from qgis_mcp.server import McpServer, StdioTransport
from qgis_mcp.tool_catalog import TOOL_METHODS, TOOLS
from qgis_mcp.tool_registry import CORE_TOOL_NAMES, DISCOVERY_TOOL_NAMES, ToolRegistry


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.handler = None

    def add_event_handler(self, handler):
        self.handler = handler

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "resources.list":
            return [{"uri": "qgis://layers/a", "name": "A"}]
        if method == "ui.screenshot":
            return {
                "data": base64.b64encode(b"png").decode(),
                "mime_type": "image/png",
                "width": 1,
                "height": 1,
            }
        if method == "visual.review":
            return {
                "data": base64.b64encode(b"review").decode(),
                "mime_type": "image/png",
                "width": 2,
                "height": 1,
                "automated_review": {"passed": True, "findings": []},
            }
        return {"method": method, "params": params}


@pytest.mark.asyncio
async def test_initialize_and_catalog():
    server = McpServer(FakeBridge())
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"]["tools"] == {"listChanged": True}
    listed = await server.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert len(names) == len(set(names))
    assert set(names) == CORE_TOOL_NAMES | DISCOVERY_TOOL_NAMES
    assert len(names) < len(TOOLS) / 4


def test_full_catalog_mode_remains_available():
    registry = ToolRegistry("full")
    names = {tool["name"] for tool in registry.visible_tools()}
    assert names == set(TOOL_METHODS) | DISCOVERY_TOOL_NAMES


def test_adaptive_catalog_has_bounded_default_context_cost():
    status = ToolRegistry("adaptive").status()
    assert status["catalog_tools"] == len(TOOLS)
    assert status["visible_catalog_bytes"] < 20_000
    assert status["visible_catalog_bytes"] < status["catalog_bytes"] * 0.25


@pytest.mark.asyncio
async def test_tool_call_forwards_to_bridge():
    bridge = FakeBridge()
    server = McpServer(bridge)
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "qgis_layer_inspect",
                "arguments": {"layer": "roads"},
            },
        }
    )
    assert bridge.calls == [("layer.inspect", {"layer": "roads"})]
    assert response["result"]["structuredContent"]["method"] == "layer.inspect"


@pytest.mark.asyncio
async def test_specialist_tool_can_be_searched_and_called_without_being_visible():
    bridge = FakeBridge()
    server = McpServer(bridge)
    listed = await server.dispatch(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}
    )
    assert "qgis_point_cloud" not in {
        tool["name"] for tool in listed["result"]["tools"]
    }
    search = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "qgis_tools",
                "arguments": {"action": "search", "query": "nuage de points"},
            },
        }
    )
    matches = search["result"]["structuredContent"]["matches"]
    assert matches[0]["name"] == "qgis_point_cloud"
    called = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "qgis_tool_call",
                "arguments": {
                    "tool": "qgis_point_cloud",
                    "arguments": {"action": "inspect", "layer": "lidar"},
                },
            },
        }
    )
    assert bridge.calls[-1] == (
        "point_cloud.control",
        {"action": "inspect", "layer": "lidar"},
    )
    assert called["result"]["structuredContent"]["method"] == "point_cloud.control"


@pytest.mark.asyncio
async def test_activating_toolset_emits_catalog_change_notification():
    server = McpServer(FakeBridge())
    notifications = []

    async def sink(message):
        notifications.append(message)

    server.set_notification_sink(sink)
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "qgis_tools",
                "arguments": {"action": "activate", "toolsets": ["cartography"]},
            },
        }
    )
    assert "cartography" in response["result"]["structuredContent"]["active_toolsets"]
    assert notifications == [
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    ]
    listed = await server.dispatch(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}}
    )
    assert "qgis_layout_items" in {
        tool["name"] for tool in listed["result"]["tools"]
    }


@pytest.mark.asyncio
async def test_screenshot_returns_image_content():
    server = McpServer(FakeBridge())
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "qgis_screenshot", "arguments": {}},
        }
    )
    assert response["result"]["content"][0]["type"] == "image"
    assert "data" not in response["result"]["structuredContent"]


@pytest.mark.asyncio
async def test_visual_review_returns_image_and_structural_checks():
    server = McpServer(FakeBridge())
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "qgis_visual_review", "arguments": {"action": "capture"}},
        }
    )
    assert response["result"]["content"][0]["type"] == "image"
    assert response["result"]["structuredContent"]["automated_review"]["passed"]
    assert "data" not in response["result"]["structuredContent"]


@pytest.mark.asyncio
async def test_resources_include_dynamic_layers():
    server = McpServer(FakeBridge())
    response = await server.dispatch(
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list", "params": {}}
    )
    uris = {item["uri"] for item in response["result"]["resources"]}
    assert {"qgis://session", "qgis://layers/a"} <= uris


@pytest.mark.asyncio
async def test_unknown_tool_is_structured_error():
    server = McpServer(FakeBridge())
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "not_a_tool"},
        }
    )
    assert response["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_stdio_transport_handles_requests_concurrently():
    class DelayedServer:
        def set_notification_sink(self, sink):
            self.sink = sink

        async def dispatch(self, request):
            if request["id"] == 1:
                await asyncio.sleep(0.03)
            return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    stdout = io.BytesIO()
    transport = StdioTransport(DelayedServer(), stdin, stdout)
    await transport.run()
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [2, 1]
