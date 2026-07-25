from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest

from qgis_mcp.server import McpServer, StdioTransport
from qgis_mcp.tool_catalog import TOOL_METHODS, TOOLS


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
    assert response["result"]["capabilities"]["tools"] == {"listChanged": False}
    listed = await server.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert len(names) == len(set(names)) == len(TOOLS)
    assert set(names) == set(TOOL_METHODS)


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
