from __future__ import annotations

from qgis_mcp.tool_catalog import TOOL_METHODS, TOOLS

RUNTIME_TOOLS = {
    "qgis_runtime": "runtime.control",
    "qgis_tasks": "runtime.tasks",
    "qgis_events": "runtime.events",
    "qgis_render": "runtime.render",
    "qgis_transaction": "runtime.transaction",
    "qgis_undo": "runtime.undo",
    "qgis_preflight": "runtime.preflight",
    "qgis_state_diff": "runtime.diff",
    "qgis_diagnostics": "runtime.diagnostics",
    "qgis_permissions": "runtime.permissions",
    "qgis_auth": "runtime.auth",
}


def test_runtime_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {name: TOOL_METHODS[name] for name in RUNTIME_TOOLS} == RUNTIME_TOOLS
    for name in RUNTIME_TOOLS:
        assert by_name[name]["inputSchema"]["type"] == "object"
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


def test_runtime_mutations_have_reliability_controls():
    by_name = {tool["name"]: tool for tool in TOOLS}
    for name in {"qgis_tasks", "qgis_render", "qgis_transaction", "qgis_undo"}:
        properties = by_name[name]["inputSchema"]["properties"]
        assert {"idempotency_key", "if_revision", "if_resource_revisions", "dry_run"} <= set(properties)
