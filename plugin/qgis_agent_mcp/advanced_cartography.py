from __future__ import annotations

from pathlib import Path

from qgis.core import (
    QgsExpression,
    QgsFillSymbol,
    QgsLayoutItem,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutItemShape,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPrintLayout,
    QgsProject,
    QgsRectangle,
    QgsRenderContext,
    QgsRuleBasedRenderer,
    QgsStyle,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
    QgsWkbTypes,
)
from qgis.PyQt.QtGui import QColor, QFont

from .serialize import json_safe, layer_summary, renderer_summary


class AdvancedCartographyTools:
    def __init__(self, state, layer_resolver):
        self.state = state
        self.layer_resolver = layer_resolver

    def renderer(
        self,
        layer,
        action="inspect",
        rules=None,
        path=None,
    ):
        target = self._vector(layer)
        if action == "inspect":
            return {
                "layer": layer_summary(target),
                "renderer": renderer_summary(target),
                "symbols": [self._symbol(item) for item in self._renderer_symbols(target)],
            }
        if action == "rule_based":
            if not isinstance(rules, list) or not rules:
                raise ValueError("rules must be a non-empty array")
            root = QgsRuleBasedRenderer.Rule(None)
            for definition in rules:
                expression = str(definition.get("expression") or "")
                parsed = QgsExpression(expression)
                if expression and parsed.hasParserError():
                    raise ValueError("Invalid rule expression: {}".format(parsed.parserErrorString()))
                symbol = _symbol_for_layer(
                    target,
                    definition.get("color", "#4a90e2"),
                    definition.get("opacity", 1.0),
                    definition.get("size", 3.0),
                    definition.get("width", 0.8),
                )
                rule = QgsRuleBasedRenderer.Rule(
                    symbol,
                    int(definition.get("minimum_scale", 0)),
                    int(definition.get("maximum_scale", 0)),
                    expression,
                    str(definition.get("label") or expression or "Rule"),
                    str(definition.get("description") or ""),
                    bool(definition.get("else", False)),
                )
                root.appendChild(rule)
            target.setRenderer(QgsRuleBasedRenderer(root))
        elif action == "save_qml":
            if not path:
                raise ValueError("path is required")
            message, ok = target.saveNamedStyle(str(Path(path).expanduser()))
            if not ok:
                raise RuntimeError("Could not save style: {}".format(message))
            return {"layer_id": target.id(), "path": str(Path(path).expanduser()), "message": message}
        elif action == "load_qml":
            if not path:
                raise ValueError("path is required")
            message, ok = target.loadNamedStyle(str(Path(path).expanduser()))
            if not ok:
                raise RuntimeError("Could not load style: {}".format(message))
        else:
            raise ValueError("Unknown renderer action")
        target.triggerRepaint()
        self.state.touch("layer.renderer", {"layer_id": target.id(), "action": action})
        return {"layer": layer_summary(target), "renderer": renderer_summary(target)}

    def symbol(
        self,
        layer,
        action="inspect",
        color=None,
        opacity=None,
        size=None,
        width=None,
        angle=None,
    ):
        target = self._vector(layer)
        symbols = self._renderer_symbols(target)
        if action == "inspect":
            return {"layer_id": target.id(), "symbols": [self._symbol(item) for item in symbols]}
        if action != "set":
            raise ValueError("Unknown symbol action")
        for symbol in symbols:
            if color is not None:
                symbol.setColor(QColor(str(color)))
            if opacity is not None:
                symbol.setOpacity(max(0.0, min(float(opacity), 1.0)))
            if size is not None and hasattr(symbol, "setSize"):
                symbol.setSize(float(size))
            if width is not None and hasattr(symbol, "setWidth"):
                symbol.setWidth(float(width))
            if angle is not None and hasattr(symbol, "setAngle"):
                symbol.setAngle(float(angle))
        target.triggerRepaint()
        self.state.touch("layer.symbol", {"layer_id": target.id()})
        return {"layer_id": target.id(), "symbols": [self._symbol(item) for item in symbols]}

    def style_library(
        self,
        action="list",
        kind="symbols",
        query="",
        name=None,
        layer=None,
        path=None,
        limit=200,
    ):
        style = QgsStyle.defaultStyle()
        if style is None:
            raise RuntimeError("QGIS default style library is unavailable")
        if action == "list":
            names = _style_names(style, kind)
            query = str(query or "").casefold()
            if query:
                names = [item for item in names if query in item.casefold()]
            limit = max(1, min(int(limit), 1000))
            return {"kind": kind, "names": names[:limit], "count": len(names), "has_more": len(names) > limit}
        if action == "inspect_symbol":
            symbol = style.symbol(str(name or ""))
            if symbol is None:
                raise KeyError("Style symbol not found")
            return {"name": str(name), "symbol": self._symbol(symbol)}
        if action == "save_layer_symbol":
            target = self._vector(layer)
            symbols = self._renderer_symbols(target)
            if not name or len(symbols) != 1:
                raise ValueError("name and a single-symbol renderer are required")
            if not style.addSymbol(str(name), symbols[0].clone(), True):
                raise RuntimeError("Could not save symbol")
        elif action == "remove_symbol":
            if not name or not style.removeSymbol(str(name)):
                raise KeyError("Style symbol not found")
        elif action == "export_xml":
            if not path or not style.exportXml(str(Path(path).expanduser())):
                raise RuntimeError("Could not export style library")
        elif action == "import_xml":
            if not path or not style.importXml(str(Path(path).expanduser())):
                raise RuntimeError("Could not import style library")
        else:
            raise ValueError("Unknown style library action")
        self.state.touch("style.library", {"action": action, "name": name})
        return {"action": action, "name": name, "path": path, "ok": True}

    def labeling(
        self,
        layer,
        action="inspect",
        enabled=True,
        field=None,
        expression=None,
        font_family=None,
        font_size=10,
        color="#202020",
        buffer_size=0,
        buffer_color="#ffffff",
        placement=None,
        priority=5,
        obstacle=True,
    ):
        target = self._vector(layer)
        if action == "inspect":
            settings = target.labeling().settings() if target.labeling() else None
            return {
                "layer_id": target.id(),
                "enabled": target.labelsEnabled(),
                "field_name": settings.fieldName if settings else None,
                "is_expression": settings.isExpression if settings else None,
                "placement": int(settings.placement) if settings else None,
                "priority": settings.priority if settings else None,
                "obstacle": settings.obstacle if settings else None,
            }
        if action == "disable":
            target.setLabelsEnabled(False)
        elif action == "set":
            value = expression if expression is not None else field
            if not value:
                raise ValueError("field or expression is required")
            if expression is None and target.fields().indexOf(str(field)) < 0:
                raise KeyError("Label field not found")
            settings = QgsPalLayerSettings()
            settings.enabled = bool(enabled)
            settings.fieldName = str(value)
            settings.isExpression = expression is not None
            settings.priority = max(0, min(int(priority), 10))
            settings.obstacle = bool(obstacle)
            if placement:
                settings.placement = _placement(placement)
            text = QgsTextFormat()
            font = QFont(str(font_family)) if font_family else QFont()
            text.setFont(font)
            text.setSize(float(font_size))
            text.setColor(QColor(str(color)))
            if float(buffer_size) > 0:
                buffer = QgsTextBufferSettings()
                buffer.setEnabled(True)
                buffer.setSize(float(buffer_size))
                buffer.setColor(QColor(str(buffer_color)))
                text.setBuffer(buffer)
            settings.setFormat(text)
            target.setLabeling(QgsVectorLayerSimpleLabeling(settings))
            target.setLabelsEnabled(bool(enabled))
        else:
            raise ValueError("Unknown labeling action")
        target.triggerRepaint()
        self.state.touch("layer.labeling", {"layer_id": target.id(), "action": action})
        return self.labeling(target.id(), "inspect")

    def layout_items(
        self,
        layout,
        action="list",
        item_id=None,
        item_type=None,
        x=None,
        y=None,
        width=None,
        height=None,
        rotation=None,
        visible=None,
        locked=None,
        frame=None,
        background=None,
        text=None,
        extent=None,
        scale=None,
        layers=None,
        picture_path=None,
        linked_map=None,
    ):
        target_layout = self._layout(layout)
        if action == "list":
            return {
                "layout": target_layout.name(),
                "items": [self._item(item) for item in target_layout.items() if isinstance(item, QgsLayoutItem)],
            }
        if action == "add":
            if not item_type:
                raise ValueError("item_type is required")
            item = self._new_item(target_layout, item_type)
            target_layout.addLayoutItem(item)
            if not item_id:
                item_id = "mcp_{}_{}".format(item_type, len(target_layout.items()))
            item.setId(str(item_id))
            if isinstance(item, QgsLayoutItemMap):
                item.setExtent(self._map_extent(extent))
            if isinstance(item, QgsLayoutItemLabel):
                item.setText(str(text or ""))
                item.adjustSizeToText()
            if isinstance(item, QgsLayoutItemPicture) and picture_path:
                item.setPicturePath(str(Path(picture_path).expanduser()))
            if isinstance(item, (QgsLayoutItemLegend, QgsLayoutItemScaleBar)):
                map_item = self._map_item(target_layout, linked_map)
                item.setLinkedMap(map_item)
                if isinstance(item, QgsLayoutItemLegend):
                    item.setTitle(str(text or "Legend"))
                    item.setAutoUpdateModel(True)
                else:
                    item.setStyle("Single Box")
                    item.applyDefaultSize()
            if isinstance(item, QgsLayoutItemShape):
                item.setShapeType(_shape_type(text or "rectangle"))
        else:
            item = target_layout.itemById(str(item_id or ""))
            if item is None:
                raise KeyError("Layout item not found")
            if action == "remove":
                summary = self._item(item)
                target_layout.removeLayoutItem(item)
                self.state.touch("layout.item_removed", {"layout": layout, "item_id": item_id})
                return {"removed": summary}
            if action != "update":
                raise ValueError("Unknown layout item action")

        if x is not None or y is not None:
            current = item.positionWithUnits()
            item.attemptMove(
                QgsLayoutPoint(
                    float(x if x is not None else current.x()),
                    float(y if y is not None else current.y()),
                    QgsUnitTypes.LayoutUnit.LayoutMillimeters,
                )
            )
        if width is not None or height is not None:
            current = item.sizeWithUnits()
            item.attemptResize(
                QgsLayoutSize(
                    float(width if width is not None else current.width()),
                    float(height if height is not None else current.height()),
                    QgsUnitTypes.LayoutUnit.LayoutMillimeters,
                )
            )
        if rotation is not None:
            item.setItemRotation(float(rotation))
        if visible is not None:
            item.setVisibility(bool(visible))
        if locked is not None:
            item.setLocked(bool(locked))
        if frame is not None:
            item.setFrameEnabled(bool(frame))
        if background is not None:
            item.setBackgroundEnabled(bool(background))
        if text is not None and isinstance(item, QgsLayoutItemLabel):
            item.setText(str(text))
        if isinstance(item, QgsLayoutItemMap):
            if extent is not None:
                item.setExtent(self._map_extent(extent))
            if scale is not None:
                item.setScale(float(scale))
            if layers is not None:
                item.setLayers([self.layer_resolver(reference) for reference in layers])
                item.setKeepLayerSet(True)
        if picture_path is not None and isinstance(item, QgsLayoutItemPicture):
            item.setPicturePath(str(Path(picture_path).expanduser()))
        target_layout.refresh()
        self.state.touch("layout.item", {"layout": layout, "item_id": item.id(), "action": action})
        return self._item(item)

    def atlas(
        self,
        layout,
        action="status",
        coverage_layer=None,
        enabled=None,
        filter_expression=None,
        sort_expression=None,
        sort_ascending=True,
        filename_expression=None,
        page_name_expression=None,
        hide_coverage=False,
    ):
        target_layout = self._layout(layout)
        atlas = target_layout.atlas()
        if action == "status":
            return self._atlas(atlas)
        if action == "disable":
            atlas.setEnabled(False)
        elif action == "configure":
            coverage = self._vector(coverage_layer)
            atlas.setCoverageLayer(coverage)
            atlas.setEnabled(True if enabled is None else bool(enabled))
            atlas.setHideCoverage(bool(hide_coverage))
            atlas.setFilterFeatures(bool(filter_expression))
            if filter_expression:
                _validate_expression(filter_expression)
                atlas.setFilterExpression(str(filter_expression))
            atlas.setSortFeatures(bool(sort_expression))
            if sort_expression:
                _validate_expression(sort_expression)
                atlas.setSortExpression(str(sort_expression))
                atlas.setSortAscending(bool(sort_ascending))
            if filename_expression is not None:
                _validate_expression(filename_expression)
                atlas.setFilenameExpression(str(filename_expression))
            if page_name_expression is not None:
                _validate_expression(page_name_expression)
                atlas.setPageNameExpression(str(page_name_expression))
        else:
            raise ValueError("Unknown atlas action")
        self.state.touch("layout.atlas", {"layout": layout, "action": action})
        return self._atlas(atlas)

    def layout_validate(self, layout):
        target = self._layout(layout)
        issues = []
        ids = []
        for item in target.items():
            if not isinstance(item, QgsLayoutItem):
                continue
            summary = self._item(item)
            if not item.id():
                issues.append({"severity": "warning", "type": "missing_id", "item_type": summary["type"]})
            else:
                ids.append(item.id())
            if summary["size"][0] <= 0 or summary["size"][1] <= 0:
                issues.append({"severity": "error", "type": "empty_size", "item_id": item.id()})
            if isinstance(item, QgsLayoutItemLabel) and not item.text().strip():
                issues.append({"severity": "warning", "type": "empty_label", "item_id": item.id()})
            if isinstance(item, QgsLayoutItemPicture):
                path = item.picturePath()
                if path and not (path.startswith("http") or Path(path).expanduser().exists()):
                    issues.append({"severity": "error", "type": "missing_picture", "item_id": item.id(), "path": path})
        for duplicate in {value for value in ids if ids.count(value) > 1}:
            issues.append({"severity": "error", "type": "duplicate_id", "item_id": duplicate})
        atlas = target.atlas()
        if atlas.enabled() and atlas.coverageLayer() is None:
            issues.append({"severity": "error", "type": "atlas_missing_coverage"})
        return {
            "layout": target.name(),
            "valid": not any(item["severity"] == "error" for item in issues),
            "issues": issues,
            "item_count": len(target.items()),
            "atlas": self._atlas(atlas),
        }

    def _vector(self, reference):
        target = self.layer_resolver(reference)
        if not isinstance(target, QgsVectorLayer):
            raise ValueError("Vector layer is required")
        return target

    @staticmethod
    def _renderer_symbols(layer):
        return layer.renderer().symbols(QgsRenderContext())

    @staticmethod
    def _symbol(symbol):
        return {
            "type": int(symbol.type()),
            "color": symbol.color().name(QColor.NameFormat.HexArgb),
            "opacity": symbol.opacity(),
            "output_unit": int(symbol.outputUnit()),
            "layers": [
                {
                    "type": item.layerType(),
                    "properties": json_safe(item.properties()),
                    "enabled": item.enabled(),
                    "locked": item.isLocked(),
                }
                for item in symbol.symbolLayers()
            ],
        }

    @staticmethod
    def _layout(name):
        layout = QgsProject.instance().layoutManager().layoutByName(str(name))
        if not isinstance(layout, QgsPrintLayout):
            raise KeyError("Print layout not found")
        return layout

    @staticmethod
    def _new_item(layout, item_type):
        mapping = {
            "map": QgsLayoutItemMap,
            "label": QgsLayoutItemLabel,
            "legend": QgsLayoutItemLegend,
            "scalebar": QgsLayoutItemScaleBar,
            "picture": QgsLayoutItemPicture,
            "shape": QgsLayoutItemShape,
        }
        item_class = mapping.get(str(item_type).casefold())
        if item_class is None:
            raise ValueError("Unsupported layout item type")
        return item_class(layout)

    @staticmethod
    def _item(item):
        position = item.positionWithUnits()
        size = item.sizeWithUnits()
        background_enabled = getattr(item, "backgroundEnabled", None)
        result = {
            "id": item.id(),
            "uuid": item.uuid(),
            "type": type(item).__name__,
            "position": [position.x(), position.y()],
            "size": [size.width(), size.height()],
            "rotation": item.itemRotation(),
            "visible": item.isVisible(),
            "locked": item.isLocked(),
            "frame": item.frameEnabled(),
            "background": background_enabled() if callable(background_enabled) else None,
        }
        if isinstance(item, QgsLayoutItemLabel):
            result["text"] = item.text()
        elif isinstance(item, QgsLayoutItemMap):
            extent = item.extent()
            result.update(
                {
                    "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
                    "scale": item.scale(),
                    "layer_ids": [layer.id() for layer in item.layers()],
                }
            )
        elif isinstance(item, QgsLayoutItemPicture):
            result["picture_path"] = item.picturePath()
        return result

    def _map_extent(self, extent):
        if extent is None:
            return self._canvas_extent()
        if not isinstance(extent, list) or len(extent) != 4:
            raise ValueError("extent must contain xmin, ymin, xmax, ymax")
        return QgsRectangle(*extent)

    @staticmethod
    def _canvas_extent():
        project = QgsProject.instance()
        extent = QgsRectangle()
        for layer in project.mapLayers().values():
            if extent.isNull():
                extent = QgsRectangle(layer.extent())
            else:
                extent.combineExtentWith(layer.extent())
        return extent

    @staticmethod
    def _map_item(layout, reference):
        if reference:
            item = layout.itemById(str(reference))
            if isinstance(item, QgsLayoutItemMap):
                return item
            raise KeyError("Linked map item not found")
        item = next(
            (item for item in layout.items() if isinstance(item, QgsLayoutItemMap)),
            None,
        )
        if item is None:
            raise ValueError("A map item must exist before adding a linked legend or scale bar")
        return item

    @staticmethod
    def _atlas(atlas):
        coverage = atlas.coverageLayer()
        return {
            "enabled": atlas.enabled(),
            "coverage_layer_id": coverage.id() if coverage else None,
            "filter_expression": atlas.filterExpression(),
            "sort_expression": atlas.sortExpression(),
            "filename_expression": atlas.filenameExpression(),
            "page_name_expression": atlas.pageNameExpression(),
            "hide_coverage": atlas.hideCoverage(),
        }


def _symbol_for_layer(layer, color, opacity, size, width):
    geometry = QgsWkbTypes.geometryType(layer.wkbType())
    properties = {"color": str(color)}
    if geometry == QgsWkbTypes.GeometryType.PointGeometry:
        properties["size"] = str(size)
        symbol = QgsMarkerSymbol.createSimple(properties)
    elif geometry == QgsWkbTypes.GeometryType.LineGeometry:
        properties["width"] = str(width)
        symbol = QgsLineSymbol.createSimple(properties)
    else:
        symbol = QgsFillSymbol.createSimple(properties)
    symbol.setOpacity(max(0.0, min(float(opacity), 1.0)))
    return symbol


def _style_names(style, kind):
    mapping = {
        "symbols": style.symbolNames,
        "color_ramps": style.colorRampNames,
        "text_formats": style.textFormatNames,
        "label_settings": style.labelSettingsNames,
    }
    getter = mapping.get(str(kind))
    if getter is None:
        raise ValueError("Unknown style item kind")
    return sorted(getter())


def _placement(value):
    mapping = {
        "around_point": QgsPalLayerSettings.Placement.AroundPoint,
        "over_point": QgsPalLayerSettings.PredefinedPointPosition.OverPoint,
        "line": QgsPalLayerSettings.Placement.Line,
        "curved": QgsPalLayerSettings.Placement.Curved,
        "horizontal": QgsPalLayerSettings.Placement.Horizontal,
        "free": QgsPalLayerSettings.Placement.Free,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown label placement")
    return result


def _shape_type(value):
    mapping = {
        "rectangle": QgsLayoutItemShape.Shape.Rectangle,
        "ellipse": QgsLayoutItemShape.Shape.Ellipse,
        "triangle": QgsLayoutItemShape.Shape.Triangle,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown layout shape")
    return result


def _validate_expression(value):
    parsed = QgsExpression(str(value))
    if parsed.hasParserError():
        raise ValueError("Invalid expression: {}".format(parsed.parserErrorString()))
