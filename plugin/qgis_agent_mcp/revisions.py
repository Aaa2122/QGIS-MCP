from __future__ import annotations

from urllib.parse import quote

SESSION_URI = "qgis://session"
PROJECT_URI = "qgis://project"
LAYER_TREE_URI = "qgis://project/layer-tree"
CAPABILITIES_URI = "qgis://capabilities"
LOGS_URI = "qgis://logs"


def layer_uri(layer_id, suffix=None):
    uri = "qgis://layers/{}".format(quote(str(layer_id), safe=""))
    return "{}/{}".format(uri, suffix) if suffix else uri


def operation_uri(operation_id):
    return "qgis://operations/{}".format(quote(str(operation_id), safe=""))


class ResourceRevisionIndex:
    """Tracks independent monotonic revisions for canonical MCP resources."""

    def __init__(self):
        self._revisions = {}

    def revision(self, uri):
        return self._revisions.get(uri, 0)

    def snapshot(self):
        return dict(sorted(self._revisions.items()))

    def bump(self, uris, revision):
        changed = []
        for uri in dict.fromkeys(uris):
            if self._revisions.get(uri) != revision:
                self._revisions[uri] = revision
                changed.append(uri)
        return changed

    def affected(self, event, data=None):
        data = data if isinstance(data, dict) else {}
        uris = [SESSION_URI]
        layer_id = data.get("layer_id") or data.get("layer")
        layer_ids = [str(item) for item in (data.get("layer_ids") or [])]
        if layer_id is not None:
            layer_ids.append(str(layer_id))
        operation_id = data.get("id") if event.startswith("operation.") else None

        if event.startswith(("project.", "layers.")):
            uris.extend((PROJECT_URI, LAYER_TREE_URI))
        if event.startswith("canvas.") or event == "active_layer.changed":
            uris.append(PROJECT_URI)
        for current_layer_id in dict.fromkeys(layer_ids):
            uris.append(layer_uri(current_layer_id))
            if event in {
                "layer.data",
                "layer.name",
                "vector.edit",
                "vector.schema",
            }:
                uris.append(layer_uri(current_layer_id, "schema"))
            if event in {"layer.selection", "selection.set", "selection.advanced"}:
                uris.append(layer_uri(current_layer_id, "selection"))
            if any(
                token in event
                for token in ("style", "renderer", "symbol", "label")
            ):
                uris.append(layer_uri(current_layer_id, "style"))
            if event in {"layer.data", "vector.edit", "vector.geometry"}:
                uris.append(layer_uri(current_layer_id, "data"))
            if any(
                token in event
                for token in ("name", "source", "properties", "temporal", "elevation")
            ):
                uris.append(layer_uri(current_layer_id, "metadata"))
        if operation_id:
            uris.append(operation_uri(operation_id))
        if event.startswith("capability."):
            uris.append(CAPABILITIES_URI)
        if event.startswith(("bridge.", "log.")):
            uris.append(LOGS_URI)
        return list(dict.fromkeys(uris))

