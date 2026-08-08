from __future__ import annotations

import re
from typing import Any

_VARIABLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def validate_variable(name: str) -> str:
    value = str(name)
    if _VARIABLE.fullmatch(value) is None:
        raise ValueError("Pipeline variable names must match {}".format(_VARIABLE.pattern))
    return value


def resolve_references(value: Any, variables: dict[str, Any]) -> Any:
    """Resolve explicit ``{"$ref": "name#/json/pointer"}`` values only."""

    if isinstance(value, list):
        return [resolve_references(item, variables) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"$ref"}:
        reference = str(value["$ref"])
        name, separator, pointer = reference.partition("#")
        if name not in variables:
            raise ValueError("Unknown pipeline reference: {}".format(name))
        return select_value(variables[name], pointer if separator else "")
    return {key: resolve_references(item, variables) for key, item in value.items()}


def select_value(value: Any, pointer: str | None) -> Any:
    if pointer in {None, "", "#"}:
        return value
    path = str(pointer)
    if path.startswith("#"):
        path = path[1:]
    if not path.startswith("/"):
        raise ValueError("select must be an RFC 6901 JSON Pointer")
    current = value
    for raw_part in path[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                raise ValueError("JSON Pointer does not exist: {}".format(pointer))
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ValueError("JSON Pointer does not exist: {}".format(pointer)) from exc
        else:
            raise ValueError("JSON Pointer crosses a scalar: {}".format(pointer))
    return current


def capture_value(value: Any, mode: str, handles: Any) -> tuple[bool, Any]:
    capture = str(mode or "full")
    if capture == "none":
        return False, None
    if capture == "summary":
        return True, summarize(value)
    if capture == "handle":
        return True, handles.put(value, kind="pipeline_result")
    if capture != "full":
        raise ValueError("capture must be full, summary, handle, or none")
    return True, value


def summarize(value: Any, depth: int = 0) -> Any:
    if depth >= 2:
        if isinstance(value, dict):
            return {"type": "object", "keys": list(value)[:20], "key_count": len(value)}
        if isinstance(value, list):
            return {"type": "array", "item_count": len(value)}
        if isinstance(value, str) and len(value) > 160:
            return value[:157] + "..."
        return value
    if isinstance(value, dict):
        return {
            key: summarize(item, depth + 1)
            for key, item in list(value.items())[:20]
        }
    if isinstance(value, list):
        return {
            "item_count": len(value),
            "sample": [summarize(item, depth + 1) for item in value[:3]],
        }
    if isinstance(value, str) and len(value) > 320:
        return value[:317] + "..."
    return value
