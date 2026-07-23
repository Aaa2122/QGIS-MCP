from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
import traceback
from urllib.parse import quote, unquote, urlparse

from qgis.PyQt.QtCore import QBuffer, QIODevice, Qt
from qgis.PyQt.QtWidgets import (
    QAction,
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QMainWindow,
    QSpinBox,
    QDoubleSpinBox,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsExpression,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from .capabilities import CapabilityIndex, ObjectRegistry
from .operations import OperationManager
from .serialize import (
    feature_summary,
    field_schema,
    json_safe,
    layer_summary,
    renderer_summary,
)
from .state import StateTracker
from .store import EventLog, HandleStore

MAX_INLINE_RESULT_BYTES = 1024 * 1024


class DispatchError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Dispatcher:
    def __init__(self, iface):
        self.iface = iface
        self.log = EventLog()
        self.handles = HandleStore()
        self.state = StateTracker(iface, self.log)
        self.objects = ObjectRegistry(iface)
        self.capabilities = CapabilityIndex(iface, self.objects)
        self.operations = OperationManager(self.log, self.state)
        self.python_enabled = _truthy(os.environ.get("QGIS_MCP_ENABLE_PYTHON"))
        self._python_globals = {
            "__builtins__": __builtins__,
            "iface": iface,
            "project": QgsProject.instance(),
        }
        self._methods = {
            "session.snapshot": self.session_snapshot,
            "project.inspect": self.project_inspect,
            "project.action": self.project_action,
            "layer.inspect": self.layer_inspect,
            "feature.query": self.feature_query,
            "selection.set": self.selection_set,
            "vector.edit": self.vector_edit,
            "capabilities.search": self.capabilities_search,
            "capabilities.describe": self.capabilities_describe,
            "capabilities.invoke": self.capabilities_invoke,
            "processing.start": self.processing_start,
            "operation.control": self.operation_control,
            "python.exec": self.python_exec,
            "ui.search": self.ui_search,
            "ui.invoke": self.ui_invoke,
            "ui.screenshot": self.ui_screenshot,
            "logs.read": self.logs_read,
            "handle.read": self.handle_read,
            "batch.execute": self.batch_execute,
            "resources.list": self.resources_list,
            "resources.read": self.resources_read,
        }
        QgsApplicationMessageLog.connect(self.log)

    def close(self):
        self.state.close()
        QgsApplicationMessageLog.disconnect(self.log)

    def dispatch(self, method, params):
        handler = self._methods.get(method)
        if handler is None:
            raise DispatchError(-32601, "Unknown QGIS bridge method: {}".format(method))
        if not isinstance(params, dict):
            raise DispatchError(-32602, "Parameters must be an object")
        try:
            result = handler(**params)
            if method not in {"handle.read", "ui.screenshot"}:
                encoded_size = len(
                    json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
                )
                if encoded_size > MAX_INLINE_RESULT_BYTES:
                    descriptor = self.handles.put(
                        result,
                        kind="large_result",
                        metadata={"method": method, "estimated_bytes": encoded_size},
                    )
                    return {
                        **descriptor,
                        "truncated": True,
                        "message": (
                            "Result retained inside QGIS because it exceeds the inline "
                            "payload limit; page it with qgis_handle_read."
                        ),
                    }
            return result
        except DispatchError:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise DispatchError(
                -32602,
                str(exc).strip("'"),
                {"method": method, "exception": type(exc).__name__},
            ) from exc
        except Exception as exc:
            stack = traceback.format_exc()
            self.log.add("error", str(exc), "error", {"method": method, "traceback": stack})
            raise DispatchError(
                -32010,
                "QGIS operation failed: {}".format(exc),
                {"method": method, "exception": type(exc).__name__, "traceback": stack},
            ) from exc

    def session_snapshot(self, detail="standard", since_revision=None):
        if detail not in {"summary", "standard", "full"}:
            raise ValueError("detail must be summary, standard, or full")
        result = self.state.snapshot(detail, since_revision)
        if detail != "summary":
            result["capabilities"] = self.capabilities.summary()
            result["operations"] = self.operations.list_public()
            result["python_execution_enabled"] = self.python_enabled
            result["open_windows"] = [
                self.objects.summarize(runtime_id, obj)
                for runtime_id, obj in self.objects.refresh().items()
                if isinstance(obj, QMainWindow) and obj.isVisible()
            ]
        return result

    def project_inspect(self, section="project"):
        project = QgsProject.instance()
        if section == "project":
            return {
                **self.state.snapshot("summary")["project"],
                "home_path": project.homePath(),
                "absolute_path": project.absolutePath(),
                "ellipsoid": project.ellipsoid(),
                "distance_units": int(project.distanceUnits()),
                "area_units": int(project.areaUnits()),
                "metadata": json_safe(project.metadata()),
                "custom_variables": json_safe(project.customVariables()),
            }
        if section == "layer_tree":
            return _tree_node(project.layerTreeRoot())
        if section == "variables":
            return json_safe(project.customVariables())
        if section == "relations":
            return [
                {
                    "id": relation.id(),
                    "name": relation.name(),
                    "valid": relation.isValid(),
                    "referenced_layer": relation.referencedLayerId(),
                    "referencing_layer": relation.referencingLayerId(),
                    "field_pairs": json_safe(relation.fieldPairs()),
                }
                for relation in project.relationManager().relations().values()
            ]
        if section == "layouts":
            return [
                {
                    "name": layout.name(),
                    "type": type(layout).__name__,
                    "item_count": len(layout.items()),
                }
                for layout in project.layoutManager().layouts()
            ]
        raise ValueError("Unknown project section")

    def project_action(
        self,
        action,
        layer=None,
        source=None,
        name=None,
        provider=None,
        path=None,
    ):
        project = QgsProject.instance()
        result = None
        if action == "save":
            success = project.write(path) if path else project.write()
            if not success:
                raise RuntimeError("QGIS could not save the project")
            result = {"file": project.fileName(), "saved": True}
        elif action in {"add_vector", "add_raster"}:
            if not source:
                raise ValueError("source is required")
            layer_name = name or os.path.basename(source) or "Layer"
            if action == "add_vector":
                created = QgsVectorLayer(source, layer_name, provider or "ogr")
            else:
                created = QgsRasterLayer(source, layer_name, provider or "gdal")
            if not created.isValid():
                raise ValueError("QGIS could not create a valid layer from the source")
            project.addMapLayer(created)
            result = layer_summary(created)
        elif action == "remove_layer":
            target = self._layer(layer)
            result = layer_summary(target)
            project.removeMapLayer(target.id())
        elif action == "set_active_layer":
            target = self._layer(layer)
            self.iface.setActiveLayer(target)
            result = layer_summary(target)
        elif action == "zoom_layer":
            target = self._layer(layer)
            self.iface.mapCanvas().setExtent(target.extent())
            self.iface.mapCanvas().refresh()
            result = layer_summary(target)
        elif action == "refresh":
            if layer:
                target = self._layer(layer)
                target.reload()
                target.triggerRepaint()
                result = layer_summary(target)
            else:
                self.iface.mapCanvas().refresh()
                result = {"canvas_refreshed": True}
        elif action == "load_style":
            target = self._layer(layer)
            if not path:
                raise ValueError("path is required")
            message, success = target.loadNamedStyle(path)
            if not success:
                raise ValueError("Could not load style: {}".format(message))
            target.triggerRepaint()
            result = {"layer_id": target.id(), "style_path": path, "message": message}
        else:
            raise ValueError("Unknown project action")
        self.state.touch("project.action", {"action": action, "layer": layer})
        return result

    def layer_inspect(self, layer, include=None, sample_limit=5):
        target = self._layer(layer)
        wanted = set(include or ("metadata", "schema", "style", "selection"))
        result = {"summary": layer_summary(target)}
        if "metadata" in wanted:
            result["metadata"] = {
                "abstract": target.abstract(),
                "title": target.title(),
                "attribution": target.attribution(),
                "keywords": json_safe(target.keywordList()),
                "metadata": json_safe(target.metadata()),
            }
        if "schema" in wanted and isinstance(target, QgsVectorLayer):
            result["schema"] = [field_schema(field) for field in target.fields()]
            result["primary_key_fields"] = target.primaryKeyAttributes()
        if "style" in wanted:
            result["style"] = renderer_summary(target)
        if "selection" in wanted and isinstance(target, QgsVectorLayer):
            ids = list(target.selectedFeatureIds())
            result["selection"] = {
                "count": len(ids),
                "feature_ids": ids[:1000],
                "truncated": len(ids) > 1000,
            }
        if "sample" in wanted and isinstance(target, QgsVectorLayer) and sample_limit:
            result["sample"] = self.feature_query(
                target.id(), limit=min(int(sample_limit), 100)
            )
        return result

    def feature_query(
        self,
        layer,
        expression=None,
        fields=None,
        selected_only=False,
        include_geometry=False,
        limit=100,
        offset=0,
    ):
        target = self._vector_layer(layer)
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        field_names = list(fields) if fields else [field.name() for field in target.fields()]
        unknown = [name for name in field_names if target.fields().indexOf(name) < 0]
        if unknown:
            raise ValueError("Unknown fields: {}".format(", ".join(unknown)))
        request = QgsFeatureRequest()
        if expression:
            parsed = QgsExpression(expression)
            if parsed.hasParserError():
                raise ValueError("Invalid QGIS expression: {}".format(parsed.parserErrorString()))
            request.setFilterExpression(expression)
        if selected_only:
            request.setFilterFids(target.selectedFeatureIds())
        request.setSubsetOfAttributes(field_names, target.fields())
        if not include_geometry:
            request.setFlags(QgsFeatureRequest.NoGeometry)
        if hasattr(request, "setOffset"):
            request.setLimit(limit + 1)
            request.setOffset(offset)
            iterator = target.getFeatures(request)
        else:
            request.setLimit(offset + limit + 1)
            iterator = target.getFeatures(request)
            for _ in range(offset):
                next(iterator, None)
        selected_fields = [target.fields().field(name) for name in field_names]
        items = []
        for feature in iterator:
            items.append(feature_summary(feature, selected_fields, include_geometry))
            if len(items) > limit:
                break
        has_more = len(items) > limit
        return {
            "layer_id": target.id(),
            "offset": offset,
            "limit": limit,
            "items": items[:limit],
            "has_more": has_more,
            "feature_count": target.featureCount(),
        }

    def selection_set(
        self, layer, mode="replace", feature_ids=None, expression=None
    ):
        target = self._vector_layer(layer)
        if mode == "clear":
            target.removeSelection()
        else:
            behavior = {
                "replace": QgsVectorLayer.SetSelection,
                "add": QgsVectorLayer.AddToSelection,
                "remove": QgsVectorLayer.RemoveFromSelection,
            }.get(mode)
            if behavior is None:
                raise ValueError("Unknown selection mode")
            if expression is not None:
                parsed = QgsExpression(expression)
                if parsed.hasParserError():
                    raise ValueError(
                        "Invalid QGIS expression: {}".format(parsed.parserErrorString())
                    )
                target.selectByExpression(expression, behavior)
            elif feature_ids is not None:
                target.selectByIds([int(item) for item in feature_ids], behavior)
            else:
                raise ValueError("feature_ids or expression is required")
        self.state.touch("selection.set", {"layer_id": target.id(), "mode": mode})
        return {
            "layer_id": target.id(),
            "selected_count": target.selectedFeatureCount(),
            "feature_ids": list(target.selectedFeatureIds())[:1000],
        }

    def vector_edit(
        self,
        layer,
        action,
        features=None,
        feature_ids=None,
        auto_start=True,
    ):
        target = self._vector_layer(layer)
        if action == "start":
            if not target.isEditable() and not target.startEditing():
                raise RuntimeError("QGIS could not start an edit session")
        elif action in {"add", "update", "delete"}:
            if not target.isEditable():
                if not auto_start or not target.startEditing():
                    raise RuntimeError("Layer is not editable")
            target.beginEditCommand("QGIS MCP {}".format(action))
            try:
                if action == "add":
                    added = []
                    for item in (features or [])[:1000]:
                        feature = QgsFeature(target.fields())
                        for field_name, value in (item.get("attributes") or {}).items():
                            if target.fields().indexOf(field_name) < 0:
                                raise ValueError("Unknown field: {}".format(field_name))
                            feature[field_name] = value
                        if item.get("geometry_wkt"):
                            geometry = QgsGeometry.fromWkt(item["geometry_wkt"])
                            if geometry.isNull():
                                raise ValueError("Invalid geometry WKT")
                            feature.setGeometry(geometry)
                        if not target.addFeature(feature):
                            raise RuntimeError("Provider rejected a new feature")
                        added.append(feature.id())
                    result = {"added_feature_ids": added}
                elif action == "update":
                    updated = []
                    for item in (features or [])[:1000]:
                        if "id" not in item:
                            raise ValueError("Each updated feature requires an id")
                        feature_id = int(item["id"])
                        changes = {}
                        for field_name, value in (item.get("attributes") or {}).items():
                            field_index = target.fields().indexOf(field_name)
                            if field_index < 0:
                                raise ValueError("Unknown field: {}".format(field_name))
                            changes[field_index] = value
                        if changes and not target.changeAttributeValues(feature_id, changes):
                            raise RuntimeError(
                                "Could not update feature {}".format(feature_id)
                            )
                        if item.get("geometry_wkt"):
                            geometry = QgsGeometry.fromWkt(item["geometry_wkt"])
                            if geometry.isNull() or not target.changeGeometry(
                                feature_id, geometry
                            ):
                                raise RuntimeError(
                                    "Could not update geometry for feature {}".format(
                                        feature_id
                                    )
                                )
                        updated.append(feature_id)
                    result = {"updated_feature_ids": updated}
                else:
                    ids = [int(item) for item in (feature_ids or [])[:1000]]
                    if not target.deleteFeatures(ids):
                        raise RuntimeError("Could not delete requested features")
                    result = {"deleted_feature_ids": ids}
                target.endEditCommand()
            except Exception:
                target.destroyEditCommand()
                raise
        elif action == "commit":
            if not target.isEditable():
                raise ValueError("Layer has no active edit session")
            if not target.commitChanges():
                errors = target.commitErrors()
                raise RuntimeError("Commit failed: {}".format("; ".join(errors)))
            result = {"committed": True}
        elif action == "rollback":
            if not target.isEditable():
                raise ValueError("Layer has no active edit session")
            if not target.rollBack():
                raise RuntimeError("Rollback failed")
            result = {"rolled_back": True}
        else:
            raise ValueError("Unknown vector edit action")
        self.state.touch(
            "vector.edit",
            {"layer_id": target.id(), "action": action},
        )
        return {
            "layer_id": target.id(),
            "editable": target.isEditable(),
            "modified": target.isModified(),
            **(result or {}),
        }

    def capabilities_search(self, query="", kinds=None, limit=30):
        return self.capabilities.search(query, kinds, int(limit))

    def capabilities_describe(self, kind, id):
        return self.capabilities.describe(kind, id)

    def capabilities_invoke(
        self, kind, target, member, args=None, kwargs=None
    ):
        result = self.capabilities.invoke(kind, target, member, args, kwargs)
        self.state.touch(
            "capability.invoked",
            {"kind": kind, "target": target, "member": member},
        )
        return {"result": json_safe(result)}

    def processing_start(self, algorithm, parameters):
        return self.operations.start_processing(algorithm, parameters)

    def operation_control(self, operation_id, action="status"):
        return self.operations.control(operation_id, action)

    def python_exec(
        self, code, mode="exec", result_expression=None, timeout_ms=30000
    ):
        if not self.python_enabled:
            raise DispatchError(
                -32020,
                "Python execution is disabled",
                {
                    "enable": (
                        "Set QGIS_MCP_ENABLE_PYTHON=1 before starting QGIS. "
                        "This grants arbitrary code execution in the QGIS process."
                    )
                },
            )
        if mode not in {"eval", "exec"}:
            raise ValueError("mode must be eval or exec")
        started = time.monotonic()
        deadline = started + int(timeout_ms) / 1000.0
        stdout, stderr = io.StringIO(), io.StringIO()
        previous_trace = sys.gettrace()

        def deadline_trace(frame, event, arg):
            if time.monotonic() > deadline:
                raise TimeoutError("Python execution exceeded timeout_ms")
            return deadline_trace

        result = None
        try:
            sys.settrace(deadline_trace)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if mode == "eval":
                    result = eval(code, self._python_globals, self._python_globals)
                else:
                    exec(code, self._python_globals, self._python_globals)
                    if result_expression:
                        result = eval(
                            result_expression, self._python_globals, self._python_globals
                        )
        finally:
            sys.settrace(previous_trace)
        self.state.touch("python.executed", {"mode": mode})
        return {
            "result": json_safe(result),
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "note": "The timeout can interrupt Python bytecode, not blocking C++/provider calls.",
        }

    def ui_search(self, query="", types=None, visible_only=True, limit=50):
        needle = query.casefold().strip()
        wanted = {item.casefold() for item in types} if types else None
        values = []
        for runtime_id, obj in self.objects.refresh().items():
            item = self.objects.summarize(runtime_id, obj)
            haystack = " ".join(str(value) for value in item.values()).casefold()
            if needle and needle not in haystack:
                continue
            if wanted and item["class"].casefold() not in wanted and item["kind"] not in wanted:
                continue
            if visible_only and item.get("visible") is False and item["kind"] != "action":
                continue
            values.append(item)
        return {"results": values[: int(limit)], "truncated": len(values) > int(limit)}

    def ui_invoke(self, target, action, value=None):
        obj = self.objects.get(target)
        if action == "trigger" and isinstance(obj, QAction):
            obj.trigger()
        elif action == "click" and isinstance(obj, QAbstractButton):
            obj.click()
        elif action == "set_text" and isinstance(obj, (QLineEdit, QComboBox)):
            obj.setText(str(value)) if isinstance(obj, QLineEdit) else obj.setCurrentText(str(value))
        elif action == "set_value" and isinstance(obj, (QSpinBox, QDoubleSpinBox)):
            obj.setValue(value)
        elif action == "set_checked" and isinstance(obj, (QAction, QAbstractButton)):
            obj.setChecked(bool(value))
        elif action == "show" and isinstance(obj, QWidget):
            obj.show()
            obj.raise_()
            obj.activateWindow()
        elif action == "close" and isinstance(obj, QWidget):
            obj.close()
        else:
            raise ValueError(
                "Action {} is not supported by {}".format(action, type(obj).__name__)
            )
        self.state.touch("ui.invoked", {"target": target, "action": action})
        return self.objects.summarize(target, obj)

    def ui_screenshot(self, target="canvas", max_width=1600):
        if target == "canvas":
            widget = self.iface.mapCanvas()
        elif target in {"main", "window"}:
            widget = self.iface.mainWindow()
        else:
            widget = self.objects.get(target)
            if not isinstance(widget, QWidget):
                raise ValueError("Screenshot target must be a QWidget")
        pixmap = widget.grab()
        if pixmap.width() > int(max_width):
            pixmap = pixmap.scaledToWidth(int(max_width), Qt.SmoothTransformation)
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            raise RuntimeError("Could not encode screenshot")
        import base64

        return {
            "data": base64.b64encode(bytes(buffer.data())).decode("ascii"),
            "mime_type": "image/png",
            "width": pixmap.width(),
            "height": pixmap.height(),
            "target": target,
            "revision": self.state.revision,
        }

    def logs_read(self, after=0, level=None, limit=100):
        return self.log.read(int(after), level, int(limit))

    def handle_read(self, handle, offset=0, limit=100):
        return self.handles.read(handle, int(offset), int(limit))

    def batch_execute(self, calls, continue_on_error=False):
        results = []
        for index, call in enumerate(calls[:100]):
            method = call.get("method")
            if method == "batch.execute":
                raise ValueError("Nested batches are not supported")
            try:
                results.append(
                    {
                        "index": index,
                        "result": self.dispatch(method, call.get("params") or {}),
                    }
                )
            except DispatchError as exc:
                results.append(
                    {
                        "index": index,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "data": exc.data,
                        },
                    }
                )
                if not continue_on_error:
                    break
        return {"results": results, "completed": len(results), "requested": len(calls)}

    def resources_list(self):
        resources = []
        for layer in QgsProject.instance().mapLayers().values():
            resources.extend(
                [
                    {
                        "uri": "qgis://layers/{}".format(quote(layer.id(), safe="")),
                        "name": "Layer: {}".format(layer.name()),
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "qgis://layers/{}/schema".format(quote(layer.id(), safe="")),
                        "name": "Layer schema: {}".format(layer.name()),
                        "mimeType": "application/json",
                    },
                ]
            )
        for operation in self.operations.list_public():
            resources.append(
                {
                    "uri": "qgis://operations/{}".format(operation["id"]),
                    "name": "Operation: {}".format(operation["id"]),
                    "mimeType": "application/json",
                }
            )
        return resources

    def resources_read(self, uri):
        parsed = urlparse(uri)
        if parsed.scheme != "qgis":
            raise ValueError("Unsupported resource URI")
        path = [unquote(item) for item in parsed.path.strip("/").split("/") if item]
        root = parsed.netloc
        if root == "session":
            return self.session_snapshot()
        if root == "project":
            return self.project_inspect("project")
        if root == "capabilities":
            return self.capabilities.summary()
        if root == "logs":
            return self.logs_read()
        if root == "layers" and path:
            include = ["schema"] if len(path) > 1 and path[1] == "schema" else None
            return self.layer_inspect(path[0], include=include)
        if root == "operations" and path:
            return self.operation_control(path[0])
        raise ValueError("Unknown QGIS resource URI")

    def _layer(self, identifier):
        project = QgsProject.instance()
        layer = project.mapLayer(str(identifier))
        if layer is not None:
            return layer
        matches = project.mapLayersByName(str(identifier))
        if not matches:
            raise KeyError("Layer not found: {}".format(identifier))
        if len(matches) > 1:
            raise ValueError("Layer name is ambiguous; use the layer ID")
        return matches[0]

    def _vector_layer(self, identifier):
        layer = self._layer(identifier)
        if not isinstance(layer, QgsVectorLayer):
            raise ValueError("Layer is not a vector layer")
        return layer


def _tree_node(node):
    value = {
        "name": node.name(),
        "visible": node.isVisible(),
        "expanded": node.isExpanded(),
    }
    if hasattr(node, "layerId"):
        value["kind"] = "layer"
        value["layer_id"] = node.layerId()
    else:
        value["kind"] = "group"
        value["children"] = [_tree_node(child) for child in node.children()]
    return value


def _truthy(value):
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


class QgsApplicationMessageLog:
    _signal = None
    _callback = None

    @classmethod
    def connect(cls, log):
        from qgis.core import QgsApplication

        cls._signal = QgsApplication.messageLog().messageReceived

        def callback(message, tag, level):
            level_name = {
                Qgis.Info: "info",
                Qgis.Warning: "warning",
                Qgis.Critical: "error",
                Qgis.Success: "info",
            }.get(level, "info")
            log.add("qgis.message", message, level_name, {"tag": tag})

        cls._callback = callback
        cls._signal.connect(callback)

    @classmethod
    def disconnect(cls, log):
        if cls._signal is not None and cls._callback is not None:
            try:
                cls._signal.disconnect(cls._callback)
            except Exception:
                pass
        cls._signal = None
        cls._callback = None
