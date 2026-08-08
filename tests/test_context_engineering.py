from __future__ import annotations

import asyncio
import json

import pytest
from qgis_agent_mcp.context_pipeline import (
    capture_value,
    resolve_references,
    select_value,
    summarize,
)

from qgis_mcp.context_pack import build_context_pack
from qgis_mcp.server import MODERN_PROTOCOL, McpServer
from qgis_mcp.tasks import TaskManager
from qgis_mcp.tool_registry import ToolRegistry


class ContextBridge:
    def __init__(self):
        self.calls = []
        self.release = None

    def add_event_handler(self, _handler):
        return None

    async def request(self, method, params, **_kwargs):
        self.calls.append((method, params))
        if method == "session.snapshot":
            return {
                "revision": 42,
                "incremental": False,
                "project": {"name": "Terrain study"},
                "layers": [
                    {"id": "dem", "name": "Elevation DEM", "type": "raster"},
                    {"id": "roads", "name": "Road network", "type": "vector"},
                ],
                "resource_revisions": {
                    "qgis://project": 42,
                    "qgis://layers/dem": 41,
                    "qgis://layers/roads": 40,
                },
            }
        if method == "capabilities.search":
            return {
                "results": [
                    {"kind": "processing", "id": "gdal:slope", "name": "Slope"}
                ]
            }
        if method == "capabilities.describe":
            return {"id": params["id"], "parameters": {"INPUT": {"type": "raster"}}}
        if self.release is not None:
            await self.release.wait()
        return {"method": method, "params": params}


def modern_meta(*, tasks=False):
    extensions = {"io.modelcontextprotocol/tasks": {}} if tasks else {}
    return {
        "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        "io.modelcontextprotocol/clientCapabilities": {"extensions": extensions},
    }


def test_adaptive_registry_has_small_deterministic_initial_catalog():
    registry = ToolRegistry("adaptive")
    status = registry.status()
    assert status["visible_tools"] <= 8
    assert status["visible_catalog_bytes"] < 10_000
    assert [tool["name"] for tool in registry.visible_tools()] == [
        tool["name"] for tool in registry.visible_tools()
    ]

    result = registry.search("style roads labels", limit=5)
    assert 1 <= len(result["schema_tools"]) <= 2
    assert len(json.dumps(result, separators=(",", ":")).encode("utf-8")) < 5_000
    summaries = registry.search("style roads labels", limit=5, detail="summary")
    assert all("inputSchema" not in match for match in summaries["matches"])


def test_context_pack_enforces_hard_utf8_budget():
    snapshot = {
        "revision": 8,
        "project": {"name": "x" * 5000},
        "layers": [
            {"id": str(index), "name": "elevation " + "x" * 500}
            for index in range(100)
        ],
        "changes": ["x" * 1000 for _ in range(50)],
    }
    discovery = {
        "matches": [
            {
                "name": "qgis_processing_start",
                "description": "x" * 2000,
                "inputSchema": {"type": "object", "description": "x" * 5000},
            }
        ]
    }
    result = build_context_pack(
        "calculate terrain slope", snapshot, discovery, budget_bytes=2048
    )
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert len(encoded) <= 2048
    assert result["returned_bytes"] == len(encoded)
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_qgis_context_collapses_snapshot_search_and_describe_under_budget():
    bridge = ContextBridge()
    response = await McpServer(bridge).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "_meta": modern_meta(),
                "name": "qgis_context",
                "arguments": {
                    "task": "calculate slope from the elevation DEM",
                    "budget_bytes": 4096,
                },
            },
        }
    )
    result = response["result"]
    pack = result["structuredContent"]
    assert result["resultType"] == "complete"
    assert pack["returned_bytes"] <= 4096
    assert pack["preconditions"]["if_revision"] == 42
    assert pack["runtime_matches"][0]["id"] == "gdal:slope"
    assert {method for method, _params in bridge.calls} == {
        "session.snapshot",
        "capabilities.search",
        "capabilities.describe",
    }


@pytest.mark.asyncio
async def test_qgis_context_validation_is_returned_as_correctable_tool_error():
    response = await McpServer(ContextBridge()).dispatch(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "qgis_context",
                "arguments": {"task": "x", "budget_bytes": 1},
            },
        }
    )
    assert "error" not in response
    assert response["result"]["isError"] is True
    errors = response["result"]["structuredContent"]["error"]["data"][
        "validation_errors"
    ]
    assert "$.task is too short" in errors
    assert "$.budget_bytes is below the minimum" in errors


@pytest.mark.asyncio
async def test_modern_discovery_cache_metadata_and_version_errors():
    server = McpServer(ContextBridge())
    discovered = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "server/discover",
            "params": {"_meta": modern_meta()},
        }
    )
    result = discovered["result"]
    assert result["resultType"] == "complete"
    assert MODERN_PROTOCOL in result["supportedVersions"]
    assert "io.modelcontextprotocol/tasks" in result["capabilities"]["extensions"]
    assert result["ttlMs"] > 0

    rejected = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2099-01-01",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
    )
    assert rejected["error"]["code"] == -32022
    assert MODERN_PROTOCOL in rejected["error"]["data"]["supported"]


@pytest.mark.asyncio
async def test_negotiated_long_tool_returns_durable_mcp_task(tmp_path):
    bridge = ContextBridge()
    bridge.release = asyncio.Event()
    server = McpServer(bridge, task_manager=TaskManager(tmp_path))
    created = await server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "_meta": modern_meta(tasks=True),
                "name": "qgis_project_verify",
                "arguments": {},
            },
        }
    )
    task_id = created["result"]["taskId"]
    assert created["result"]["resultType"] == "task"
    assert created["result"]["status"] == "working"
    assert (tmp_path / (task_id + ".json")).is_file()

    bridge.release.set()
    for _ in range(20):
        await asyncio.sleep(0)
        polled = await server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tasks/get",
                "params": {"_meta": modern_meta(tasks=True), "taskId": task_id},
            }
        )
        if polled["result"]["status"] == "completed":
            break
    assert polled["result"]["status"] == "completed"
    assert polled["result"]["resultType"] == "complete"
    assert polled["result"]["result"]["structuredContent"]["method"] == "project.verify"


class MemoryHandles:
    def __init__(self):
        self.values = []

    def put(self, value, **_kwargs):
        self.values.append(value)
        return {"handle": "h_{}".format(len(self.values))}


def test_declarative_pipeline_references_projection_and_capture():
    variables = {"query": {"items": [{"name": "Paris"}], "count": 1}}
    params = resolve_references(
        {"layer": "cities", "name": {"$ref": "query#/items/0/name"}}, variables
    )
    assert params["name"] == "Paris"
    assert select_value(variables["query"], "/count") == 1
    assert summarize({"items": list(range(100))})["items"]["item_count"] == 100
    handles = MemoryHandles()
    captured, descriptor = capture_value(variables["query"], "handle", handles)
    assert captured is True
    assert descriptor == {"handle": "h_1"}
