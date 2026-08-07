from __future__ import annotations

import asyncio
import base64
import io
import json

import pytest

from qgis_mcp.errors import RpcError
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
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    assert response["result"]["protocolVersion"] == "2025-11-25"
    assert response["result"]["capabilities"]["tools"] == {"listChanged": True}
    listed = await server.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert len(names) == len(set(names))
    assert set(names) == CORE_TOOL_NAMES | DISCOVERY_TOOL_NAMES
    assert "qgis_batch" in names
    assert len(names) < len(TOOLS) / 4


def test_full_catalog_mode_remains_available():
    registry = ToolRegistry("full")
    names = {tool["name"] for tool in registry.visible_tools()}
    assert names == set(TOOL_METHODS) | DISCOVERY_TOOL_NAMES


def test_catalog_excludes_arbitrary_python_execution():
    assert "qgis_python_exec" not in {tool["name"] for tool in TOOLS}
    assert "qgis_python_exec" not in TOOL_METHODS
    assert "python.exec" not in TOOL_METHODS.values()


def test_adaptive_catalog_has_bounded_default_context_cost():
    status = ToolRegistry("adaptive").status()
    assert status["catalog_tools"] == len(TOOLS)
    assert status["visible_catalog_bytes"] < 20_000
    assert status["visible_catalog_bytes"] < status["catalog_bytes"] * 0.25


def test_search_ranks_tool_intent_without_leaking_toolset_keywords():
    registry = ToolRegistry("adaptive")
    offline = registry.search("offline project package", limit=3)
    assert offline["matches"][0]["name"] == "qgis_offline"

    slope = registry.search("slope raster", limit=3)
    assert {match["name"] for match in slope["matches"][:2]} == {
        "qgis_capabilities_search",
        "qgis_processing_start",
    }
    assert slope["runtime_discovery_recommended"] is True


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
    assert '"layer.inspect"' not in response["result"]["content"][0]["text"]


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
    assert matches[0]["inputSchema"]["type"] == "object"
    assert matches[0]["examples"]
    assert matches[0]["call"]["arguments"] == {"layer": "<layer>"}
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
async def test_processing_search_federates_live_algorithms_and_keeps_partial_results():
    class ProcessingBridge(FakeBridge):
        async def request(self, method, params, **kwargs):
            self.calls.append((method, params))
            if method == "capabilities.search":
                return {
                    "results": [
                        {
                            "kind": "processing",
                            "id": "gdal:slope",
                            "name": "Slope",
                        }
                    ]
                }
            if method == "capabilities.describe":
                raise RpcError(-32002, "Description temporarily unavailable")
            return await super().request(method, params, **kwargs)

    response = await McpServer(ProcessingBridge()).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {
                "name": "qgis_tools",
                "arguments": {"action": "search", "query": "slope raster"},
            },
        }
    )
    result = response["result"]["structuredContent"]
    assert result["runtime_matches"][0]["id"] == "gdal:slope"
    assert result["runtime_matches"][0]["call"]["arguments"]["algorithm"] == "gdal:slope"
    assert result["runtime_schema_warning"]["code"] == -32002


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
async def test_resource_updates_only_notify_matching_subscriptions_and_are_deduplicated():
    bridge = FakeBridge()
    server = McpServer(bridge)
    notifications = []

    async def sink(message):
        notifications.append(message)

    server.set_notification_sink(sink)
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        }
    )
    await server.dispatch(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    )
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "resources/subscribe",
            "params": {"uri": "qgis://layers/a"},
        }
    )

    await bridge.handler(
        {"type": "resource.updated", "uri": "qgis://layers/b", "revision": 4}
    )
    event = {
        "type": "resource.updated",
        "uri": "qgis://layers/a/style",
        "revision": 5,
    }
    await bridge.handler(event)
    await bridge.handler(event)
    assert notifications == [
        {
            "jsonrpc": "2.0",
            "method": "notifications/resources/updated",
            "params": {"uri": "qgis://layers/a/style"},
        }
    ]

    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "resources/unsubscribe",
            "params": {"uri": "qgis://layers/a"},
        }
    )
    await bridge.handler({**event, "revision": 6})
    assert len(notifications) == 1


@pytest.mark.asyncio
async def test_resource_subscription_requires_a_qgis_uri():
    server = McpServer(FakeBridge())
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "resources/subscribe",
            "params": {"uri": "file:///tmp/project"},
        }
    )
    assert response["error"]["code"] == -32602


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
async def test_qgis_execution_error_is_returned_to_the_model_as_tool_error():
    class FailingBridge(FakeBridge):
        async def request(self, method, params, **kwargs):
            raise RpcError(-32010, "Layer is invalid", {"hint": "repair source"})

    response = await McpServer(FailingBridge()).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {"name": "qgis_session_snapshot", "arguments": {}},
        }
    )
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == -32010


@pytest.mark.asyncio
async def test_hidden_tool_arguments_are_server_validated():
    bridge = FakeBridge()
    response = await McpServer(bridge).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {
                "name": "qgis_tool_call",
                "arguments": {
                    "tool": "qgis_feature_query",
                    "arguments": {"unexpected": True},
                },
            },
        }
    )
    assert response["result"]["isError"] is True
    errors = response["result"]["structuredContent"]["error"]["data"][
        "validation_errors"
    ]
    assert "$.layer is required" in errors
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_cancellation_targets_the_mcp_request_id():
    class BlockingBridge(FakeBridge):
        async def request(self, method, params, **kwargs):
            await asyncio.Event().wait()

    server = McpServer(BlockingBridge())
    pending = asyncio.create_task(
        server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "request-to-cancel",
                "method": "tools/call",
                "params": {"name": "qgis_session_snapshot", "arguments": {}},
            }
        )
    )
    await asyncio.sleep(0)
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "request-to-cancel", "reason": "user cancelled"},
        }
    )
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert server._in_flight_requests == {}


@pytest.mark.asyncio
async def test_large_modern_structured_result_is_not_duplicated_as_text():

    class LargeBridge(FakeBridge):
        async def request(self, method, params, **kwargs):
            return {"items": ["x" * 5000]}

    response = await McpServer(LargeBridge()).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {"name": "qgis_project_inspect", "arguments": {}},
        }
    )
    result = response["result"]
    assert result["structuredContent"]["items"][0] == "x" * 5000
    assert "x" * 100 not in result["content"][0]["text"]
    assert len(result["content"][0]["text"]) < 200


@pytest.mark.asyncio
async def test_legacy_protocol_keeps_complete_json_text_results():
    server = McpServer(FakeBridge())
    await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 320,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
    )
    response = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 321,
            "method": "tools/call",
            "params": {"name": "qgis_project_inspect", "arguments": {}},
        }
    )
    assert json.loads(response["result"]["content"][0]["text"])["method"] == "project.inspect"


@pytest.mark.asyncio
async def test_tools_and_resources_support_pagination_and_templates():
    server = McpServer(FakeBridge(), tool_mode="full")
    first = await server.dispatch(
        {"jsonrpc": "2.0", "id": 33, "method": "tools/list", "params": {}}
    )
    assert len(first["result"]["tools"]) == 100
    second = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 34,
            "method": "tools/list",
            "params": {"cursor": first["result"]["nextCursor"]},
        }
    )
    assert second["result"]["tools"]
    templates = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 35,
            "method": "resources/templates/list",
            "params": {},
        }
    )
    uris = {
        item["uriTemplate"] for item in templates["result"]["resourceTemplates"]
    }
    assert "qgis://layers/{layer_id}/{view}" in uris


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


@pytest.mark.asyncio
async def test_stdio_transport_rejects_excess_in_flight_requests():
    release = asyncio.Event()

    class DelayedServer:
        def set_notification_sink(self, sink):
            self.sink = sink

        async def dispatch(self, request):
            await release.wait()
            return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    stdin = io.BytesIO(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}\n'
    )
    stdout = io.BytesIO()
    transport = StdioTransport(
        DelayedServer(), stdin, stdout, max_in_flight=2
    )
    asyncio.get_running_loop().call_later(0.1, release.set)
    await transport.run()
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    by_id = {response["id"]: response for response in responses}
    assert by_id[1]["result"] == {}
    assert by_id[2]["error"]["code"] == -32029
    assert by_id[3]["error"]["code"] == -32029
