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

PROJECT_TOOLS = {
    "qgis_project_manage": "project.manage",
    "qgis_project_properties": "project.properties",
    "qgis_project_repair": "project.repair",
    "qgis_source": "layer.source",
    "qgis_canvas": "canvas.control",
    "qgis_identify": "map.identify",
    "qgis_measure": "map.measure",
    "qgis_bookmarks": "bookmark.manage",
    "qgis_map_themes": "map_theme.manage",
    "qgis_crs": "crs.control",
    "qgis_expression": "expression.control",
    "qgis_metadata": "metadata.manage",
    "qgis_connections": "connection.inspect",
}

VECTOR_RASTER_TOOLS = {
    "qgis_vector_schema": "vector.schema",
    "qgis_vector_statistics": "vector.statistics",
    "qgis_geometry_edit": "vector.geometry",
    "qgis_vector_indexes": "vector.index",
    "qgis_vector_joins": "vector.join",
    "qgis_relations": "project.relation",
    "qgis_snapping": "project.snapping",
    "qgis_vector_select": "selection.advanced",
    "qgis_raster_inspect": "raster.inspect",
}

PROCESSING_DATABASE_TOOLS = {
    "qgis_processing_providers": "processing.provider",
    "qgis_processing_batch": "processing.batch",
    "qgis_processing_history": "processing.history",
    "qgis_processing_assets": "processing.assets",
    "qgis_processing_context": "processing.context",
    "qgis_database": "database.control",
    "qgis_connection_manage": "connection.manage",
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


def test_project_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {name: TOOL_METHODS[name] for name in PROJECT_TOOLS} == PROJECT_TOOLS
    for name in PROJECT_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


def test_vector_raster_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {
        name: TOOL_METHODS[name] for name in VECTOR_RASTER_TOOLS
    } == VECTOR_RASTER_TOOLS
    for name in VECTOR_RASTER_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


def test_processing_database_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {
        name: TOOL_METHODS[name] for name in PROCESSING_DATABASE_TOOLS
    } == PROCESSING_DATABASE_TOOLS
    for name in PROCESSING_DATABASE_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False
