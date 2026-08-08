from __future__ import annotations

import time

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject, QTimer, pyqtSignal

from .revisions import ResourceRevisionIndex, layer_uri
from .serialize import layer_summary


class _SignalRelay(QObject):
    def __init__(self, callback, parent):
        super().__init__(parent)
        self.callback = callback

    def forward(self, *args):
        if self.callback is not None:
            self.callback(*args)

    def close(self):
        self.callback = None
        self.deleteLater()


class StateTracker(QObject):
    changed = pyqtSignal(dict)

    def __init__(self, iface, log):
        super().__init__()
        self.iface = iface
        self.log = log
        self.revision = 0
        self.resources = ResourceRevisionIndex()
        self.started_at = time.time()
        self._changes = []
        self._connections = []
        self._layer_connections = {}
        self._layer_summary_cache = {}
        self._canvas_events = set()
        self._canvas_timer = QTimer(self)
        self._canvas_timer.setSingleShot(True)
        self._canvas_timer.setInterval(100)
        self._canvas_timer.timeout.connect(self._flush_canvas_events)
        self._connect_project()

    def _connect(self, signal, callback):
        relay = _SignalRelay(callback, self)
        signal.connect(relay.forward)
        self._connections.append(relay)

    def _connect_project(self):
        project = QgsProject.instance()
        for signal_name, event in (
            ("readProject", "project.read"),
            ("cleared", "project.cleared"),
            ("fileNameChanged", "project.filename"),
            ("isDirtyChanged", "project.dirty"),
        ):
            signal = getattr(project, signal_name, None)
            if signal is not None:
                def callback(*args, _event=event):
                    self.touch(_event, args)

                self._connect(signal, callback)
        self._connect(project.layersAdded, self._layers_added)
        self._connect(project.layersRemoved, self._layers_removed)
        self._attach_layers(list(project.mapLayers().values()))
        canvas = self.iface.mapCanvas()
        for signal_name, event in (
            ("extentsChanged", "canvas.extent"),
            ("scaleChanged", "canvas.scale"),
            ("rotationChanged", "canvas.rotation"),
        ):
            signal = getattr(canvas, signal_name, None)
            if signal is not None:
                def callback(*args, _event=event):
                    self._debounce_canvas_event(_event)

                self._connect(signal, callback)
        for signal_name, event in (
            ("destinationCrsChanged", "canvas.crs"),
            ("mapToolSet", "canvas.tool"),
        ):
            signal = getattr(canvas, signal_name, None)
            if signal is not None:
                def callback(*args, _event=event):
                    self.touch(_event, None)

                self._connect(signal, callback)
        current_layer_changed = getattr(self.iface, "currentLayerChanged", None)
        if current_layer_changed is not None:
            self._connect(
                current_layer_changed,
                lambda layer: self.touch(
                    "active_layer.changed",
                    {"layer_id": layer.id() if layer else None},
                ),
            )

    def _attach_layers(self, layers):
        for layer in layers:
            if layer.id() in self._layer_connections:
                continue
            connections = []
            for signal_name, event in (
                ("selectionChanged", "layer.selection"),
                ("editingStarted", "layer.editing_started"),
                ("editingStopped", "layer.editing_stopped"),
                ("dataChanged", "layer.data"),
                ("styleChanged", "layer.style"),
                ("nameChanged", "layer.name"),
            ):
                signal = getattr(layer, signal_name, None)
                if signal is not None:
                    def callback(*args, _event=event, _id=layer.id()):
                        self.touch(_event, {"layer_id": _id})

                    relay = _SignalRelay(callback, self)
                    signal.connect(relay.forward)
                    connections.append(relay)
            self._layer_connections[layer.id()] = connections

    def _layers_added(self, layers):
        layer_ids = [layer.id() for layer in layers]
        self.touch("layers.added", {"layer_ids": layer_ids})
        self._attach_layers(layers)

    def _layers_removed(self, layer_ids):
        normalized = [str(layer_id) for layer_id in layer_ids]
        self.touch("layers.removed", {"layer_ids": normalized})
        self._detach_layers(normalized)

    def _debounce_canvas_event(self, event):
        self._canvas_events.add(str(event))
        self._canvas_timer.start()

    def _flush_canvas_events(self):
        if not self._canvas_events:
            return
        events = sorted(self._canvas_events)
        self._canvas_events.clear()
        self.touch("canvas.view", {"events": events})

    def _detach_layers(self, layer_ids):
        for layer_id in layer_ids:
            for relay in self._layer_connections.pop(str(layer_id), []):
                relay.close()

    def touch(self, event, data=None):
        self.revision += 1
        compact_data = _compact(data)
        if event in {"project.read", "project.cleared"}:
            self._layer_summary_cache.clear()
        elif isinstance(compact_data, dict):
            changed_layer_ids = list(compact_data.get("layer_ids") or [])
            layer_id = compact_data.get("layer_id") or compact_data.get("layer")
            if layer_id is not None:
                changed_layer_ids.append(layer_id)
            for changed_layer_id in changed_layer_ids:
                for detail in ("summary", "standard", "full"):
                    self._layer_summary_cache.pop(
                        (str(changed_layer_id), detail), None
                    )
        affected = self.resources.affected(event, compact_data)
        self.resources.bump(affected, self.revision)
        change = {
            "revision": self.revision,
            "time": time.time(),
            "event": event,
            "data": compact_data,
            "resources": {
                uri: self.resources.revision(uri) for uri in affected
            },
        }
        self._changes.append(change)
        if len(self._changes) > 1000:
            self._changes = self._changes[-1000:]
        self.changed.emit(change)

    def changes_since(self, revision):
        if revision is None:
            return []
        return [item for item in self._changes if item["revision"] > revision]

    def snapshot(self, detail="summary", since_revision=None):
        project = QgsProject.instance()
        canvas = self.iface.mapCanvas()
        project_layers = list(project.mapLayers().values())
        changes = self.changes_since(since_revision)
        incremental = since_revision is not None
        base_revision = max(0, int(since_revision or 0))
        if incremental and base_revision > 0:
            visible_layers = [
                layer
                for layer in project_layers
                if self.resources.revision(layer_uri(layer.id())) > base_revision
            ]
        else:
            visible_layers = project_layers
        layers = [self._cached_layer_summary(layer, detail) for layer in visible_layers]
        removed_layer_ids = sorted(
            {
                str(layer_id)
                for change in changes
                if change["event"] == "layers.removed"
                for layer_id in (change.get("data") or {}).get("layer_ids", [])
            }
        )
        active = self.iface.activeLayer()
        all_resource_revisions = self.resources.snapshot()
        resource_revisions = (
            {
                uri: revision
                for uri, revision in all_resource_revisions.items()
                if revision > base_revision
            }
            if incremental and base_revision > 0
            else all_resource_revisions
        )
        oldest_available_revision = (
            self._changes[0]["revision"] if self._changes else self.revision
        )
        delta_complete = (
            not incremental
            or base_revision == 0
            or base_revision >= oldest_available_revision - 1
        )
        result = {
            "revision": self.revision,
            "base_revision": base_revision if incremental else None,
            "incremental": incremental,
            "resource_revisions": resource_revisions,
            "resource_revisions_complete": not incremental or base_revision == 0,
            "oldest_available_revision": oldest_available_revision,
            "delta_complete": delta_complete,
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "project": {
                "title": project.title(),
                "file": project.fileName(),
                "dirty": project.isDirty(),
                "crs": project.crs().authid() if project.crs().isValid() else None,
                "layer_count": len(project_layers),
            },
            "active_layer_id": active.id() if active else None,
            "canvas": {
                "extent": {
                    "xmin": canvas.extent().xMinimum(),
                    "ymin": canvas.extent().yMinimum(),
                    "xmax": canvas.extent().xMaximum(),
                    "ymax": canvas.extent().yMaximum(),
                },
                "scale": canvas.scale(),
                "rotation": canvas.rotation(),
                "crs": canvas.mapSettings().destinationCrs().authid(),
                "rendering": canvas.isDrawing(),
            },
            "layers": layers,
            "removed_layer_ids": removed_layer_ids,
            "unchanged_layer_count": len(project_layers) - len(visible_layers),
            "changes": changes,
        }
        if detail != "summary":
            result["visible_layer_ids"] = [
                layer.id() for layer in canvas.layers()
            ]
        return result

    def _cached_layer_summary(self, layer, detail):
        normalized_detail = str(detail)
        key = (layer.id(), normalized_detail)
        revision = self.resources.revision(layer_uri(layer.id()))
        cached = self._layer_summary_cache.get(key)
        if cached is not None and cached[0] == revision:
            return cached[1]
        summary = layer_summary(layer, detail=normalized_detail)
        self._layer_summary_cache[key] = (revision, summary)
        return summary

    def resource_revision(self, uri):
        return self.resources.revision(uri)

    def close(self):
        self._canvas_timer.stop()
        for relay in self._connections:
            relay.close()
        for connections in self._layer_connections.values():
            for relay in connections:
                relay.close()
        self._connections.clear()
        self._layer_connections.clear()


def _compact(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_compact(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key): _compact(item) for key, item in value.items()}
    if hasattr(value, "id"):
        try:
            return {"id": value.id(), "name": value.name()}
        except Exception:
            return repr(value)[:500]
    return repr(value)[:500]
