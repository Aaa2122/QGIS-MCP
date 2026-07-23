from __future__ import annotations

from typing import Any


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


TOOLS: list[dict[str, Any]] = [
    {
        "name": "qgis_session_snapshot",
        "description": "Get a compact, revisioned snapshot of the active QGIS project, canvas, layers, selections, editing state, plugins, UI, tasks, and recent changes.",
        "inputSchema": _object(
            {
                "detail": {
                    "type": "string",
                    "enum": ["summary", "standard", "full"],
                    "default": "standard",
                },
                "since_revision": {"type": "integer", "minimum": 0},
            }
        ),
    },
    {
        "name": "qgis_project_inspect",
        "description": "Inspect the active project or layer tree without exporting spatial datasets.",
        "inputSchema": _object(
            {
                "section": {
                    "type": "string",
                    "enum": ["project", "layer_tree", "variables", "relations", "layouts"],
                    "default": "project",
                }
            }
        ),
    },
    {
        "name": "qgis_project_action",
        "description": "Perform a common project or canvas mutation: save, add/remove a layer, set the active layer, zoom to a layer, refresh, or load a named layer style.",
        "inputSchema": _object(
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "save",
                        "add_vector",
                        "add_raster",
                        "remove_layer",
                        "set_active_layer",
                        "zoom_layer",
                        "refresh",
                        "load_style",
                    ],
                },
                "layer": {"type": "string"},
                "source": {"type": "string"},
                "name": {"type": "string"},
                "provider": {"type": "string"},
                "path": {"type": "string"},
            },
            ["action"],
        ),
    },
    {
        "name": "qgis_layer_inspect",
        "description": "Inspect one layer's metadata, schema, renderer summary, selection, extent, and optional bounded feature sample.",
        "inputSchema": _object(
            {
                "layer": {"type": "string", "description": "Layer ID or exact layer name."},
                "include": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["metadata", "schema", "style", "selection", "statistics", "sample"],
                    },
                },
                "sample_limit": {"type": "integer", "minimum": 0, "maximum": 100, "default": 5},
            },
            ["layer"],
        ),
    },
    {
        "name": "qgis_feature_query",
        "description": "Query a bounded page of vector features in QGIS using an expression, selected-only mode, requested fields, and optional geometry summaries.",
        "inputSchema": _object(
            {
                "layer": {"type": "string"},
                "expression": {"type": "string"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "selected_only": {"type": "boolean", "default": False},
                "include_geometry": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            ["layer"],
        ),
    },
    {
        "name": "qgis_selection_set",
        "description": "Replace, add to, remove from, or clear a vector layer selection using feature IDs or a QGIS expression.",
        "inputSchema": _object(
            {
                "layer": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["replace", "add", "remove", "clear"],
                    "default": "replace",
                },
                "feature_ids": {"type": "array", "items": {"type": "integer"}},
                "expression": {"type": "string"},
            },
            ["layer"],
        ),
    },
    {
        "name": "qgis_vector_edit",
        "description": "Control a vector edit session or add, update, and delete features in the active edit buffer. Geometry is accepted as WKT and changes remain undoable until committed.",
        "inputSchema": _object(
            {
                "layer": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["start", "add", "update", "delete", "commit", "rollback"],
                },
                "features": {
                    "type": "array",
                    "maxItems": 1000,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "attributes": {"type": "object"},
                            "geometry_wkt": {"type": "string"},
                        },
                    },
                },
                "feature_ids": {"type": "array", "items": {"type": "integer"}},
                "auto_start": {"type": "boolean", "default": True},
            },
            ["layer", "action"],
        ),
    },
    {
        "name": "qgis_capabilities_search",
        "description": "Search runtime-discovered Processing algorithms, enabled plugins, QGIS/Qt actions, widgets, and selected PyQGIS API surfaces.",
        "inputSchema": _object(
            {
                "query": {"type": "string", "default": ""},
                "kinds": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["processing", "plugin", "action", "widget", "api"],
                    },
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
            }
        ),
    },
    {
        "name": "qgis_capability_describe",
        "description": "Describe a discovered capability, including parameters and outputs where available.",
        "inputSchema": _object(
            {"kind": {"type": "string"}, "id": {"type": "string"}},
            ["kind", "id"],
        ),
    },
    {
        "name": "qgis_capability_invoke",
        "description": "Invoke a public method on a discovered enabled plugin or selected live PyQGIS API root using JSON-compatible positional and keyword arguments.",
        "inputSchema": _object(
            {
                "kind": {"type": "string", "enum": ["plugin", "api"]},
                "target": {
                    "type": "string",
                    "description": "Plugin ID, or API root: project, iface, canvas, active_layer.",
                },
                "member": {"type": "string"},
                "args": {"type": "array", "default": []},
                "kwargs": {"type": "object", "default": {}},
            },
            ["kind", "target", "member"],
        ),
    },
    {
        "name": "qgis_processing_start",
        "description": "Start any installed QGIS Processing algorithm as a managed background operation. Returns an operation ID for status, result, progress, and cancellation.",
        "inputSchema": _object(
            {
                "algorithm": {"type": "string"},
                "parameters": {"type": "object"},
            },
            ["algorithm", "parameters"],
        ),
    },
    {
        "name": "qgis_operation",
        "description": "Inspect or cancel a long-running QGIS operation.",
        "inputSchema": _object(
            {
                "operation_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["status", "cancel"],
                    "default": "status",
                },
            },
            ["operation_id"],
        ),
    },
    {
        "name": "qgis_python_exec",
        "description": "Execute Python/PyQGIS in the live QGIS interpreter. This explicit escape hatch is disabled unless enabled in plugin settings or QGIS_MCP_ENABLE_PYTHON=1.",
        "inputSchema": _object(
            {
                "code": {"type": "string"},
                "mode": {"type": "string", "enum": ["eval", "exec"], "default": "exec"},
                "result_expression": {"type": "string"},
                "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 300000, "default": 30000},
            },
            ["code"],
        ),
    },
    {
        "name": "qgis_ui_search",
        "description": "Search open Qt windows, docks, actions, menus, toolbars, and widgets by semantic object names and visible text.",
        "inputSchema": _object(
            {
                "query": {"type": "string", "default": ""},
                "types": {"type": "array", "items": {"type": "string"}},
                "visible_only": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            }
        ),
    },
    {
        "name": "qgis_ui_invoke",
        "description": "Semantically invoke a discovered QAction or interact with a widget by stable runtime ID.",
        "inputSchema": _object(
            {
                "target": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["trigger", "click", "set_text", "set_value", "set_checked", "show", "close"],
                },
                "value": {},
            },
            ["target", "action"],
        ),
    },
    {
        "name": "qgis_screenshot",
        "description": "Capture the QGIS main window, map canvas, or a discovered Qt widget for visual verification.",
        "inputSchema": _object(
            {
                "target": {"type": "string", "default": "canvas"},
                "max_width": {"type": "integer", "minimum": 64, "maximum": 4096, "default": 1600},
            }
        ),
    },
    {
        "name": "qgis_logs",
        "description": "Read recent bridge, QGIS message-log, operation, and error events after a sequence number.",
        "inputSchema": _object(
            {
                "after": {"type": "integer", "minimum": 0, "default": 0},
                "level": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            }
        ),
    },
    {
        "name": "qgis_handle_read",
        "description": "Read a page from a temporary server-side handle returned for a result too large to inline.",
        "inputSchema": _object(
            {
                "handle": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            },
            ["handle"],
        ),
    },
    {
        "name": "qgis_batch",
        "description": "Execute several bridge calls in order with one transport round trip. Stops on error unless continue_on_error is true.",
        "inputSchema": _object(
            {
                "calls": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "required": ["method"],
                        "properties": {
                            "method": {"type": "string"},
                            "params": {"type": "object"},
                        },
                    },
                },
                "continue_on_error": {"type": "boolean", "default": False},
            },
            ["calls"],
        ),
    },
]

TOOL_METHODS = {
    "qgis_session_snapshot": "session.snapshot",
    "qgis_project_inspect": "project.inspect",
    "qgis_project_action": "project.action",
    "qgis_layer_inspect": "layer.inspect",
    "qgis_feature_query": "feature.query",
    "qgis_selection_set": "selection.set",
    "qgis_vector_edit": "vector.edit",
    "qgis_capabilities_search": "capabilities.search",
    "qgis_capability_describe": "capabilities.describe",
    "qgis_capability_invoke": "capabilities.invoke",
    "qgis_processing_start": "processing.start",
    "qgis_operation": "operation.control",
    "qgis_python_exec": "python.exec",
    "qgis_ui_search": "ui.search",
    "qgis_ui_invoke": "ui.invoke",
    "qgis_screenshot": "ui.screenshot",
    "qgis_logs": "logs.read",
    "qgis_handle_read": "handle.read",
    "qgis_batch": "batch.execute",
}
