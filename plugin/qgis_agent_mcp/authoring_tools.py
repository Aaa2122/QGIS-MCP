from __future__ import annotations

from pathlib import Path

from qgis.core import (
    Qgis,
    QgsAnnotationLayer,
    QgsAnnotationMarkerItem,
    QgsAnnotationPointTextItem,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDiagramLayerSettings,
    QgsDiagramSettings,
    QgsFeatureRequest,
    QgsHistogramDiagram,
    QgsMarkerSymbol,
    QgsPieDiagram,
    QgsPoint,
    QgsPointXY,
    QgsProject,
    QgsSingleCategoryDiagramRenderer,
    QgsStackedBarDiagram,
    QgsTextDiagram,
    QgsTextFormat,
    QgsVectorFileWriter,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QSizeF
from qgis.PyQt.QtGui import QColor

from .serialize import layer_summary

_VECTOR_DRIVERS = {
    "gpkg": "GPKG",
    "geojson": "GeoJSON",
    "shapefile": "ESRI Shapefile",
    "csv": "CSV",
    "kml": "KML",
    "gml": "GML",
    "dxf": "DXF",
    "flatgeobuf": "FlatGeobuf",
    "parquet": "Parquet",
}


class AuthoringTools:
    def __init__(self, state, layer_resolver):
        self.state = state
        self.layer_resolver = layer_resolver

    def forms(
        self,
        layer,
        action="inspect",
        layout=None,
        ui_file=None,
        field=None,
        read_only=None,
        label_on_top=None,
        reuse_last_value=None,
        suppress=None,
    ):
        target = self._vector(layer)
        config = target.editFormConfig()
        if action == "inspect":
            return self._form(target, config)
        if action == "set_layout":
            if not layout:
                raise ValueError("layout is required")
            config.setLayout(_form_layout(layout))
            if str(layout).casefold() == "ui_file":
                if not ui_file:
                    raise ValueError("ui_file is required")
                path = Path(ui_file).expanduser()
                if not path.is_file():
                    raise FileNotFoundError("UI form file not found")
                config.setUiForm(str(path))
        elif action == "configure_field":
            index = self._field(target, field)
            if read_only is not None:
                config.setReadOnly(index, bool(read_only))
            if label_on_top is not None:
                config.setLabelOnTop(index, bool(label_on_top))
            if reuse_last_value is not None:
                config.setReuseLastValue(index, bool(reuse_last_value))
        elif action == "set_suppression":
            if suppress is None:
                raise ValueError("suppress is required")
            config.setSuppress(_form_suppression(suppress))
        else:
            raise ValueError("Unknown form action")
        target.setEditFormConfig(config)
        self.state.touch("authoring.forms", {"layer_id": target.id(), "action": action})
        return self._form(target, target.editFormConfig())

    def diagrams(
        self,
        layer,
        action="inspect",
        diagram_type="pie",
        fields=None,
        colors=None,
        labels=None,
        width=15,
        height=15,
        opacity=1,
        pen_color="#404040",
        pen_width=0.3,
        placement="around_point",
        priority=5,
        obstacle=False,
    ):
        target = self._vector(layer)
        if action == "inspect":
            return self._diagram(target)
        if action == "disable":
            target.setDiagramRenderer(None)
            target.setDiagramLayerSettings(QgsDiagramLayerSettings())
        elif action == "set":
            fields = [str(item) for item in fields or []]
            if not fields:
                raise ValueError("fields is required")
            for field in fields:
                self._field(target, field)
            palette = list(colors or _palette(len(fields)))
            if len(palette) != len(fields):
                raise ValueError("colors must match fields")
            settings = QgsDiagramSettings()
            settings.enabled = True
            settings.categoryAttributes = fields
            settings.categoryColors = [QColor(str(item)) for item in palette]
            settings.categoryLabels = [str(item) for item in (labels or fields)]
            if len(settings.categoryLabels) != len(fields):
                raise ValueError("labels must match fields")
            settings.size = QSizeF(float(width), float(height))
            settings.opacity = max(0.0, min(float(opacity), 1.0))
            settings.penColor = QColor(str(pen_color))
            settings.penWidth = max(0.0, float(pen_width))
            renderer = QgsSingleCategoryDiagramRenderer()
            renderer.setDiagram(_diagram(diagram_type))
            renderer.setDiagramSettings(settings)
            layer_settings = QgsDiagramLayerSettings()
            layer_settings.setPlacement(_diagram_placement(placement))
            layer_settings.setPriority(max(0, min(int(priority), 10)))
            layer_settings.setIsObstacle(bool(obstacle))
            target.setDiagramRenderer(renderer)
            target.setDiagramLayerSettings(layer_settings)
        else:
            raise ValueError("Unknown diagram action")
        target.triggerRepaint()
        self.state.touch("authoring.diagrams", {"layer_id": target.id(), "action": action})
        return self._diagram(target)

    def annotations(
        self,
        action="list",
        layer=None,
        name=None,
        item_id=None,
        point=None,
        text=None,
        color="#e53935",
        size=4,
        font_size=10,
    ):
        if action == "create_layer":
            if not name:
                raise ValueError("name is required")
            project = QgsProject.instance()
            options = QgsAnnotationLayer.LayerOptions(project.transformContext())
            target = QgsAnnotationLayer(str(name), options)
            project.addMapLayer(target)
            self.state.touch("authoring.annotations", {"layer_id": target.id(), "action": action})
            return self._annotation_layer(target)
        target = self._annotation_layer_reference(layer)
        if action == "list":
            return self._annotation_layer(target)
        if action == "clear":
            target.clear()
        elif action == "remove":
            if not item_id or not target.removeItem(str(item_id)):
                raise KeyError("Annotation item not found")
        elif action in {"add_marker", "add_text"}:
            if not isinstance(point, list) or len(point) < 2:
                raise ValueError("point is required")
            position = QgsPointXY(float(point[0]), float(point[1]))
            if action == "add_marker":
                item = QgsAnnotationMarkerItem(QgsPoint(position))
                item.setSymbol(
                    QgsMarkerSymbol.createSimple(
                        {"color": str(color), "size": str(float(size))}
                    )
                )
            else:
                item = QgsAnnotationPointTextItem(str(text or ""), position)
                text_format = QgsTextFormat()
                text_format.setColor(QColor(str(color)))
                text_format.setSize(float(font_size))
                item.setFormat(text_format)
            item_id = target.addItem(item)
        else:
            raise ValueError("Unknown annotation action")
        target.triggerRepaint()
        self.state.touch(
            "authoring.annotations",
            {"layer_id": target.id(), "action": action, "item_id": item_id},
        )
        return self._annotation_layer(target)

    def geometry_quality(
        self,
        layer,
        action="validate",
        expression=None,
        selected_only=False,
        limit=10000,
    ):
        target = self._vector(layer)
        request = QgsFeatureRequest()
        if expression:
            request.setFilterExpression(str(expression))
        if selected_only:
            request.setFilterFids(target.selectedFeatureIds())
        limit = max(1, min(int(limit), 100000))
        issues = []
        fixes = []
        seen = {}
        if action == "repair" and not target.isEditable() and not target.startEditing():
            raise RuntimeError("Layer could not enter edit mode")
        if action not in {"validate", "repair"}:
            raise ValueError("Unknown geometry quality action")
        if action == "repair":
            target.beginEditCommand("QGIS MCP repair invalid geometries")
        try:
            for index, feature in enumerate(target.getFeatures(request)):
                if index >= limit:
                    break
                geometry = feature.geometry()
                if geometry.isNull() or geometry.isEmpty():
                    issues.append({"feature_id": feature.id(), "type": "empty_geometry"})
                    continue
                fingerprint = bytes(geometry.asWkb())
                if fingerprint in seen:
                    issues.append(
                        {
                            "feature_id": feature.id(),
                            "type": "duplicate_geometry",
                            "duplicate_of": seen[fingerprint],
                        }
                    )
                else:
                    seen[fingerprint] = feature.id()
                errors = geometry.validateGeometry()
                if errors:
                    issues.append(
                        {
                            "feature_id": feature.id(),
                            "type": "invalid_geometry",
                            "errors": [
                                {
                                    "message": item.what(),
                                    "where": [item.where().x(), item.where().y()]
                                    if item.hasWhere()
                                    else None,
                                }
                                for item in errors
                            ],
                        }
                    )
                    if action == "repair":
                        fixed = geometry.makeValid()
                        if fixed.isNull() or not target.changeGeometry(feature.id(), fixed):
                            raise RuntimeError(
                                "Could not repair feature {}".format(feature.id())
                            )
                        fixes.append(feature.id())
            if action == "repair":
                target.endEditCommand()
        except Exception:
            if action == "repair":
                target.destroyEditCommand()
            raise
        if action == "repair":
            self.state.touch(
                "authoring.geometry_quality",
                {"layer_id": target.id(), "repaired_feature_ids": fixes},
            )
        return {
            "layer_id": target.id(),
            "checked": min(index + 1 if "index" in locals() else 0, limit),
            "issue_count": len(issues),
            "issues": issues,
            "repaired_feature_ids": fixes,
            "truncated": target.featureCount() > limit,
        }

    def vector_export(
        self,
        layer,
        path,
        format="gpkg",
        layer_name=None,
        encoding="UTF-8",
        selected_only=False,
        fields=None,
        destination_crs=None,
        overwrite=False,
        create_parent=False,
        include_z=False,
        save_metadata=True,
    ):
        target = self._vector(layer)
        destination = Path(path).expanduser().resolve()
        if destination.exists() and not overwrite:
            raise FileExistsError("Destination exists; set overwrite=true")
        if not destination.parent.exists():
            if not create_parent:
                raise FileNotFoundError("Destination parent does not exist")
            destination.parent.mkdir(parents=True, exist_ok=True)
        driver = _VECTOR_DRIVERS.get(str(format).casefold())
        if driver is None:
            raise ValueError("Unsupported vector export format")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = driver
        options.fileEncoding = str(encoding)
        options.layerName = str(layer_name or target.name())
        options.onlySelectedFeatures = bool(selected_only)
        options.includeZ = bool(include_z)
        options.saveMetadata = bool(save_metadata)
        if fields is not None:
            options.attributes = [self._field(target, field) for field in fields]
        if destination_crs:
            target_crs = QgsCoordinateReferenceSystem(str(destination_crs))
            if not target_crs.isValid():
                raise ValueError("Invalid destination CRS")
            options.ct = QgsCoordinateTransform(
                target.crs(), target_crs, QgsProject.instance()
            )
        options.actionOnExistingFile = (
            QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
        )
        error, message, new_path, new_layer = QgsVectorFileWriter.writeAsVectorFormatV3(
            target,
            str(destination),
            QgsProject.instance().transformContext(),
            options,
        )
        if error != QgsVectorFileWriter.WriterError.NoError:
            raise RuntimeError("Vector export failed: {}".format(message))
        self.state.touch(
            "authoring.vector_export",
            {"layer_id": target.id(), "path": str(destination), "format": format},
        )
        return {
            "path": new_path or str(destination),
            "layer_name": new_layer or options.layerName,
            "driver": driver,
            "selected_only": bool(selected_only),
            "feature_count": target.selectedFeatureCount()
            if selected_only
            else target.featureCount(),
        }

    def _vector(self, reference):
        target = self.layer_resolver(reference)
        if not isinstance(target, QgsVectorLayer):
            raise ValueError("Vector layer is required")
        return target

    @staticmethod
    def _field(layer, reference):
        index = reference if isinstance(reference, int) else layer.fields().indexOf(str(reference or ""))
        if index < 0 or index >= len(layer.fields()):
            raise KeyError("Field not found")
        return index

    @staticmethod
    def _form(layer, config):
        return {
            "layer_id": layer.id(),
            "layout": int(config.layout()),
            "ui_file": config.uiForm(),
            "suppression": int(config.suppress()),
            "fields": [
                {
                    "name": field.name(),
                    "read_only": config.readOnly(index),
                    "label_on_top": config.labelOnTop(index),
                    "reuse_last_value": config.reuseLastValue(index),
                }
                for index, field in enumerate(layer.fields())
            ],
        }

    @staticmethod
    def _diagram(layer):
        renderer = layer.diagramRenderer()
        settings = layer.diagramLayerSettings()
        diagram_settings = renderer.diagramSettings() if renderer else []
        return {
            "layer_id": layer.id(),
            "enabled": renderer is not None,
            "renderer": renderer.rendererName() if renderer else None,
            "diagram": renderer.diagram().diagramName() if renderer and renderer.diagram() else None,
            "attributes": renderer.diagramAttributes() if renderer else [],
            "settings": [
                {
                    "enabled": item.enabled,
                    "size": [item.size.width(), item.size.height()],
                    "opacity": item.opacity,
                    "colors": [
                        color.name(QColor.NameFormat.HexArgb)
                        for color in item.categoryColors
                    ],
                    "labels": list(item.categoryLabels),
                }
                for item in diagram_settings
            ],
            "placement": int(settings.placement()) if settings else None,
            "priority": settings.priority() if settings else None,
        }

    def _annotation_layer_reference(self, reference):
        target = self.layer_resolver(reference)
        if not isinstance(target, QgsAnnotationLayer):
            raise ValueError("Annotation layer is required")
        return target

    @staticmethod
    def _annotation_layer(layer):
        raw_items = layer.items()
        items = raw_items.items() if isinstance(raw_items, dict) else []
        return {
            "layer": layer_summary(layer),
            "items": [
                {
                    "id": identifier,
                    "type": item.type(),
                    "bounds": _bounds(item.boundingBox()),
                    "text": item.text() if hasattr(item, "text") else None,
                }
                for identifier, item in items
            ],
        }


def _form_layout(value):
    mapping = {
        "auto": Qgis.AttributeFormLayout.AutoGenerated,
        "drag_and_drop": Qgis.AttributeFormLayout.DragAndDrop,
        "ui_file": Qgis.AttributeFormLayout.UiFile,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown form layout")
    return result


def _form_suppression(value):
    mapping = {
        "default": Qgis.AttributeFormSuppression.Default,
        "on": Qgis.AttributeFormSuppression.On,
        "off": Qgis.AttributeFormSuppression.Off,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown form suppression")
    return result


def _diagram(value):
    mapping = {
        "pie": QgsPieDiagram,
        "histogram": QgsHistogramDiagram,
        "stacked_bar": QgsStackedBarDiagram,
        "text": QgsTextDiagram,
    }
    diagram = mapping.get(str(value).casefold())
    if diagram is None:
        raise ValueError("Unknown diagram type")
    return diagram()


def _diagram_placement(value):
    mapping = {
        "around_point": QgsDiagramLayerSettings.Placement.AroundPoint,
        "over_point": QgsDiagramLayerSettings.Placement.OverPoint,
        "line": QgsDiagramLayerSettings.Placement.Line,
        "curved": QgsDiagramLayerSettings.Placement.Curved,
        "horizontal": QgsDiagramLayerSettings.Placement.Horizontal,
        "free": QgsDiagramLayerSettings.Placement.Free,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown diagram placement")
    return result


def _palette(count):
    values = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    return [values[index % len(values)] for index in range(count)]


def _bounds(rectangle):
    return [
        rectangle.xMinimum(),
        rectangle.yMinimum(),
        rectangle.xMaximum(),
        rectangle.yMaximum(),
    ]
