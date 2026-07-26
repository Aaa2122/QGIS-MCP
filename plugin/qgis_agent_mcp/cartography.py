from __future__ import annotations

from pathlib import Path

from qgis.core import (
    QgsCategorizedSymbolRenderer,
    QgsGraduatedSymbolRenderer,
    QgsLayerTreeGroup,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsPalLayerSettings,
    QgsPrintLayout,
    QgsProject,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSingleSymbolRenderer,
    QgsSymbol,
    QgsTextBufferSettings,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor, QFont

from .autonomy import OutputPathPolicy
from .serialize import layer_summary, renderer_summary

FIELD_TYPES = {
    "bool": "boolean",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "double": "double",
    "float": "double",
    "int": "integer",
    "integer": "integer",
    "string": "string",
    "text": "string",
}


class ProjectLayerManager:
    def __init__(self, iface, state):
        self.iface = iface
        self.state = state

    def execute(
        self,
        action,
        layer=None,
        name=None,
        geometry=None,
        crs="EPSG:4326",
        fields=None,
        group=None,
        index=None,
        visible=None,
        opacity=None,
        subset=None,
        minimum_scale=None,
        maximum_scale=None,
        remove_layers=False,
    ):
        project = QgsProject.instance()
        root = project.layerTreeRoot()
        target = self._layer(layer) if layer else None
        if action == "create_memory":
            if not name or not geometry:
                raise ValueError("name and geometry are required")
            source = self._memory_source(geometry, crs, fields or [])
            created = QgsVectorLayer(source, name, "memory")
            if not created.isValid():
                raise ValueError("QGIS could not create the memory layer")
            project.addMapLayer(created, False)
            parent = self._group(group, create=True) if group else root
            parent.insertLayer(int(index or 0), created)
            result = layer_summary(created)
            event_data = {"layer_id": created.id(), "action": action}
        elif action == "clone":
            self._require_layer(target)
            created = target.clone()
            created.setName(name or "{} copy".format(target.name()))
            project.addMapLayer(created, False)
            parent = self._group(group, create=True) if group else root
            parent.insertLayer(int(index or 0), created)
            result = layer_summary(created)
            event_data = {"layer_id": created.id(), "action": action}
        elif action == "rename_layer":
            self._require_layer(target)
            if not name:
                raise ValueError("name is required")
            target.setName(name)
            result = layer_summary(target)
            event_data = {"layer_id": target.id(), "action": action}
        elif action == "move_layer":
            self._require_layer(target)
            node = root.findLayer(target.id())
            if node is None:
                raise ValueError("Layer is not present in the layer tree")
            parent = self._group(group, create=True) if group else root
            clone = node.clone()
            parent.insertChildNode(int(index or 0), clone)
            node.parent().removeChildNode(node)
            result = layer_summary(target)
            event_data = {"layer_id": target.id(), "action": action, "group": group}
        elif action == "set_visibility":
            self._require_layer(target)
            node = root.findLayer(target.id())
            if node is None:
                raise ValueError("Layer is not present in the layer tree")
            node.setItemVisibilityChecked(bool(visible))
            result = {"layer_id": target.id(), "visible": node.itemVisibilityChecked()}
            event_data = {"layer_id": target.id(), "action": action}
        elif action == "set_opacity":
            self._require_layer(target)
            value = float(opacity)
            if not 0 <= value <= 1:
                raise ValueError("opacity must be between 0 and 1")
            renderer = target.renderer()
            if renderer is None or not hasattr(renderer, "setOpacity"):
                raise ValueError("Layer renderer does not support opacity")
            renderer.setOpacity(value)
            target.triggerRepaint()
            result = {"layer_id": target.id(), "opacity": value}
            event_data = {"layer_id": target.id(), "action": action}
        elif action == "set_subset":
            self._require_vector(target)
            if not target.setSubsetString(subset or ""):
                raise ValueError("Provider rejected the subset expression")
            result = {"layer_id": target.id(), "subset": target.subsetString()}
            event_data = {"layer_id": target.id(), "action": action}
        elif action == "set_scale_visibility":
            self._require_layer(target)
            enabled = minimum_scale is not None or maximum_scale is not None
            target.setScaleBasedVisibility(enabled)
            if minimum_scale is not None:
                target.setMinimumScale(float(minimum_scale))
            if maximum_scale is not None:
                target.setMaximumScale(float(maximum_scale))
            result = {
                "layer_id": target.id(),
                "enabled": target.hasScaleBasedVisibility(),
                "minimum_scale": target.minimumScale(),
                "maximum_scale": target.maximumScale(),
            }
            event_data = {"layer_id": target.id(), "action": action}
        elif action == "create_group":
            if not name:
                raise ValueError("name is required")
            parent = self._group(group, create=True) if group else root
            created = parent.insertGroup(int(index or 0), name)
            result = {"name": created.name(), "path": self._group_path(created)}
            event_data = {"action": action, "group": result["path"]}
        elif action == "rename_group":
            target_group = self._group(group)
            if not name:
                raise ValueError("name is required")
            target_group.setName(name)
            result = {"name": name, "path": self._group_path(target_group)}
            event_data = {"action": action, "group": result["path"]}
        elif action == "remove_group":
            target_group = self._group(group)
            layer_ids = [node.layerId() for node in target_group.findLayers()]
            parent = target_group.parent()
            if parent is None:
                raise ValueError("The root group cannot be removed")
            parent.removeChildNode(target_group)
            if remove_layers:
                project.removeMapLayers(layer_ids)
            result = {"removed_group": group, "removed_layer_ids": layer_ids if remove_layers else []}
            event_data = {"action": action, "group": group}
        else:
            raise ValueError("Unknown layer management action")
        self.state.touch("layer.manage", event_data)
        return result

    @staticmethod
    def _memory_source(geometry, crs, fields):
        components = ["{}?crs={}".format(geometry, crs)]
        for field in fields:
            field_name = str(field.get("name") or "").strip()
            if not field_name:
                raise ValueError("Every field requires a name")
            field_type = FIELD_TYPES.get(str(field.get("type", "string")).casefold())
            if field_type is None:
                raise ValueError("Unsupported field type: {}".format(field.get("type")))
            components.append("field={}:{}".format(field_name, field_type))
        return "&".join(components)

    @staticmethod
    def _layer(reference):
        if not reference:
            return None
        project = QgsProject.instance()
        if reference in project.mapLayers():
            return project.mapLayer(reference)
        matches = project.mapLayersByName(reference)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError("Layer name is ambiguous; use its ID")
        raise ValueError("Layer not found: {}".format(reference))

    @staticmethod
    def _require_layer(layer):
        if layer is None:
            raise ValueError("layer is required")

    @staticmethod
    def _require_vector(layer):
        ProjectLayerManager._require_layer(layer)
        if not isinstance(layer, QgsVectorLayer):
            raise ValueError("A vector layer is required")

    def _group(self, path, create=False):
        if not path:
            raise ValueError("group is required")
        current = QgsProject.instance().layerTreeRoot()
        for name in [item for item in str(path).split("/") if item]:
            found = next(
                (child for child in current.children() if isinstance(child, QgsLayerTreeGroup) and child.name() == name),
                None,
            )
            if found is None:
                if not create:
                    raise ValueError("Group not found: {}".format(path))
                found = current.addGroup(name)
            current = found
        return current

    @staticmethod
    def _group_path(group):
        names = []
        current = group
        while current is not None and current.parent() is not None:
            names.append(current.name())
            current = current.parent()
        return "/".join(reversed(names))


class CartographyManager:
    def __init__(self, state):
        self.state = state

    def style(
        self,
        layer,
        mode="simple",
        field=None,
        color="#3388ff",
        opacity=1.0,
        size=3.0,
        width=0.8,
        classes=5,
        color_ramp=None,
    ):
        if not isinstance(layer, QgsVectorLayer):
            raise ValueError("Styling currently requires a vector layer")
        opacity = float(opacity)
        if not 0 <= opacity <= 1:
            raise ValueError("opacity must be between 0 and 1")
        if mode == "simple":
            symbol = self._symbol(layer, color, opacity, size, width)
            layer.setRenderer(QgsSingleSymbolRenderer(symbol))
        elif mode == "categorized":
            self._field(layer, field)
            values = sorted(layer.uniqueValues(layer.fields().indexOf(field), 100), key=str)
            categories = []
            for index, value in enumerate(values):
                symbol = self._symbol(layer, self._color(index, len(values), color_ramp), opacity, size, width)
                categories.append(QgsRendererCategory(value, symbol, str(value)))
            layer.setRenderer(QgsCategorizedSymbolRenderer(field, categories))
        elif mode == "graduated":
            self._field(layer, field)
            field_index = layer.fields().indexOf(field)
            minimum = layer.minimumValue(field_index)
            maximum = layer.maximumValue(field_index)
            if minimum is None or maximum is None:
                raise ValueError("The selected field has no numeric values")
            minimum, maximum = float(minimum), float(maximum)
            count = max(2, min(int(classes), 20))
            step = (maximum - minimum) / count if maximum != minimum else 1
            ranges = []
            for index in range(count):
                lower = minimum + index * step
                upper = maximum if index == count - 1 else minimum + (index + 1) * step
                symbol = self._symbol(layer, self._color(index, count, color_ramp), opacity, size, width)
                ranges.append(QgsRendererRange(lower, upper, symbol, "{:.3g} – {:.3g}".format(lower, upper)))
            layer.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))
        else:
            raise ValueError("mode must be simple, categorized, or graduated")
        layer.triggerRepaint()
        self.state.touch("layer.style", {"layer_id": layer.id(), "mode": mode})
        return {"layer_id": layer.id(), "style": renderer_summary(layer)}

    def labels(
        self,
        layer,
        field,
        enabled=True,
        font_size=10.0,
        color="#222222",
        buffer_size=1.0,
        buffer_color="#ffffff",
        expression=False,
    ):
        if not isinstance(layer, QgsVectorLayer):
            raise ValueError("Labels require a vector layer")
        if not expression:
            self._field(layer, field)
        settings = QgsPalLayerSettings()
        settings.enabled = bool(enabled)
        settings.fieldName = str(field)
        settings.isExpression = bool(expression)
        text_format = QgsTextFormat()
        text_format.setFont(QFont("Sans Serif"))
        text_format.setSize(float(font_size))
        text_format.setColor(QColor(color))
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(float(buffer_size) > 0)
        buffer.setSize(float(buffer_size))
        buffer.setColor(QColor(buffer_color))
        text_format.setBuffer(buffer)
        settings.setFormat(text_format)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(bool(enabled))
        layer.triggerRepaint()
        self.state.touch("layer.labels", {"layer_id": layer.id(), "enabled": bool(enabled)})
        return {"layer_id": layer.id(), "enabled": layer.labelsEnabled(), "field": field}

    @staticmethod
    def _field(layer, field):
        if not field or layer.fields().indexOf(field) < 0:
            raise ValueError("Unknown field: {}".format(field))

    @staticmethod
    def _symbol(layer, color, opacity, size, width):
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            raise ValueError("QGIS could not create a symbol for the layer geometry")
        symbol.setColor(QColor(color))
        symbol.setOpacity(float(opacity))
        if hasattr(symbol, "setSize"):
            symbol.setSize(float(size))
        if hasattr(symbol, "setWidth"):
            symbol.setWidth(float(width))
        return symbol

    @staticmethod
    def _color(index, count, ramp):
        presets = {
            "fire": ["#fff7bc", "#fec44f", "#fe9929", "#ec7014", "#cc4c02", "#8c2d04"],
            "blue": ["#eff3ff", "#bdd7e7", "#6baed6", "#3182bd", "#08519c"],
            "green": ["#edf8e9", "#bae4b3", "#74c476", "#31a354", "#006d2c"],
        }
        colors = presets.get(str(ramp or "blue").casefold(), presets["blue"])
        if count <= 1:
            return colors[-1]
        return colors[round(index * (len(colors) - 1) / (count - 1))]


class LayoutManager:
    def __init__(self, state, output_policy=None, iface=None):
        self.state = state
        self.output_policy = output_policy or OutputPathPolicy()
        self.iface = iface

    def execute(
        self,
        action,
        name=None,
        title=None,
        subtitle=None,
        orientation="landscape",
        source_text=None,
        path=None,
        format=None,
        dpi=200,
    ):
        project = QgsProject.instance()
        manager = project.layoutManager()
        if action == "list":
            return {"layouts": [self._summary(layout) for layout in manager.layouts()]}
        if action == "create":
            if not name:
                raise ValueError("name is required")
            if manager.layoutByName(name):
                raise ValueError("A layout with this name already exists")
            layout = self._create(project, name, title or name, subtitle, orientation, source_text)
            manager.addLayout(layout)
            self.state.touch("layout.created", {"name": name})
            return self._summary(layout)
        layout = manager.layoutByName(name or "")
        if layout is None:
            raise ValueError("Layout not found: {}".format(name))
        if action == "remove":
            manager.removeLayout(layout)
            self.state.touch("layout.removed", {"name": name})
            return {"removed": name}
        if action == "export":
            if not path:
                raise ValueError("path is required")
            export_format = str(format or Path(path).suffix.lstrip(".")).casefold()
            destination = self.output_policy.validate(path, project.fileName() or None)
            exporter = QgsLayoutExporter(layout)
            if export_format == "pdf":
                status = exporter.exportToPdf(str(destination), QgsLayoutExporter.PdfExportSettings())
            elif export_format == "png":
                settings = QgsLayoutExporter.ImageExportSettings()
                settings.dpi = int(dpi)
                status = exporter.exportToImage(str(destination), settings)
            elif export_format == "svg":
                status = exporter.exportToSvg(str(destination), QgsLayoutExporter.SvgExportSettings())
            else:
                raise ValueError("format must be pdf, png, or svg")
            if status != QgsLayoutExporter.Success:
                raise RuntimeError("QGIS layout export failed with status {}".format(int(status)))
            self.state.touch("layout.exported", {"name": name, "format": export_format})
            return {"layout": name, "path": str(destination), "format": export_format}
        raise ValueError("Unknown layout action")

    def _create(self, project, name, title, subtitle, orientation, source_text):
        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        layout.setName(name)
        page = layout.pageCollection().page(0)
        if orientation == "portrait":
            width, height = 210, 297
        elif orientation == "landscape":
            width, height = 297, 210
        else:
            raise ValueError("orientation must be portrait or landscape")
        page.setPageSize(QgsLayoutSize(width, height, QgsUnitTypes.LayoutMillimeters))
        margin = 12
        title_item = QgsLayoutItemLabel(layout)
        title_item.setText(title)
        title_item.setFont(QFont("Sans Serif", 18, QFont.Bold))
        title_item.adjustSizeToText()
        title_item.attemptMove(QgsLayoutPoint(margin, 8, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(title_item)
        top = 24
        if subtitle:
            subtitle_item = QgsLayoutItemLabel(layout)
            subtitle_item.setText(subtitle)
            subtitle_item.setFont(QFont("Sans Serif", 9))
            subtitle_item.adjustSizeToText()
            subtitle_item.attemptMove(QgsLayoutPoint(margin, 19, QgsUnitTypes.LayoutMillimeters))
            layout.addLayoutItem(subtitle_item)
            top = 30
        map_width = width - 2 * margin - 48
        map_height = height - top - 23
        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(margin, top, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptResize(QgsLayoutSize(map_width, map_height, QgsUnitTypes.LayoutMillimeters))
        canvas_extent = self.iface.mapCanvas().extent() if self.iface else None
        if canvas_extent is not None and not canvas_extent.isEmpty():
            map_item.setExtent(canvas_extent)
        else:
            layers = list(project.mapLayers().values())
            if layers:
                extent = layers[0].extent()
                for layer in layers[1:]:
                    extent.combineExtentWith(layer.extent())
                map_item.setExtent(extent)
        layout.addLayoutItem(map_item)
        legend = QgsLayoutItemLegend(layout)
        legend.setLinkedMap(map_item)
        legend.setTitle("Légende")
        legend.attemptMove(QgsLayoutPoint(width - margin - 43, top, QgsUnitTypes.LayoutMillimeters))
        legend.attemptResize(QgsLayoutSize(43, max(45, map_height - 25), QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(legend)
        scale = QgsLayoutItemScaleBar(layout)
        scale.setStyle("Single Box")
        scale.setLinkedMap(map_item)
        scale.applyDefaultSize()
        scale.attemptMove(QgsLayoutPoint(margin, height - 18, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(scale)
        if source_text:
            source = QgsLayoutItemLabel(layout)
            source.setText(source_text)
            source.setFont(QFont("Sans Serif", 7))
            source.adjustSizeToText()
            source.attemptMove(QgsLayoutPoint(width - margin - 95, height - 14, QgsUnitTypes.LayoutMillimeters))
            layout.addLayoutItem(source)
        return layout

    @staticmethod
    def _summary(layout):
        return {"name": layout.name(), "item_count": len(layout.items()), "page_count": layout.pageCollection().pageCount()}
