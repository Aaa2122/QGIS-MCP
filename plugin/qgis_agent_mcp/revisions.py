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
        operation_id = data.get("id") if event.startswith("operation.") else None

        if event.startswith(("project.", "layers.")):
            uris.extend((PROJECT_URI, LAYER_TREE_URI))
        if event.startswith("canvas.") or event == "active_layer.changed":
            uris.append(PROJECT_URI)
        if event.startswith(("layer.", "selection.", "vector.")) and layer_id:
            uris.append(layer_uri(layer_id))
            if event in {"layer.data", "layer.name", "vector.edit"}:
                uris.append(layer_uri(layer_id, "schema"))
            if event in {"layer.selection", "selection.set"}:
                uris.append(layer_uri(layer_id, "selection"))
        if operation_id:
            uris.append(operation_uri(operation_id))
        if event.startswith("capability."):
            uris.append(CAPABILITIES_URI)
        if event.startswith(("bridge.", "log.")):
            uris.append(LOGS_URI)
        return list(dict.fromkeys(uris))

