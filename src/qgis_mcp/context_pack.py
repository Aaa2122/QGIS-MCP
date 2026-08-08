from __future__ import annotations

import json
import re
import unicodedata
from typing import Any


def build_context_pack(
    task: str,
    snapshot: Any,
    discovery: dict[str, Any],
    *,
    budget_bytes: int,
) -> dict[str, Any]:
    """Return a valid JSON context pack that never exceeds ``budget_bytes``."""

    budget = max(2048, min(int(budget_bytes), 32768))
    compact_snapshot, resource_revisions = _compact_snapshot(snapshot, task)
    pack: dict[str, Any] = {
        "task": task,
        "budget_bytes": budget,
        "snapshot": compact_snapshot,
        "tools": list(discovery.get("matches") or []),
        "runtime_matches": list(discovery.get("runtime_matches") or []),
        "recent_hints": list(discovery.get("hints") or [])[:3],
        "preconditions": {
            "if_revision": compact_snapshot.get("revision"),
            "if_resource_revisions": resource_revisions,
        },
        "guidance": (
            "Use the expanded schema directly. Read a handle or describe a capability only "
            "when this pack does not contain the required detail. Pass the returned revision "
            "preconditions to mutations."
        ),
        "truncated": False,
        "omitted_sections": [],
    }

    reducers = (
        _drop_secondary_runtime_schemas,
        _drop_secondary_tool_schemas,
        _trim_snapshot_lists,
        _drop_snapshot_changes,
        _trim_match_lists,
        _shorten_descriptions,
        _minimal_snapshot,
        _minimal_pack,
    )
    for reducer in reducers:
        if _encoded_size(pack) <= budget:
            break
        reducer(pack)
        pack["truncated"] = True

    _set_returned_size(pack)
    if _encoded_size(pack) > budget:
        pack["tools"] = [
            {
                key: match[key]
                for key in ("name", "call", "relevance")
                if key in match
            }
            for match in list(pack.get("tools") or [])[:1]
            if isinstance(match, dict)
        ]
        pack["runtime_matches"] = [
            {
                key: match[key]
                for key in ("kind", "id", "name", "call")
                if key in match
            }
            for match in list(pack.get("runtime_matches") or [])[:1]
            if isinstance(match, dict)
        ]
        pack["recent_hints"] = []
        pack["task"] = task[:512]
        pack["guidance"] = "Use the first candidate and pass revision preconditions."
        _mark(pack, "expanded_schema")
        _set_returned_size(pack)
    return pack


def _compact_snapshot(snapshot: Any, task: str) -> tuple[dict[str, Any], dict[str, int]]:
    if not isinstance(snapshot, dict):
        return {"available": False}, {}
    tokens = _tokens(task)
    layers = list(snapshot.get("layers") or [])
    ranked_layers = sorted(
        layers,
        key=lambda layer: (
            -len(tokens & _tokens(json.dumps(layer, ensure_ascii=False, default=str))),
            str(layer.get("name", "")) if isinstance(layer, dict) else "",
        ),
    )[:20]
    compact: dict[str, Any] = {}
    for key in (
        "revision",
        "incremental",
        "project",
        "canvas",
        "active_layer",
        "selection",
        "editing",
        "tasks",
        "changes",
    ):
        if key in snapshot:
            compact[key] = snapshot[key]
    compact["layers"] = ranked_layers
    compact["layer_count"] = snapshot.get("layer_count", len(layers))

    revisions = snapshot.get("resource_revisions") or {}
    if not isinstance(revisions, dict):
        return compact, {}
    layer_ids = {
        str(layer.get("id"))
        for layer in ranked_layers
        if isinstance(layer, dict) and layer.get("id")
    }
    relevant = {
        str(uri): int(revision)
        for uri, revision in revisions.items()
        if not layer_ids
        or any(layer_id in str(uri) for layer_id in layer_ids)
        or str(uri) in {"qgis://session", "qgis://project"}
    }
    return compact, dict(list(relevant.items())[:20])


def _drop_secondary_runtime_schemas(pack: dict[str, Any]) -> None:
    for match in pack.get("runtime_matches", [])[1:]:
        if isinstance(match, dict):
            match.pop("schema", None)
    _mark(pack, "secondary_runtime_schemas")


def _drop_secondary_tool_schemas(pack: dict[str, Any]) -> None:
    for match in pack.get("tools", [])[1:]:
        if isinstance(match, dict):
            match.pop("inputSchema", None)
            match.pop("examples", None)
    _mark(pack, "secondary_tool_schemas")


def _trim_snapshot_lists(pack: dict[str, Any]) -> None:
    snapshot = pack.get("snapshot")
    if not isinstance(snapshot, dict):
        return
    for key in ("layers", "tasks", "selection", "editing"):
        if isinstance(snapshot.get(key), list):
            snapshot[key] = snapshot[key][:8]
    revisions = pack.get("preconditions", {}).get("if_resource_revisions")
    if isinstance(revisions, dict):
        pack["preconditions"]["if_resource_revisions"] = dict(
            list(revisions.items())[:8]
        )
    _mark(pack, "snapshot_list_tail")


def _drop_snapshot_changes(pack: dict[str, Any]) -> None:
    snapshot = pack.get("snapshot")
    if isinstance(snapshot, dict):
        snapshot.pop("changes", None)
        snapshot.pop("tasks", None)
        snapshot.pop("editing", None)
    _mark(pack, "snapshot_changes")


def _trim_match_lists(pack: dict[str, Any]) -> None:
    pack["tools"] = list(pack.get("tools") or [])[:2]
    pack["runtime_matches"] = list(pack.get("runtime_matches") or [])[:2]
    pack["recent_hints"] = list(pack.get("recent_hints") or [])[:2]
    _mark(pack, "lower_ranked_matches")


def _shorten_descriptions(pack: dict[str, Any]) -> None:
    for key in ("tools", "runtime_matches"):
        for match in pack.get(key, []):
            if not isinstance(match, dict):
                continue
            for field in ("description", "help", "group", "tool_tip"):
                if isinstance(match.get(field), str) and len(match[field]) > 160:
                    match[field] = match[field][:157] + "..."
    _mark(pack, "long_descriptions")


def _minimal_snapshot(pack: dict[str, Any]) -> None:
    snapshot = pack.get("snapshot")
    if not isinstance(snapshot, dict):
        return
    pack["snapshot"] = {
        key: snapshot[key]
        for key in ("revision", "incremental", "project", "active_layer", "layer_count")
        if key in snapshot
    }
    _mark(pack, "snapshot_detail")


def _minimal_pack(pack: dict[str, Any]) -> None:
    tools = list(pack.get("tools") or [])[:1]
    if tools and isinstance(tools[0], dict):
        tools[0] = {
            key: tools[0][key]
            for key in ("name", "description", "call", "inputSchema", "relevance")
            if key in tools[0]
        }
    pack["tools"] = tools
    pack["runtime_matches"] = list(pack.get("runtime_matches") or [])[:1]
    pack["recent_hints"] = list(pack.get("recent_hints") or [])[:1]
    snapshot = pack.get("snapshot")
    if isinstance(snapshot, dict):
        pack["snapshot"] = {
            key: snapshot[key]
            for key in ("revision", "incremental", "layer_count")
            if key in snapshot
        }
    pack["preconditions"]["if_resource_revisions"] = {}
    _mark(pack, "all_optional_detail")


def _mark(pack: dict[str, Any], section: str) -> None:
    omitted = pack.setdefault("omitted_sections", [])
    if section not in omitted:
        omitted.append(section)


def _set_returned_size(pack: dict[str, Any]) -> None:
    pack["returned_bytes"] = 0
    for _ in range(3):
        size = _encoded_size(pack)
        if pack["returned_bytes"] == size:
            break
        pack["returned_bytes"] = size


def _encoded_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    )


def _tokens(value: str) -> set[str]:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value).casefold())
        if not unicodedata.combining(character)
    )
    return {token for token in re.findall(r"[a-z0-9]+", normalized) if len(token) > 2}
