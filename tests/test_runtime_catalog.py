from __future__ import annotations

from qgis_mcp.tool_catalog import _MUTATION_TOOLS, TOOL_METHODS, TOOLS

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
    "qgis_visual_review": "visual.review",
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

CARTOGRAPHY_TOOLS = {
    "qgis_renderer": "cartography.renderer",
    "qgis_symbol": "cartography.symbol",
    "qgis_style_library": "style.library",
    "qgis_labeling": "cartography.labeling",
    "qgis_layout_items": "layout.item",
    "qgis_atlas": "layout.atlas",
    "qgis_layout_validate": "layout.validate",
}

SPECIALIZED_DATA_TOOLS = {
    "qgis_layer_properties": "layer.properties",
    "qgis_layer_capabilities": "layer.capabilities",
    "qgis_raster_style": "raster.style",
    "qgis_mesh": "mesh.control",
    "qgis_point_cloud": "point_cloud.control",
    "qgis_vector_tiles": "vector_tile.control",
    "qgis_tiled_scene": "tiled_scene.control",
    "qgis_temporal": "layer.temporal",
    "qgis_elevation": "layer.elevation",
}

ECOSYSTEM_TOOLS = {
    "qgis_plugin_advisor": "plugins.advise",
    "qgis_plugins": "ecosystem.plugins",
    "qgis_settings": "ecosystem.settings",
    "qgis_shortcuts": "ecosystem.shortcuts",
    "qgis_gps": "ecosystem.gps",
    "qgis_3d_views": "ecosystem.3d",
    "qgis_server": "ecosystem.server",
    "qgis_offline": "ecosystem.offline",
}

AUTHORING_TOOLS = {
    "qgis_forms": "authoring.forms",
    "qgis_diagrams": "authoring.diagrams",
    "qgis_annotations": "authoring.annotations",
    "qgis_geometry_quality": "authoring.geometry_quality",
    "qgis_vector_export": "authoring.vector_export",
}

QA_TOOLS = {
    "qgis_compatibility": "qa.compatibility",
    "qgis_project_audit": "qa.project_audit",
    "qgis_benchmark": "qa.benchmark",
    "qgis_self_test": "qa.self_test",
}


def test_entire_catalog_has_unique_complete_routes_and_reliability_controls():
    names = [tool["name"] for tool in TOOLS]
    assert len(names) == len(set(names))
    assert set(names) == set(TOOL_METHODS)
    for tool in TOOLS:
        assert tool["description"].strip()
        assert tool["title"].strip()
        assert tool["outputSchema"]["type"] == "object"
        assert {
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        } <= set(tool["annotations"])
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert all(
            property_schema.get("description")
            for property_schema in schema["properties"].values()
        )
        if tool["name"] in _MUTATION_TOOLS:
            assert {
                "idempotency_key",
                "if_revision",
                "if_resource_revisions",
                "dry_run",
            } <= set(schema["properties"])


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


def test_stability_guards_are_exposed_in_tool_schemas():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert by_name["qgis_batch"]["inputSchema"]["properties"]["calls"]["maxItems"] == 25
    assert by_name["qgis_workflow"]["inputSchema"]["properties"]["steps"]["maxItems"] == 25
    assert (
        by_name["qgis_workflow"]["inputSchema"]["properties"]["resume_on_restart"][
            "default"
        ]
        is False
    )
    assert by_name["qgis_data_fetch"]["inputSchema"]["properties"][
        "timeout_seconds"
    ]["default"] == 30
    assert by_name["qgis_processing_start"]["inputSchema"]["properties"][
        "allow_main_thread"
    ]["default"] is False
    assert by_name["qgis_database"]["inputSchema"]["properties"][
        "allow_blocking"
    ]["default"] is False
    feature_query = by_name["qgis_feature_query"]["inputSchema"]["properties"]
    assert {"cursor", "order_by", "bbox", "include_total_count", "max_bytes"} <= set(
        feature_query
    )
    assert feature_query["include_total_count"]["default"] is False
    assert feature_query["max_bytes"]["default"] == 65536
    assert feature_query["max_bytes"]["maximum"] == 1048576


def test_point_cloud_raster_and_3d_repairs_are_exposed_in_schemas():
    by_name = {tool["name"]: tool for tool in TOOLS}
    project_actions = by_name["qgis_project_action"]["inputSchema"]["properties"][
        "action"
    ]["enum"]
    assert "add_point_cloud" in project_actions
    style = by_name["qgis_style_apply"]["inputSchema"]["properties"]
    assert {"single_band_gray", "multiband_color", "pseudocolor"} <= set(
        style["mode"]["enum"]
    )
    assert "oneOf" in style["color_ramp"]
    raster_ramp = by_name["qgis_raster_style"]["inputSchema"]["properties"][
        "color_ramp"
    ]
    assert "oneOf" in raster_ramp
    assert by_name["qgis_3d_views"]["inputSchema"]["properties"]["scene_mode"][
        "default"
    ] == "local"


def test_cartography_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {name: TOOL_METHODS[name] for name in CARTOGRAPHY_TOOLS} == CARTOGRAPHY_TOOLS
    for name in CARTOGRAPHY_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


def test_specialized_data_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {
        name: TOOL_METHODS[name] for name in SPECIALIZED_DATA_TOOLS
    } == SPECIALIZED_DATA_TOOLS
    for name in SPECIALIZED_DATA_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


def test_ecosystem_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {name: TOOL_METHODS[name] for name in ECOSYSTEM_TOOLS} == ECOSYSTEM_TOOLS
    for name in ECOSYSTEM_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


def test_authoring_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {name: TOOL_METHODS[name] for name in AUTHORING_TOOLS} == AUTHORING_TOOLS
    for name in AUTHORING_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


def test_qa_tool_families_are_public_and_routed():
    by_name = {tool["name"]: tool for tool in TOOLS}
    assert {name: TOOL_METHODS[name] for name in QA_TOOLS} == QA_TOOLS
    for name in QA_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False
