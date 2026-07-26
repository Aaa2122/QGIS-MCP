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
                "retain_outputs": {"type": "boolean", "default": True},
                "add_to_project": {"type": "boolean", "default": False},
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
                "as_artifact": {"type": "boolean", "default": False},
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
                "atomic": {
                    "type": "boolean",
                    "default": False,
                    "description": "Checkpoint the project and restore it if any call fails.",
                },
            },
            ["calls"],
        ),
    },
    {
        "name": "qgis_artifact_read",
        "description": "Read a bounded base64 chunk from a binary artifact retained inside QGIS.",
        "inputSchema": _object(
            {
                "artifact_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "length": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1048576,
                    "default": 1048576,
                },
            },
            ["artifact_id"],
        ),
    },
    {
        "name": "qgis_artifacts_list",
        "description": "List live binary artifacts and their sizes, hashes, MIME types, and TTL.",
        "inputSchema": _object({}),
    },
    {
        "name": "qgis_data_fetch",
        "description": "Securely download a bounded spatial dataset through QGIS networking, reuse its local cache, record provenance, and optionally add supported vector or raster data to the project.",
        "inputSchema": _object(
            {
                "url": {"type": "string", "format": "uri"},
                "name": {"type": "string"},
                "authcfg": {
                    "type": "string",
                    "description": "QGIS Authentication configuration ID; never pass credentials in the URL.",
                },
                "cache_mode": {
                    "type": "string",
                    "enum": ["reuse", "refresh"],
                    "default": "reuse",
                },
                "max_age_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2592000,
                    "default": 3600,
                },
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 268435456,
                    "default": 67108864,
                },
                "expected_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-fA-F]{64}$",
                },
                "add_to_project": {"type": "boolean", "default": True},
                "provider": {"type": "string"},
                "x_field": {"type": "string"},
                "y_field": {"type": "string"},
                "delimiter": {"type": "string", "minLength": 1, "maxLength": 4, "default": ","},
                "crs": {"type": "string", "default": "EPSG:4326"},
            },
            ["url"],
        ),
    },
    {
        "name": "qgis_data_service",
        "description": "Add a remote XYZ, OGC WMS/WMTS/WFS, or ArcGIS service layer using QGIS providers and authentication settings.",
        "inputSchema": _object(
            {
                "kind": {
                    "type": "string",
                    "enum": ["xyz", "wms", "wmts", "wfs", "arcgis_featureserver", "arcgis_mapserver"],
                },
                "url": {"type": "string", "format": "uri"},
                "name": {"type": "string", "minLength": 1},
                "authcfg": {"type": "string"},
                "layer": {"type": "string"},
                "crs": {"type": "string"},
                "format": {"type": "string", "default": "image/png"},
                "zmin": {"type": "integer", "minimum": 0, "maximum": 30, "default": 0},
                "zmax": {"type": "integer", "minimum": 0, "maximum": 30, "default": 20},
            },
            ["kind", "url", "name"],
        ),
    },
    {
        "name": "qgis_data_refresh",
        "description": "Reload a project layer from its provider and repaint it.",
        "inputSchema": _object({"layer": {"type": "string"}}, ["layer"]),
    },
    {
        "name": "qgis_data_catalog",
        "description": "List acquisition formats, service kinds, authentication behavior, and download limits supported by the QGIS bridge.",
        "inputSchema": _object({}),
    },
    {
        "name": "qgis_data_provenance",
        "description": "Read the source, hash, fetch time, and cache metadata recorded for an acquired layer.",
        "inputSchema": _object({"layer": {"type": "string"}}, ["layer"]),
    },
    {
        "name": "qgis_layer_manage",
        "description": "Create or clone a layer, organize the layer tree, rename layers or groups, and control visibility, opacity, scale ranges, and provider subset filters.",
        "inputSchema": _object(
            {
                "action": {
                    "type": "string",
                    "enum": [
                        "create_memory",
                        "clone",
                        "rename_layer",
                        "move_layer",
                        "set_visibility",
                        "set_opacity",
                        "set_subset",
                        "set_scale_visibility",
                        "create_group",
                        "rename_group",
                        "remove_group",
                    ],
                },
                "layer": {"type": "string"},
                "name": {"type": "string"},
                "geometry": {
                    "type": "string",
                    "enum": ["NoGeometry", "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"],
                },
                "crs": {"type": "string", "default": "EPSG:4326"},
                "fields": {
                    "type": "array",
                    "maxItems": 200,
                    "items": _object(
                        {
                            "name": {"type": "string", "minLength": 1},
                            "type": {
                                "type": "string",
                                "enum": ["string", "integer", "double", "boolean", "date", "datetime"],
                                "default": "string",
                            },
                        },
                        ["name"],
                    ),
                },
                "group": {"type": "string", "description": "Slash-separated group path."},
                "index": {"type": "integer", "minimum": 0},
                "visible": {"type": "boolean"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                "subset": {"type": "string"},
                "minimum_scale": {"type": "number", "minimum": 0},
                "maximum_scale": {"type": "number", "minimum": 0},
                "remove_layers": {"type": "boolean", "default": False},
            },
            ["action"],
        ),
    },
    {
        "name": "qgis_style_apply",
        "description": "Apply a deterministic simple, categorized, or graduated vector style with bounded classes and curated color ramps.",
        "inputSchema": _object(
            {
                "layer": {"type": "string"},
                "mode": {"type": "string", "enum": ["simple", "categorized", "graduated"], "default": "simple"},
                "field": {"type": "string"},
                "color": {"type": "string", "default": "#3388ff"},
                "opacity": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
                "size": {"type": "number", "minimum": 0, "maximum": 100, "default": 3},
                "width": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.8},
                "classes": {"type": "integer", "minimum": 2, "maximum": 20, "default": 5},
                "color_ramp": {"type": "string", "enum": ["blue", "green", "fire"], "default": "blue"},
            },
            ["layer"],
        ),
    },
    {
        "name": "qgis_labels_apply",
        "description": "Enable or disable buffered labels for a vector layer using a field or a QGIS expression.",
        "inputSchema": _object(
            {
                "layer": {"type": "string"},
                "field": {"type": "string"},
                "enabled": {"type": "boolean", "default": True},
                "font_size": {"type": "number", "minimum": 1, "maximum": 200, "default": 10},
                "color": {"type": "string", "default": "#222222"},
                "buffer_size": {"type": "number", "minimum": 0, "maximum": 20, "default": 1},
                "buffer_color": {"type": "string", "default": "#ffffff"},
                "expression": {"type": "boolean", "default": False},
            },
            ["layer", "field"],
        ),
    },
    {
        "name": "qgis_layout",
        "description": "Create, list, remove, or export a standard QGIS print layout with map, title, legend, scale bar, and source attribution.",
        "inputSchema": _object(
            {
                "action": {"type": "string", "enum": ["create", "list", "remove", "export"]},
                "name": {"type": "string"},
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "orientation": {"type": "string", "enum": ["landscape", "portrait"], "default": "landscape"},
                "source_text": {"type": "string"},
                "path": {"type": "string"},
                "format": {"type": "string", "enum": ["pdf", "png", "svg"]},
                "dpi": {"type": "integer", "minimum": 72, "maximum": 1200, "default": 200},
            },
            ["action"],
        ),
    },
    {
        "name": "qgis_checkpoint",
        "description": "Create, list, restore, or delete a bounded QGZ project checkpoint for recoverable autonomous mutations.",
        "inputSchema": _object(
            {
                "action": {"type": "string", "enum": ["create", "list", "restore", "delete"]},
                "checkpoint_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "name": {"type": "string", "maxLength": 100},
            },
            ["action"],
        ),
    },
    {
        "name": "qgis_project_verify",
        "description": "Run a bounded delivery audit over project/layer validity, CRS, pending edits, sampled geometries, provenance, and layouts.",
        "inputSchema": _object(
            {
                "geometry_sample": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 100},
                "require_layout": {"type": "boolean", "default": False},
                "require_saved": {"type": "boolean", "default": False},
            }
        ),
    },
    {
        "name": "qgis_workflow",
        "description": "Create, inspect, run, resume, schedule, enable, disable, or delete a durable multi-step QGIS workflow that survives QGIS restarts.",
        "inputSchema": _object(
            {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "inspect", "run", "resume", "enable", "disable", "delete"],
                },
                "workflow_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "name": {"type": "string", "maxLength": 200},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": _object(
                        {
                            "method": {"type": "string"},
                            "params": {"type": "object"},
                            "continue_on_error": {"type": "boolean", "default": False},
                        },
                        ["method"],
                    ),
                },
                "interval_seconds": {"type": "integer", "minimum": 60},
                "enabled": {"type": "boolean", "default": False},
                "atomic": {"type": "boolean", "default": True},
                "resume": {"type": "boolean", "default": False},
            },
            ["action"],
        ),
    },
    {
        "name": "qgis_connectors",
        "description": "List opinionated autonomous connector presets and their authentication requirements.",
        "inputSchema": _object({}),
    },
    {
        "name": "qgis_fire_map",
        "description": "Autonomously build and verify a satellite map of active fire detections from NASA FIRMS, with optional print export. Requires a free NASA FIRMS MAP_KEY environment variable.",
        "inputSchema": _object(
            {
                "bounds": {
                    "type": "array",
                    "minItems": 4,
                    "maxItems": 4,
                    "items": {"type": "number"},
                    "description": "west, south, east, north; defaults to metropolitan France plus Corsica.",
                },
                "days": {"type": "integer", "minimum": 1, "maximum": 5, "default": 1},
                "source": {
                    "type": "string",
                    "enum": ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"],
                    "default": "VIIRS_SNPP_NRT",
                },
                "date": {"type": "string", "format": "date"},
                "map_key_env": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$", "default": "NASA_FIRMS_MAP_KEY"},
                "layer_name": {"type": "string", "default": "Incendies actifs — NASA FIRMS"},
                "add_satellite": {"type": "boolean", "default": True},
                "satellite_url": {"type": "string", "format": "uri"},
                "layout_name": {"type": ["string", "null"], "default": "Incendies actifs en France"},
                "output_path": {"type": "string"},
                "output_format": {"type": "string", "enum": ["pdf", "png", "svg"]},
            }
        ),
    },
    {
        "name": "qgis_artifact_release",
        "description": "Release a retained binary artifact before its TTL expires.",
        "inputSchema": _object(
            {"artifact_id": {"type": "string"}}, ["artifact_id"]
        ),
    },
]

TOOLS.extend(
    [
        {
            "name": "qgis_runtime",
            "description": "Inspect the live QGIS/Qt/Python runtime, compatibility profile, and installed data or Processing providers.",
            "inputSchema": _object(
                {
                    "action": {
                        "type": "string",
                        "enum": ["status", "compatibility", "providers"],
                        "default": "status",
                    }
                }
            ),
        },
        {
            "name": "qgis_tasks",
            "description": "List, inspect, or cancel tasks managed by the QGIS task manager.",
            "inputSchema": _object(
                {
                    "action": {
                        "type": "string",
                        "enum": ["list", "status", "cancel"],
                        "default": "list",
                    },
                    "task_id": {"type": ["string", "integer"]},
                }
            ),
        },
        {
            "name": "qgis_events",
            "description": "Read bounded revisioned project, layer, canvas, edit, render, and task events from the live session.",
            "inputSchema": _object(
                {
                    "after_revision": {"type": "integer", "minimum": 0, "default": 0},
                    "until_revision": {"type": "integer", "minimum": 0},
                    "event_types": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
                }
            ),
        },
        {
            "name": "qgis_render",
            "description": "Inspect, refresh, enable, or cancel rendering on the main QGIS map canvas.",
            "inputSchema": _object(
                {
                    "action": {
                        "type": "string",
                        "enum": ["status", "refresh", "refresh_all", "cancel", "set_enabled"],
                        "default": "status",
                    },
                    "enabled": {"type": "boolean"},
                }
            ),
        },
        {
            "name": "qgis_transaction",
            "description": "Coordinate edit sessions across one or more vector layers, including save, commit, and rollback.",
            "inputSchema": _object(
                {
                    "action": {
                        "type": "string",
                        "enum": ["status", "start", "save", "commit", "rollback"],
                        "default": "status",
                    },
                    "layers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Layer IDs or exact names; defaults to all vector layers.",
                    },
                    "stop_editing": {"type": "boolean", "default": True},
                }
            ),
        },
        {
            "name": "qgis_undo",
            "description": "Inspect or control the undo stack for an editable vector layer.",
            "inputSchema": _object(
                {
                    "action": {
                        "type": "string",
                        "enum": ["status", "undo", "redo"],
                        "default": "status",
                    },
                    "layer": {"type": "string"},
                    "steps": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
                }
            ),
        },
        {
            "name": "qgis_preflight",
            "description": "Validate an autonomous bridge-call plan and report mutations or elevated-trust escape hatches before execution.",
            "inputSchema": _object(
                {
                    "calls": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 100,
                        "items": _object(
                            {
                                "method": {"type": "string"},
                                "params": {"type": "object"},
                            },
                            ["method"],
                        ),
                    },
                    "require_saved_project": {"type": "boolean", "default": False},
                },
                ["calls"],
            ),
        },
        {
            "name": "qgis_state_diff",
            "description": "Summarize events and resources changed between two live-session revisions.",
            "inputSchema": _object(
                {
                    "from_revision": {"type": "integer", "minimum": 0},
                    "to_revision": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 1000},
                },
                ["from_revision"],
            ),
        },
        {
            "name": "qgis_diagnostics",
            "description": "Diagnose invalid layers, missing local sources, failed operations, rendering state, and recent QGIS errors.",
            "inputSchema": _object(
                {"include_logs": {"type": "boolean", "default": True}}
            ),
        },
        {
            "name": "qgis_permissions",
            "description": "Inspect effective MCP permissions for Python, network, filesystem outputs, credentials, and plugin installation.",
            "inputSchema": _object({}),
        },
        {
            "name": "qgis_auth",
            "description": "List or describe opaque QGIS authentication configurations without exposing stored secrets.",
            "inputSchema": _object(
                {
                    "action": {
                        "type": "string",
                        "enum": ["list", "describe"],
                        "default": "list",
                    },
                    "authcfg": {"type": "string"},
                }
            ),
        },
    ]
)

TOOLS.extend(
    [
        {
            "name": "qgis_project_manage",
            "description": "Create, open, save, save-as, close, or inspect the active QGIS project.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["status", "new", "open", "save", "save_as", "close"], "default": "status"},
                    "path": {"type": "string"},
                    "save_changes": {"type": "boolean", "default": False},
                }
            ),
        },
        {
            "name": "qgis_project_properties",
            "description": "Read or set project title, home path, CRS, ellipsoid, and custom variables.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["get", "set"], "default": "get"},
                    "title": {"type": "string"},
                    "home_path": {"type": "string"},
                    "crs": {"type": ["string", "integer"]},
                    "ellipsoid": {"type": "string"},
                    "variables": {"type": "object"},
                }
            ),
        },
        {
            "name": "qgis_project_repair",
            "description": "Inspect broken project layers or rebind them to replacement data sources without replacing the layer object.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["inspect", "apply"], "default": "inspect"},
                    "repairs": {
                        "type": "array",
                        "items": _object(
                            {
                                "layer": {"type": "string"},
                                "source": {"type": "string"},
                                "provider": {"type": "string"},
                                "name": {"type": "string"},
                            },
                            ["layer", "source"],
                        ),
                    },
                }
            ),
        },
        {
            "name": "qgis_source",
            "description": "Inspect, rebind, reload, or filter the source of a project layer with credential redaction.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "action": {"type": "string", "enum": ["inspect", "rebind", "reload", "set_subset"], "default": "inspect"},
                    "source": {"type": "string"},
                    "provider": {"type": "string"},
                    "name": {"type": "string"},
                    "subset": {"type": "string"},
                },
                ["layer"],
            ),
        },
        {
            "name": "qgis_canvas",
            "description": "List map views or inspect and control a QGIS 2D canvas extent, center, scale, rotation, CRS, and refresh.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["status", "list_views", "set_extent", "set_center", "set_scale", "set_rotation", "set_crs", "zoom_full", "zoom_selected", "refresh"], "default": "status"},
                    "view": {"type": "string"},
                    "extent": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
                    "center": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
                    "crs": {"type": ["string", "integer"]},
                    "scale": {"type": "number", "exclusiveMinimum": 0},
                    "rotation": {"type": "number"},
                }
            ),
        },
        {
            "name": "qgis_identify",
            "description": "Identify vector features and raster band values at a map coordinate across selected or all project layers.",
            "inputSchema": _object(
                {
                    "point": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
                    "crs": {"type": ["string", "integer"]},
                    "layers": {"type": "array", "items": {"type": "string"}},
                    "tolerance": {"type": "number", "minimum": 0, "default": 0},
                    "limit_per_layer": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
                ["point"],
            ),
        },
        {
            "name": "qgis_measure",
            "description": "Measure geodesic length, perimeter, area, or bearing using the project transform context and ellipsoid.",
            "inputSchema": _object(
                {
                    "action": {
                        "type": "string",
                        "enum": ["length", "perimeter", "area", "bearing"],
                    },
                    "geometry_wkt": {"type": "string"},
                    "points": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 2,
                            "items": {"type": "number"},
                        },
                    },
                    "crs": {"type": ["string", "integer"]},
                    "ellipsoid": {"type": "string"},
                },
                ["action"],
            ),
        },
        {
            "name": "qgis_bookmarks",
            "description": "List, add, remove, or zoom to spatial bookmarks stored in the active project.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["list", "add", "remove", "zoom"], "default": "list"},
                    "bookmark_id": {"type": "string"},
                    "name": {"type": "string"},
                    "group": {"type": "string"},
                    "extent": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
                    "crs": {"type": ["string", "integer"]},
                    "rotation": {"type": "number", "default": 0},
                }
            ),
        },
        {
            "name": "qgis_map_themes",
            "description": "List, capture, apply, or remove QGIS map themes for reproducible layer visibility and styles.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["list", "capture", "apply", "remove"], "default": "list"},
                    "name": {"type": "string"},
                }
            ),
        },
        {
            "name": "qgis_crs",
            "description": "Search or describe CRS definitions, transform points, extents, or WKT geometry, and assign a CRS to a layer.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["describe", "search", "transform_points", "transform_extent", "transform_geometry", "assign_layer"], "default": "describe"},
                    "value": {"type": ["string", "integer"]},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "source": {"type": ["string", "integer"]},
                    "target": {"type": ["string", "integer"]},
                    "points": {
                        "type": "array",
                        "maxItems": 10000,
                        "items": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": 2,
                            "items": {"type": "number"},
                        },
                    },
                    "extent": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
                    "geometry_wkt": {"type": "string"},
                    "layer": {"type": "string"},
                }
            ),
        },
        {
            "name": "qgis_expression",
            "description": "List QGIS expression functions or validate and evaluate an expression in global, project, layer, and feature scopes.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["functions", "validate", "evaluate"], "default": "validate"},
                    "expression": {"type": "string"},
                    "layer": {"type": "string"},
                    "feature_id": {"type": "integer"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                }
            ),
        },
        {
            "name": "qgis_metadata",
            "description": "Read or update structured project or layer metadata such as title, identifier, abstract, language, categories, and history.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["get", "set"], "default": "get"},
                    "layer": {"type": "string"},
                    "values": {"type": "object"},
                }
            ),
        },
        {
            "name": "qgis_connections",
            "description": "List connection-capable QGIS providers and stored provider connections without exposing connection secrets.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["providers", "list", "describe"], "default": "list"},
                    "provider": {"type": "string"},
                    "name": {"type": "string"},
                }
            ),
        },
    ]
)

TOOLS.extend(
    [
        {
            "name": "qgis_vector_schema",
            "description": "Inspect or edit vector fields, aliases, defaults, constraints, and editor widget setups.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "action": {"type": "string", "enum": ["inspect", "add", "delete", "rename", "configure"], "default": "inspect"},
                    "field": {"type": ["string", "integer"]},
                    "name": {"type": "string"},
                    "field_type": {"type": "string", "enum": ["string", "integer", "integer64", "double", "boolean", "date", "datetime", "time"], "default": "string"},
                    "length": {"type": "integer", "minimum": 0, "default": 0},
                    "precision": {"type": "integer", "minimum": 0, "default": 0},
                    "alias": {"type": "string"},
                    "default_expression": {"type": "string"},
                    "apply_default_on_update": {"type": "boolean", "default": False},
                    "constraint": {"type": "string", "enum": ["not_null", "unique", "expression"]},
                    "constraint_expression": {"type": "string"},
                    "constraint_description": {"type": "string"},
                    "constraint_strength": {"type": "string", "enum": ["hard", "soft"], "default": "hard"},
                    "widget_type": {"type": "string"},
                    "widget_config": {"type": "object"},
                },
                ["layer"],
            ),
        },
        {
            "name": "qgis_vector_statistics",
            "description": "Inspect vector schema or calculate bounded unique values, value counts, and numeric statistics.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "action": {"type": "string", "enum": ["summary", "unique", "value_counts", "numeric"], "default": "summary"},
                    "field": {"type": ["string", "integer"]},
                    "expression": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
                },
                ["layer"],
            ),
        },
        {
            "name": "qgis_geometry_edit",
            "description": "Set, translate, rotate, simplify, or make valid feature geometries inside an undoable edit command.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "feature_ids": {"type": "array", "minItems": 1, "maxItems": 10000, "items": {"type": "integer"}},
                    "action": {"type": "string", "enum": ["set", "translate", "rotate", "simplify", "make_valid"]},
                    "geometry_wkt": {"type": "string"},
                    "dx": {"type": "number", "default": 0},
                    "dy": {"type": "number", "default": 0},
                    "angle": {"type": "number", "default": 0},
                    "center": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
                    "tolerance": {"type": "number", "minimum": 0, "default": 0},
                },
                ["layer", "feature_ids", "action"],
            ),
        },
        {
            "name": "qgis_vector_indexes",
            "description": "Inspect spatial index availability or create provider-backed spatial and attribute indexes.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "action": {"type": "string", "enum": ["inspect", "create_spatial", "create_attribute"], "default": "inspect"},
                    "field": {"type": ["string", "integer"]},
                },
                ["layer"],
            ),
        },
        {
            "name": "qgis_vector_joins",
            "description": "List, add, or remove attribute joins between vector layers.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "action": {"type": "string", "enum": ["list", "add", "remove"], "default": "list"},
                    "join_layer": {"type": "string"},
                    "target_field": {"type": "string"},
                    "join_field": {"type": "string"},
                    "prefix": {"type": "string"},
                    "memory_cache": {"type": "boolean", "default": True},
                    "editable": {"type": "boolean", "default": False},
                    "upsert": {"type": "boolean", "default": False},
                },
                ["layer"],
            ),
        },
        {
            "name": "qgis_relations",
            "description": "List, add, or remove project relations with explicit parent/child field pairs.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["list", "add", "remove"], "default": "list"},
                    "relation_id": {"type": "string"},
                    "name": {"type": "string"},
                    "referenced_layer": {"type": "string"},
                    "referencing_layer": {"type": "string"},
                    "field_pairs": {
                        "oneOf": [
                            {"type": "object", "additionalProperties": {"type": "string"}},
                            {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 2,
                                    "items": {"type": "string"},
                                },
                            },
                        ]
                    },
                }
            ),
        },
        {
            "name": "qgis_snapping",
            "description": "Read or configure project snapping mode, target types, tolerance, units, intersections, and self-snapping.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["get", "set"], "default": "get"},
                    "enabled": {"type": "boolean"},
                    "mode": {"type": "string", "enum": ["active_layer", "all_layers", "advanced"]},
                    "types": {"type": "array", "items": {"type": "string", "enum": ["vertex", "segment", "area", "centroid", "middle", "endpoint"]}},
                    "tolerance": {"type": "number", "minimum": 0},
                    "units": {"type": "string", "enum": ["pixels", "project", "layer"]},
                    "intersection": {"type": "boolean"},
                    "self_snapping": {"type": "boolean"},
                }
            ),
        },
        {
            "name": "qgis_vector_select",
            "description": "Select vector features using all, invert, clear, expression, IDs, or a CRS-aware rectangle.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "action": {"type": "string", "enum": ["all", "invert", "clear", "expression", "ids", "rect"]},
                    "expression": {"type": "string"},
                    "feature_ids": {"type": "array", "items": {"type": "integer"}},
                    "extent": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
                    "crs": {"type": ["string", "integer"]},
                },
                ["layer", "action"],
            ),
        },
        {
            "name": "qgis_raster_inspect",
            "description": "Inspect raster dimensions and bands, sample a pixel, or calculate bounded statistics and histograms.",
            "inputSchema": _object(
                {
                    "layer": {"type": "string"},
                    "action": {"type": "string", "enum": ["inspect", "sample", "statistics", "histogram"], "default": "inspect"},
                    "band": {"type": "integer", "minimum": 1, "default": 1},
                    "point": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
                    "crs": {"type": ["string", "integer"]},
                    "sample_size": {"type": "integer", "minimum": 0, "default": 0},
                    "bins": {"type": "integer", "minimum": 2, "maximum": 65536, "default": 256},
                },
                ["layer"],
            ),
        },
    ]
)

TOOLS.extend(
    [
        {
            "name": "qgis_processing_providers",
            "description": "List or refresh installed Processing providers, algorithms, and supported output extensions.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["list", "refresh"], "default": "list"},
                    "provider": {"type": "string"},
                }
            ),
        },
        {
            "name": "qgis_processing_batch",
            "description": "Start up to 500 asynchronous runs of one Processing algorithm with independent parameter rows.",
            "inputSchema": _object(
                {
                    "algorithm": {"type": "string"},
                    "rows": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "object"}},
                    "retain_outputs": {"type": "boolean", "default": True},
                    "add_to_project": {"type": "boolean", "default": False},
                    "stop_on_error": {"type": "boolean", "default": False},
                },
                ["algorithm", "rows"],
            ),
        },
        {
            "name": "qgis_processing_history",
            "description": "List managed Processing operations or replay a previous operation with the same parameters.",
            "inputSchema": _object(
                {
                    "action": {"type": "string", "enum": ["list", "replay"], "default": "list"},
                    "operation_id": {"type": "string"},
                }
            ),
        },
        {
            "name": "qgis_processing_assets",
            "description": "List installed Processing algorithms, graphical models, or scripts with bounded search.",
            "inputSchema": _object(
                {
                    "kind": {"type": "string", "enum": ["models", "scripts", "algorithms"], "default": "models"},
                    "query": {"type": "string", "default": ""},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                }
            ),
        },
        {
            "name": "qgis_processing_context",
            "description": "Inspect Processing temporary output conventions, temporary folder, providers, and managed operation count.",
            "inputSchema": _object({}),
        },
        {
            "name": "qgis_database",
            "description": "Inspect schemas, tables, and fields; run paged SQL; or explicitly mutate database schemas and tables through a stored QGIS connection.",
            "inputSchema": _object(
                {
                    "provider": {"type": "string"},
                    "connection": {"type": "string"},
                    "action": {"type": "string", "enum": ["schemas", "tables", "fields", "query", "create_schema", "drop_schema", "rename_schema", "drop_table", "rename_table", "vacuum"], "default": "schemas"},
                    "schema": {"type": "string", "default": ""},
                    "table": {"type": "string"},
                    "sql": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
                    "allow_mutation": {"type": "boolean", "default": False},
                    "new_name": {"type": "string"},
                },
                ["provider", "connection"],
            ),
        },
        {
            "name": "qgis_connection_manage",
            "description": "Create, test, or delete a stored provider connection; connection URIs are accepted as input but never returned.",
            "inputSchema": _object(
                {
                    "provider": {"type": "string"},
                    "action": {"type": "string", "enum": ["create", "test", "delete"]},
                    "name": {"type": "string"},
                    "uri": {"type": "string"},
                    "configuration": {"type": "object"},
                },
                ["provider", "action"],
            ),
        },
    ]
)

_MUTATION_TOOLS = {
    "qgis_project_action",
    "qgis_selection_set",
    "qgis_vector_edit",
    "qgis_capability_invoke",
    "qgis_processing_start",
    "qgis_operation",
    "qgis_python_exec",
    "qgis_ui_invoke",
    "qgis_batch",
    "qgis_artifact_release",
    "qgis_data_fetch",
    "qgis_data_service",
    "qgis_data_refresh",
    "qgis_layer_manage",
    "qgis_style_apply",
    "qgis_labels_apply",
    "qgis_layout",
    "qgis_checkpoint",
    "qgis_workflow",
    "qgis_fire_map",
    "qgis_tasks",
    "qgis_render",
    "qgis_transaction",
    "qgis_undo",
    "qgis_project_manage",
    "qgis_project_properties",
    "qgis_project_repair",
    "qgis_source",
    "qgis_canvas",
    "qgis_bookmarks",
    "qgis_map_themes",
    "qgis_crs",
    "qgis_metadata",
    "qgis_vector_schema",
    "qgis_geometry_edit",
    "qgis_vector_indexes",
    "qgis_vector_joins",
    "qgis_relations",
    "qgis_snapping",
    "qgis_vector_select",
    "qgis_processing_providers",
    "qgis_processing_batch",
    "qgis_processing_history",
    "qgis_database",
    "qgis_connection_manage",
}
for _tool in TOOLS:
    if _tool["name"] in _MUTATION_TOOLS:
        _tool["inputSchema"]["properties"].update(
            {
                "idempotency_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Replay key for safely retrying this mutation.",
                },
                "if_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Require the current global session revision.",
                },
                "if_resource_revisions": {
                    "type": "object",
                    "additionalProperties": {"type": "integer", "minimum": 0},
                    "description": "Require exact revisions for the named qgis:// resources.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Validate preconditions and describe the mutation without executing it.",
                },
            }
        )

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
    "qgis_artifact_read": "artifact.read",
    "qgis_artifacts_list": "artifact.list",
    "qgis_artifact_release": "artifact.release",
    "qgis_data_fetch": "data.fetch",
    "qgis_data_service": "data.service",
    "qgis_data_refresh": "data.refresh",
    "qgis_data_catalog": "data.catalog",
    "qgis_data_provenance": "data.provenance",
    "qgis_layer_manage": "layer.manage",
    "qgis_style_apply": "cartography.style",
    "qgis_labels_apply": "cartography.labels",
    "qgis_layout": "layout.execute",
    "qgis_checkpoint": "checkpoint.execute",
    "qgis_project_verify": "project.verify",
    "qgis_workflow": "workflow.execute",
    "qgis_connectors": "connector.catalog",
    "qgis_fire_map": "connector.fire_map",
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
    "qgis_vector_schema": "vector.schema",
    "qgis_vector_statistics": "vector.statistics",
    "qgis_geometry_edit": "vector.geometry",
    "qgis_vector_indexes": "vector.index",
    "qgis_vector_joins": "vector.join",
    "qgis_relations": "project.relation",
    "qgis_snapping": "project.snapping",
    "qgis_vector_select": "selection.advanced",
    "qgis_raster_inspect": "raster.inspect",
    "qgis_processing_providers": "processing.provider",
    "qgis_processing_batch": "processing.batch",
    "qgis_processing_history": "processing.history",
    "qgis_processing_assets": "processing.assets",
    "qgis_processing_context": "processing.context",
    "qgis_database": "database.control",
    "qgis_connection_manage": "connection.manage",
}
