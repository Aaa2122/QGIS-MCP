from __future__ import annotations

import time

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QObject, pyqtSignal

from .revisions import ResourceRevisionIndex
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
        self._connect_project()

    def _connect(self, signal, callback):
        relay = _SignalRelay(callback, self)
        signal.connect(relay.forward)
        self._connections.append(relay)

    def _connect_project(self):
        project = QgsProject.instance()
        for signal_name, event in (
            ("layersAdded", "layers.added"),
            ("layersRemoved", "layers.removed"),
            ("layerWillBeRemoved", "layer.removing"),
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
        self._connect(project.layersAdded, self._attach_layers)
        self._connect(project.layersRemoved, self._detach_layers)
        self._attach_layers(list(project.mapLayers().values()))
        canvas = self.iface.mapCanvas()
        for signal_name, event in (
            ("extentsChanged", "canvas.extent"),
            ("scaleChanged", "canvas.scale"),
            ("rotationChanged", "canvas.rotation"),
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

    def _detach_layers(self, layer_ids):
        for layer_id in layer_ids:
            for relay in self._layer_connections.pop(str(layer_id), []):
                relay.close()

    def touch(self, event, data=None):
        self.revision += 1
        compact_data = _compact(data)
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

    def snapshot(self, detail="standard", since_revision=None):
        project = QgsProject.instance()
        canvas = self.iface.mapCanvas()
        layers = [layer_summary(layer) for layer in project.mapLayers().values()]
        active = self.iface.activeLayer()
        result = {
            "revision": self.revision,
            "resource_revisions": self.resources.snapshot(),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "project": {
                "title": project.title(),
                "file": project.fileName(),
                "dirty": project.isDirty(),
                "crs": project.crs().authid() if project.crs().isValid() else None,
                "layer_count": len(layers),
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
            "changes": self.changes_since(since_revision),
        }
        if detail != "summary":
            result["visible_layer_ids"] = [
                layer.id() for layer in canvas.layers()
            ]
        return result

    def resource_revision(self, uri):
        return self.resources.revision(uri)

    def close(self):
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
            pass
    return repr(value)[:500]
