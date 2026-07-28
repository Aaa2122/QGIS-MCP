from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any

from .tool_catalog import TOOL_METHODS, TOOLS, _object

DISCOVERY_TOOLS: list[dict[str, Any]] = [
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
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
                "include_schema": {"type": "boolean", "default": False},
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

DISCOVERY_TOOL_NAMES = {tool["name"] for tool in DISCOVERY_TOOLS}

# These common tools remain native in adaptive mode. Every other tool stays reachable
# through qgis_tools + qgis_tool_call, so catalog growth does not increase the default cost.
CORE_TOOL_NAMES = {
    "qgis_session_snapshot",
    "qgis_project_inspect",
    "qgis_layer_inspect",
    "qgis_capabilities_search",
    "qgis_capability_describe",
    "qgis_processing_start",
    "qgis_operation",
    "qgis_screenshot",
    "qgis_workflow",
    "qgis_project_verify",
    "qgis_diagnostics",
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
    "checkpoint": "autonomy",
    "connector": "autonomy",
    "runtime": "runtime",
    "ui": "runtime",
    "logs": "runtime",
    "python": "runtime",
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

    @staticmethod
    def _toolset_for(name: str, method: str) -> str:
        if name in _SPECIAL_TOOLSETS:
            return _SPECIAL_TOOLSETS[name]
        return _METHOD_TOOLSETS.get(method.split(".", 1)[0], "runtime")

    def visible_tools(self) -> list[dict[str, Any]]:
        if self.mode == "full":
            names = set(self._tools)
        else:
            names = set(CORE_TOOL_NAMES)
            for toolset in self._active_toolsets:
                names.update(self._toolsets[toolset])
        return list(DISCOVERY_TOOLS) + [tool for tool in TOOLS if tool["name"] in names]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def method_for(self, name: str) -> str:
        return TOOL_METHODS[name]

    def command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = str(arguments.get("action", "search"))
        if action == "search":
            return self.search(
                str(arguments.get("query", "")),
                limit=int(arguments.get("limit", 10)),
                include_schema=bool(arguments.get("include_schema", False)),
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

    def search(self, query: str, *, limit: int = 10, include_schema: bool = False) -> dict[str, Any]:
        query_tokens = _tokens(query)
        normalized = _normalize(query).strip()
        matches: list[tuple[int, str, dict[str, Any]]] = []
        for name, tool in self._tools.items():
            method = TOOL_METHODS[name]
            toolset = self._toolset_for(name, method)
            direct_haystack = " ".join(
                (
                    name.replace("_", " "),
                    method.replace(".", " "),
                    str(tool.get("description", "")),
                )
            )
            direct_haystack = _normalize(direct_haystack)
            expanded_haystack = " ".join(
                (direct_haystack, toolset.replace("_", " "), TOOLSET_KEYWORDS[toolset])
            )
            direct_tokens = _tokens(direct_haystack)
            expanded_tokens = _tokens(expanded_haystack)
            if not query_tokens:
                score = 1
            else:
                direct_overlap = query_tokens & direct_tokens
                expanded_overlap = query_tokens & expanded_tokens
                score = len(direct_overlap) * 20 + len(expanded_overlap - direct_overlap) * 3
                if normalized and normalized in direct_haystack:
                    score += 40
                if normalized and normalized in name.casefold():
                    score += 50
                if not expanded_overlap and score == 0:
                    continue
            summary: dict[str, Any] = {
                "name": name,
                "toolset": toolset,
                "description": tool["description"],
                "call": {"tool": name, "arguments": {}},
            }
            if include_schema:
                summary["inputSchema"] = tool["inputSchema"]
            matches.append((score, name, summary))
        matches.sort(key=lambda item: (-item[0], item[1]))
        results = [item[2] for item in matches[: max(1, min(limit, 30))]]
        return {
            "query": query,
            "matches": results,
            "total_matches": len(matches),
            "usage": "Call qgis_tool_call with one match, or activate its toolset for native schemas.",
        }

    def describe(self, name: str) -> dict[str, Any]:
        if name not in self._tools:
            raise ValueError(f"Unknown specialist tool: {name}")
        tool = self._tools[name]
        method = TOOL_METHODS[name]
        return {
            "name": name,
            "toolset": self._toolset_for(name, method),
            "bridge_method": method,
            "description": tool["description"],
            "inputSchema": tool["inputSchema"],
            "call": {"tool": name, "arguments": {}},
        }

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
