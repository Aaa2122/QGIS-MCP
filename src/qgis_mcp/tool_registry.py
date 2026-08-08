from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any

from .tool_catalog import (
    TOOL_METHODS,
    TOOLS,
    _object,
    enrich_tool_definition,
)

DISCOVERY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "qgis_context",
        "description": (
            "Build one task-conditioned, byte-bounded context pack from the live QGIS "
            "project, specialist tools, and runtime capabilities. Prefer this before a "
            "multi-step job instead of separate snapshot, search, and describe calls."
        ),
        "inputSchema": _object(
            {
                "task": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 2000,
                    "description": "Concrete QGIS job the model intends to perform.",
                },
                "budget_bytes": {
                    "type": "integer",
                    "minimum": 2048,
                    "maximum": 32768,
                    "default": 8192,
                    "description": "Hard UTF-8 budget for the structured context pack.",
                },
                "since_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Previous project revision for a compact delta snapshot.",
                },
                "detail": {
                    "type": "string",
                    "enum": ["summary", "standard", "full"],
                    "default": "summary",
                },
                "tool_limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "runtime_mode": {
                    "type": "string",
                    "enum": ["auto", "include", "skip"],
                    "default": "auto",
                },
            },
            ["task"],
        ),
    },
    {
        "name": "qgis_tools",
        "description": (
            "Search and activate QGIS specialist tools without loading their schemas into "
            "every model turn. Start with action=search, then call qgis_tool_call or activate "
            "the relevant toolset when native tool schemas are useful."
        ),
        "inputSchema": _object(
            {
                "action": {
                    "type": "string",
                    "enum": ["search", "describe", "list_toolsets", "activate", "reset", "status"],
                    "default": "search",
                },
                "query": {"type": "string"},
                "tool": {"type": "string"},
                "toolsets": {"type": "array", "items": {"type": "string"}},
                "replace": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5},
                "detail": {
                    "type": "string",
                    "enum": ["adaptive", "names", "summary", "schema"],
                    "default": "adaptive",
                    "description": (
                        "Progressive disclosure level. Adaptive loads only the top one or two "
                        "schemas according to score confidence."
                    ),
                },
                "include_schema": {
                    "type": "boolean",
                    "description": "Deprecated compatibility switch; use detail instead.",
                },
                "runtime_mode": {
                    "type": "string",
                    "enum": ["auto", "include", "skip"],
                    "default": "auto",
                    "description": "Include live QGIS Processing matches always, never, or when relevant.",
                },
            }
        ),
    },
    {
        "name": "qgis_tool_call",
        "description": (
            "Execute any specialist tool returned by qgis_tools search while keeping the full "
            "catalog hidden. Put mutation guards such as idempotency_key and if_revision inside "
            "arguments."
        ),
        "inputSchema": _object(
            {
                "tool": {"type": "string", "pattern": "^qgis_[a-z0-9_]+$"},
                "arguments": {"type": "object", "default": {}},
            },
            ["tool"],
        ),
    },
]

enrich_tool_definition(DISCOVERY_TOOLS[0], mutation=False)
enrich_tool_definition(DISCOVERY_TOOLS[1], mutation=False)
enrich_tool_definition(
    DISCOVERY_TOOLS[2], mutation=True, destructive=True, open_world=True
)
DISCOVERY_TOOLS[0]["outputSchema"]["properties"] = {
    "task": {"type": "string"},
    "budget_bytes": {"type": "integer"},
    "returned_bytes": {"type": "integer"},
    "snapshot": {"type": "object"},
    "tools": {"type": "array"},
    "runtime_matches": {"type": "array"},
    "preconditions": {"type": "object"},
}
DISCOVERY_TOOLS[1]["outputSchema"]["properties"] = {
    "query": {"type": "string"},
    "matches": {"type": "array"},
    "runtime_matches": {"type": "array"},
    "total_matches": {"type": "integer"},
}

DISCOVERY_TOOL_NAMES = {tool["name"] for tool in DISCOVERY_TOOLS}

# These common tools remain native in adaptive mode. Every other tool stays reachable
# through qgis_tools + qgis_tool_call, so catalog growth does not increase the default cost.
CORE_TOOL_NAMES = {
    "qgis_session_snapshot",
    "qgis_operation",
    "qgis_screenshot",
    "qgis_batch",
}

TOOLSET_DESCRIPTIONS = {
    "project": "Projects, layers, canvas, CRS, bookmarks, themes, metadata and expressions.",
    "vector": "Vector schemas, features, selections, geometry, joins, relations and export.",
    "raster": "Raster inspection, styling and raster-oriented processing controls.",
    "processing": "Processing providers, algorithms, tasks, batches, outputs and artifacts.",
    "data": "External data, services, databases, connections, provenance and refresh.",
    "cartography": "Renderers, symbols, labels, styles, layouts, atlas and validation.",
    "advanced_data": "Mesh, point cloud, vector tiles, tiled scenes, temporal and elevation.",
    "autonomy": "Workflows, checkpoints, connectors, permissions, preflight and recovery.",
    "runtime": "QGIS runtime, UI, events, logs, tasks, undo, transactions and diagnostics.",
    "ecosystem": "Plugins, settings, shortcuts, GPS, 3D views, QGIS Server and offline work.",
    "authoring": "Forms, diagrams, annotations and geometry quality authoring.",
    "qa": "Compatibility, project audit and self-test tools.",
}

TOOLSET_KEYWORDS = {
    "project": "project projet layer couche canvas canevas crs bookmark signet theme metadata expression",
    "vector": "vector vecteur feature entite attribut geometry geometrie join relation selection export",
    "raster": "raster image pixel band bande nodata geotiff cog",
    "processing": "processing traitement algorithm algorithme task tache batch model output artifact",
    "data": "data donnee service web database base postgis connection provenance fetch download refresh",
    "cartography": "cartography cartographie style symbology symbologie symbol label etiquette layout mise page atlas legend",
    "advanced_data": "mesh maillage point cloud nuage de points vector tile tiled scene temporal elevation 3d",
    "autonomy": "autonomy autonome workflow checkpoint recovery reprise connector fire incendie permission preflight",
    "runtime": "runtime ui interface event log task undo transaction diagnostic render screenshot",
    "ecosystem": "plugin setting shortcut gps server offline ecosystem extension",
    "authoring": "form formulaire diagram annotation geometry quality qualite",
    "qa": "qa audit compatibility compatibilite test health sante",
}

# Tool-specific aliases improve multilingual and task-oriented discovery without
# leaking every broad toolset keyword into every member of that toolset.
TOOL_ALIASES = {
    "qgis_3d_views": "3d three dimensional vue scene camera terrain",
    "qgis_capabilities_search": (
        "find discover processing algorithm raster slope buffer proximity contour hillshade "
        "geocode rechercher trouver algorithme pente tampon proximite"
    ),
    "qgis_context": (
        "context task plan prepare project layers capability schema contexte tache planifier "
        "preparer projet couches capacite"
    ),
    "qgis_geometry_quality": "topology validate invalid geometry repair geometrie topologie",
    "qgis_labeling": "label collision placement overlap etiquette collision chevauchement",
    "qgis_offline": "offline project package sync field mobile hors ligne synchronisation",
    "qgis_plugin_advisor": (
        "plugin extension addon recommend discover install capability missing useful "
        "extension recommander decouvrir installer capacite manquante utile"
    ),
    "qgis_point_cloud": "point cloud lidar las laz nuage points nuage de points",
    "qgis_processing_start": (
        "analysis processing algorithm raster slope buffer proximity contour hillshade interpolate "
        "analyse traitement algorithme pente tampon proximite"
    ),
    "qgis_project_action": (
        "load add open geojson shapefile geopackage raster lidar charger ajouter ouvrir"
    ),
    "qgis_project_repair": "repair broken invalid missing source reparer casse invalide source",
    "qgis_style_apply": (
        "symbology categorized graduated pseudocolor style symbologie categorise gradue"
    ),
    "qgis_vector_joins": "join attribute table joindre jointure attributaire",
    "qgis_vector_export": "export geojson geopackage shapefile csv parquet exporter",
}

_METHOD_TOOLSETS = {
    "session": "project",
    "project": "project",
    "layer": "project",
    "canvas": "project",
    "map": "project",
    "bookmark": "project",
    "map_theme": "project",
    "crs": "project",
    "expression": "project",
    "metadata": "project",
    "feature": "vector",
    "selection": "vector",
    "vector": "vector",
    "raster": "raster",
    "processing": "processing",
    "operation": "processing",
    "handle": "processing",
    "artifact": "processing",
    "batch": "processing",
    "data": "data",
    "database": "data",
    "connection": "data",
    "cartography": "cartography",
    "style": "cartography",
    "layout": "cartography",
    "mesh": "advanced_data",
    "point_cloud": "advanced_data",
    "vector_tile": "advanced_data",
    "tiled_scene": "advanced_data",
    "workflow": "autonomy",
    "visual": "autonomy",
    "checkpoint": "autonomy",
    "connector": "autonomy",
    "runtime": "runtime",
    "ui": "runtime",
    "logs": "runtime",
    "python": "runtime",
    "plugins": "ecosystem",
    "ecosystem": "ecosystem",
    "authoring": "authoring",
    "qa": "qa",
}

_SPECIAL_TOOLSETS = {
    "qgis_temporal": "advanced_data",
    "qgis_elevation": "advanced_data",
    "qgis_vector_export": "vector",
    "qgis_geometry_quality": "authoring",
    "qgis_permissions": "autonomy",
    "qgis_plugin_advisor": "ecosystem",
    "qgis_preflight": "autonomy",
    "qgis_fire_map": "autonomy",
    "qgis_connectors": "autonomy",
}


_STOPWORDS = {
    "and",
    "avec",
    "dans",
    "des",
    "for",
    "les",
    "pour",
    "the",
    "une",
}


def _normalize(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )


def _tokens(value: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", _normalize(value))
        if len(token) > 2 and token not in _STOPWORDS
    }
    return {token[:-1] if len(token) > 4 and token.endswith("s") else token for token in tokens}


_PROCESSING_INTENTS = _tokens(
    "analysis algorithm processing slope buffer proximity contour hillshade interpolate "
    "geocode raster calculator analyse algorithme traitement pente tampon proximite"
)


class ToolRegistry:
    """Adaptive view over the complete specialist tool catalog."""

    def __init__(self, mode: str | None = None) -> None:
        requested = (mode or os.environ.get("QGIS_MCP_TOOL_MODE", "adaptive")).strip().lower()
        aliases = {"compact": "adaptive", "granular": "full"}
        self.mode = aliases.get(requested, requested)
        if self.mode not in {"adaptive", "full"}:
            self.mode = "adaptive"
        self._tools = {tool["name"]: tool for tool in TOOLS}
        self._discovery = {tool["name"]: tool for tool in DISCOVERY_TOOLS}
        self._active_toolsets: set[str] = set()
        self._toolsets: dict[str, set[str]] = {
            name: set() for name in TOOLSET_DESCRIPTIONS
        }
        for name, method in TOOL_METHODS.items():
            toolset = self._toolset_for(name, method)
            self._toolsets[toolset].add(name)
        self._search_index = self._build_search_index()

    def _build_search_index(self) -> list[dict[str, Any]]:
        index = []
        for name, tool in self._tools.items():
            method = TOOL_METHODS[name]
            toolset = self._toolset_for(name, method)
            direct_haystack = _normalize(
                " ".join(
                    (
                        name.replace("_", " "),
                        method.replace(".", " "),
                        str(tool.get("description", "")),
                    )
                )
            )
            alias_haystack = _normalize(TOOL_ALIASES.get(name, ""))
            index.append(
                {
                    "name": name,
                    "tool": tool,
                    "toolset": toolset,
                    "direct_haystack": direct_haystack,
                    "direct_tokens": _tokens(direct_haystack),
                    "alias_haystack": alias_haystack,
                    "alias_tokens": _tokens(alias_haystack),
                }
            )
        return index

    @staticmethod
    def _toolset_for(name: str, method: str) -> str:
        if name in _SPECIAL_TOOLSETS:
            return _SPECIAL_TOOLSETS[name]
        return _METHOD_TOOLSETS.get(method.split(".", 1)[0], "runtime")

    def visible_tools(self, *, include_active: bool = True) -> list[dict[str, Any]]:
        if self.mode == "full":
            names = set(self._tools)
        else:
            names = set(CORE_TOOL_NAMES)
            if include_active:
                for toolset in self._active_toolsets:
                    names.update(self._toolsets[toolset])
        return list(DISCOVERY_TOOLS) + sorted(
            (tool for tool in TOOLS if tool["name"] in names),
            key=lambda tool: tool["name"],
        )

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def method_for(self, name: str) -> str:
        return TOOL_METHODS[name]

    def command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", "search"))
        if action == "search":
            include_schema = arguments.get("include_schema")
            return self.search(
                str(arguments.get("query", "")),
                limit=int(arguments.get("limit", 5)),
                detail=str(arguments.get("detail", "adaptive")),
                include_schema=(
                    bool(include_schema) if include_schema is not None else None
                ),
            )
        if action == "describe":
            return self.describe(str(arguments.get("tool", "")))
        if action == "list_toolsets":
            return self.toolsets()
        if action == "activate":
            requested = arguments.get("toolsets") or []
            if not isinstance(requested, list):
                raise ValueError("toolsets must be an array")
            return self.activate(
                [str(item) for item in requested],
                replace=bool(arguments.get("replace", False)),
            )
        if action == "reset":
            self._active_toolsets.clear()
            return self.status()
        if action == "status":
            return self.status()
        raise ValueError(f"Unknown qgis_tools action: {action}")

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        detail: str = "adaptive",
        include_schema: bool | None = None,
    ) -> dict[str, Any]:
        if include_schema is not None:
            detail = "schema" if include_schema else "summary"
        if detail not in {"adaptive", "names", "summary", "schema"}:
            raise ValueError("detail must be adaptive, names, summary, or schema")
        query_tokens = _tokens(query)
        normalized = _normalize(query).strip()
        matches: list[tuple[int, str, dict[str, Any], set[str]]] = []
        for entry in self._search_index:
            name = entry["name"]
            tool = entry["tool"]
            direct_haystack = entry["direct_haystack"]
            direct_tokens = entry["direct_tokens"]
            alias_haystack = entry["alias_haystack"]
            alias_tokens = entry["alias_tokens"]
            if not query_tokens:
                score = 1
                matched_terms: set[str] = set()
            else:
                direct_overlap = query_tokens & direct_tokens
                alias_overlap = query_tokens & alias_tokens
                matched_terms = direct_overlap | alias_overlap
                # Tool-specific aliases represent curated intent, so they must
                # outrank incidental words shared by broad tool descriptions.
                score = len(direct_overlap) * 24 + len(alias_overlap) * 30
                if normalized and normalized in direct_haystack:
                    score += 40
                if normalized and normalized in alias_haystack:
                    score += 35
                if normalized and normalized in name.casefold():
                    score += 50
                if not matched_terms and score == 0:
                    continue
            matches.append((score, name, entry, matched_terms))
        matches.sort(key=lambda item: (-item[0], item[1]))
        selected = matches[: max(1, min(limit, 30))]
        schema_count = 0
        if detail == "schema":
            schema_count = len(selected)
        elif detail == "adaptive" and selected:
            schema_count = 1
            if len(selected) > 1:
                first_score, second_score = selected[0][0], selected[1][0]
                confidence_margin = max(12, int(max(first_score, 1) * 0.2))
                if first_score - second_score <= confidence_margin:
                    schema_count = 2
        results: list[dict[str, Any]] = []
        for index, (score, name, entry, matched_terms) in enumerate(selected):
            tool = entry["tool"]
            examples = self._examples(tool)
            summary: dict[str, Any] = {
                "name": name,
                "toolset": entry["toolset"],
                "relevance": score,
                "matched_terms": sorted(matched_terms),
                "call": {"tool": name, "arguments": examples[0]},
            }
            if detail != "names":
                summary["description"] = tool["description"]
            if index < schema_count:
                summary["inputSchema"] = tool["inputSchema"]
                summary["examples"] = examples
            results.append(summary)
        toolset_matches = []
        for name, description in TOOLSET_DESCRIPTIONS.items():
            tokens = _tokens(" ".join((name, description, TOOLSET_KEYWORDS[name])))
            overlap = query_tokens & tokens
            if overlap:
                toolset_matches.append(
                    {
                        "name": name,
                        "matched_terms": sorted(overlap),
                        "relevance": len(overlap),
                    }
                )
        toolset_matches.sort(key=lambda item: (-item["relevance"], item["name"]))
        return {
            "query": query,
            "matches": results,
            "total_matches": len(matches),
            "detail": detail,
            "schema_tools": [item["name"] for item in results if "inputSchema" in item],
            "suggested_toolsets": toolset_matches[:3],
            "runtime_discovery_recommended": bool(query_tokens & _PROCESSING_INTENTS),
            "usage": (
                "Call qgis_tool_call with a returned match. Use detail=schema or describe only "
                "when the required schema was not expanded."
            ),
        }

    def describe(self, name: str) -> dict[str, Any]:
        if name not in self._tools:
            raise ValueError(f"Unknown specialist tool: {name}")
        tool = self._tools[name]
        method = TOOL_METHODS[name]
        examples = self._examples(tool)
        return {
            "name": name,
            "toolset": self._toolset_for(name, method),
            "bridge_method": method,
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "outputSchema": tool["outputSchema"],
            "annotations": tool["annotations"],
            "examples": examples,
            "call": {"tool": name, "arguments": examples[0]},
        }

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> list[str]:
        tool = self._tools.get(name) or self._discovery.get(name)
        if tool is None:
            return ["Unknown specialist tool: {}".format(name)]
        errors: list[str] = []
        _validate_schema(arguments, tool["inputSchema"], "$", errors)
        return errors

    @staticmethod
    def _examples(tool: dict[str, Any]) -> list[dict[str, Any]]:
        schema = tool["inputSchema"]
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        example: dict[str, Any] = {}
        for name in required:
            value = _example_value(properties.get(name, {}), name)
            if value is not None:
                example[name] = value
        if not example:
            for name, value_schema in properties.items():
                if "default" in value_schema:
                    example[name] = value_schema["default"]
                    break
        return [example]

    def activate(self, requested: list[str], *, replace: bool = False) -> dict[str, Any]:
        unknown = sorted(set(requested) - set(self._toolsets))
        if unknown:
            raise ValueError(f"Unknown toolsets: {', '.join(unknown)}")
        if replace:
            self._active_toolsets.clear()
        self._active_toolsets.update(requested)
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "catalog_tools": len(self._tools),
            "visible_tools": len(self.visible_tools()),
            "active_toolsets": sorted(self._active_toolsets),
            "catalog_bytes": len(json.dumps(TOOLS, ensure_ascii=False, separators=(",", ":"))),
            "visible_catalog_bytes": len(
                json.dumps(self.visible_tools(), ensure_ascii=False, separators=(",", ":"))
            ),
        }

    def toolsets(self) -> dict[str, Any]:
        return {
            "toolsets": [
                {
                    "name": name,
                    "description": TOOLSET_DESCRIPTIONS[name],
                    "tool_count": len(self._toolsets[name]),
                    "active": name in self._active_toolsets,
                }
                for name in TOOLSET_DESCRIPTIONS
            ]
        }


def _example_value(schema: dict[str, Any], name: str) -> Any:
    if "default" in schema:
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][0]
    value_type = schema.get("type")
    if isinstance(value_type, list):
        value_type = next((item for item in value_type if item != "null"), None)
    if value_type == "string":
        return "<{}>".format(name)
    if value_type == "integer":
        return max(0, int(schema.get("minimum", 0)))
    if value_type == "number":
        return float(schema.get("minimum", 0))
    if value_type == "boolean":
        return False
    if value_type == "array":
        item = _example_value(schema.get("items", {}), "item")
        return [] if item is None else [item]
    if value_type == "object":
        return {}
    return None


def _validate_schema(value: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if len(errors) >= 20:
        return
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if alternatives:
        if not any(_schema_matches(value, option) for option in alternatives):
            errors.append("{} does not match any accepted schema".format(path))
        return
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    expected_types = [item for item in expected_types if item]
    if expected_types and not any(_is_type(value, item) for item in expected_types):
        errors.append(
            "{} must be {}".format(path, " or ".join(str(item) for item in expected_types))
        )
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append("{} must be one of {}".format(path, schema["enum"]))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                errors.append("{}.{} is required".format(path, required))
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for name in value:
                if name not in properties:
                    errors.append("{}.{} is not allowed".format(path, name))
        for name, item in value.items():
            item_schema = properties.get(name)
            if item_schema is None and isinstance(additional, dict):
                item_schema = additional
            if isinstance(item_schema, dict):
                _validate_schema(item, item_schema, "{}.{}".format(path, name), errors)
    elif isinstance(value, list):
        if len(value) < int(schema.get("minItems", 0)):
            errors.append("{} has too few items".format(path))
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append("{} has too many items".format(path))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, "{}[{}]".format(path, index), errors)
    elif isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            errors.append("{} is too short".format(path))
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append("{} is too long".format(path))
        if schema.get("pattern") and re.fullmatch(schema["pattern"], value) is None:
            errors.append("{} has an invalid format".format(path))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("{} is below the minimum".format(path))
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append("{} must be greater than {}".format(path, schema["exclusiveMinimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("{} is above the maximum".format(path))


def _schema_matches(value: Any, schema: dict[str, Any]) -> bool:
    errors: list[str] = []
    _validate_schema(value, schema, "$", errors)
    return not errors


def _is_type(value: Any, expected: str) -> bool:
    return {
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "null": value is None,
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
    }.get(expected, True)
