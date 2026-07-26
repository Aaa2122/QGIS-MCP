from __future__ import annotations

from qgis.core import (
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsDateTimeRange,
    QgsMeshDatasetIndex,
    QgsMultiBandColorRenderer,
    QgsRasterShader,
    QgsSingleBandGrayRenderer,
    QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtCore import QDateTime, Qt
from qgis.PyQt.QtGui import QColor

from .serialize import json_safe, layer_summary, renderer_summary


class SpecializedDataTools:
    def __init__(self, state, layer_resolver):
        self.state = state
        self.layer_resolver = layer_resolver

    def layer_properties(
        self,
        layer,
        action="inspect",
        name=None,
        opacity=None,
        blend_mode=None,
        scale_based_visibility=None,
        minimum_scale=None,
        maximum_scale=None,
        auto_refresh_interval=None,
    ):
        target = self.layer_resolver(layer)
        if action == "inspect":
            return self._layer(target)
        if action == "reload":
            target.reload()
        elif action == "set":
            if name is not None:
                target.setName(str(name))
            if opacity is not None:
                _call_required(target, "setOpacity", max(0.0, min(float(opacity), 1.0)))
            if blend_mode is not None:
                _call_required(target, "setBlendMode", int(blend_mode))
            if scale_based_visibility is not None:
                target.setScaleBasedVisibility(bool(scale_based_visibility))
            if minimum_scale is not None:
                target.setMinimumScale(float(minimum_scale))
            if maximum_scale is not None:
                target.setMaximumScale(float(maximum_scale))
            if auto_refresh_interval is not None:
                target.setAutoRefreshInterval(max(0, int(auto_refresh_interval)))
                if hasattr(target, "setAutoRefreshEnabled"):
                    target.setAutoRefreshEnabled(int(auto_refresh_interval) > 0)
        else:
            raise ValueError("Unknown layer properties action")
        target.triggerRepaint()
        self.state.touch("layer.properties", {"layer_id": target.id(), "action": action})
        return self._layer(target)

    def capabilities(self, layer, query="", include_provider=True, limit=300):
        target = self.layer_resolver(layer)
        query = str(query or "").casefold()
        limit = max(1, min(int(limit), 1000))
        methods = _public_callables(target, query, limit)
        provider = target.dataProvider()
        result = {
            "layer": layer_summary(target),
            "class": type(target).__name__,
            "methods": methods,
            "method_count": len(methods),
        }
        if include_provider and provider is not None:
            provider_methods = _public_callables(provider, query, limit)
            result["provider"] = {
                "class": type(provider).__name__,
                "name": target.providerType(),
                "capabilities": _safe_value(provider, "capabilities"),
                "capabilities_text": _safe_value(provider, "capabilitiesString"),
                "methods": provider_methods,
                "method_count": len(provider_methods),
            }
        return result

    def raster_style(
        self,
        layer,
        action="inspect",
        band=1,
        red_band=1,
        green_band=2,
        blue_band=3,
        opacity=None,
        minimum=None,
        maximum=None,
        color_ramp=None,
        interpolation="linear",
    ):
        target = self._expect(layer, "QgsRasterLayer")
        provider = target.dataProvider()
        if action == "inspect":
            return self._raster_style(target)
        if opacity is not None:
            target.renderer().setOpacity(max(0.0, min(float(opacity), 1.0)))
        if action == "set_opacity":
            if opacity is None:
                raise ValueError("opacity is required")
        elif action == "single_band_gray":
            _valid_band(target, band)
            renderer = QgsSingleBandGrayRenderer(provider, int(band))
            if minimum is not None or maximum is not None:
                enhancement = QgsContrastEnhancement(provider.dataType(int(band)))
                if minimum is not None:
                    enhancement.setMinimumValue(float(minimum))
                if maximum is not None:
                    enhancement.setMaximumValue(float(maximum))
                enhancement.setContrastEnhancementAlgorithm(
                    QgsContrastEnhancement.StretchToMinimumMaximum, True
                )
                renderer.setContrastEnhancement(enhancement)
            target.setRenderer(renderer)
        elif action == "multiband_color":
            for value in (red_band, green_band, blue_band):
                _valid_band(target, value)
            target.setRenderer(
                QgsMultiBandColorRenderer(
                    provider, int(red_band), int(green_band), int(blue_band)
                )
            )
        elif action == "pseudocolor":
            _valid_band(target, band)
            if not isinstance(color_ramp, list) or len(color_ramp) < 2:
                raise ValueError("color_ramp requires at least two value/color items")
            shader = QgsColorRampShader()
            shader.setColorRampType(QgsColorRampShader.Interpolated)
            if str(interpolation).casefold() == "discrete":
                shader.setColorRampType(QgsColorRampShader.Discrete)
            elif str(interpolation).casefold() == "exact":
                shader.setColorRampType(QgsColorRampShader.Exact)
            shader.setColorRampItemList(
                [
                    QgsColorRampShader.ColorRampItem(
                        float(item["value"]),
                        QColor(str(item["color"])),
                        str(item.get("label") or item["value"]),
                    )
                    for item in color_ramp
                ]
            )
            raster_shader = QgsRasterShader()
            raster_shader.setRasterShaderFunction(shader)
            target.setRenderer(QgsSingleBandPseudoColorRenderer(provider, int(band), raster_shader))
        else:
            raise ValueError("Unknown raster style action")
        if opacity is not None and target.renderer() is not None:
            target.renderer().setOpacity(max(0.0, min(float(opacity), 1.0)))
        target.triggerRepaint()
        self.state.touch("raster.style", {"layer_id": target.id(), "action": action})
        return self._raster_style(target)

    def mesh(
        self,
        layer,
        action="inspect",
        dataset_group=None,
        dataset_index=0,
        active_scalar=None,
        active_vector=None,
        opacity=None,
    ):
        target = self._expect(layer, "QgsMeshLayer")
        if action == "inspect":
            return self._mesh(target)
        if action == "reload":
            target.reload()
        elif action == "set_active_dataset":
            if dataset_group is None:
                raise ValueError("dataset_group is required")
            settings = target.rendererSettings()
            index = QgsMeshDatasetIndex(int(dataset_group), int(dataset_index))
            if active_scalar is not False:
                settings.setActiveScalarDataset(index)
            if active_vector:
                settings.setActiveVectorDataset(index)
            target.setRendererSettings(settings)
        elif action == "set_opacity":
            if opacity is None:
                raise ValueError("opacity is required")
            _call_required(target, "setOpacity", max(0.0, min(float(opacity), 1.0)))
        else:
            raise ValueError("Unknown mesh action")
        target.triggerRepaint()
        self.state.touch("mesh.control", {"layer_id": target.id(), "action": action})
        return self._mesh(target)

    def point_cloud(self, layer, action="inspect", opacity=None, point_budget=None):
        target = self._expect(layer, "QgsPointCloudLayer")
        if action == "inspect":
            return self._point_cloud(target)
        if action == "reload":
            target.reload()
        elif action == "set_opacity":
            if opacity is None:
                raise ValueError("opacity is required")
            _call_required(target, "setOpacity", max(0.0, min(float(opacity), 1.0)))
        elif action == "set_point_budget":
            if point_budget is None:
                raise ValueError("point_budget is required")
            renderer = target.renderer()
            _call_required(renderer, "setMaximumScreenError", float(point_budget))
        else:
            raise ValueError("Unknown point cloud action")
        target.triggerRepaint()
        self.state.touch("point_cloud.control", {"layer_id": target.id(), "action": action})
        return self._point_cloud(target)

    def vector_tiles(self, layer, action="inspect", opacity=None, style_path=None):
        target = self._expect(layer, "QgsVectorTileLayer")
        if action == "inspect":
            return self._tile_layer(target)
        if action == "reload":
            target.reload()
        elif action == "set_opacity":
            if opacity is None:
                raise ValueError("opacity is required")
            _call_required(target, "setOpacity", max(0.0, min(float(opacity), 1.0)))
        elif action == "load_style":
            if not style_path:
                raise ValueError("style_path is required")
            message, ok = target.loadNamedStyle(str(style_path))
            if not ok:
                raise RuntimeError("Could not load vector tile style: {}".format(message))
        else:
            raise ValueError("Unknown vector tile action")
        target.triggerRepaint()
        self.state.touch("vector_tile.control", {"layer_id": target.id(), "action": action})
        return self._tile_layer(target)

    def tiled_scene(self, layer, action="inspect", opacity=None, style_path=None):
        target = self._expect(layer, "QgsTiledSceneLayer")
        if action == "inspect":
            return self._tile_layer(target)
        if action == "reload":
            target.reload()
        elif action == "set_opacity":
            if opacity is None:
                raise ValueError("opacity is required")
            _call_required(target, "setOpacity", max(0.0, min(float(opacity), 1.0)))
        elif action == "load_style":
            if not style_path:
                raise ValueError("style_path is required")
            message, ok = target.loadNamedStyle(str(style_path))
            if not ok:
                raise RuntimeError("Could not load tiled scene style: {}".format(message))
        else:
            raise ValueError("Unknown tiled scene action")
        target.triggerRepaint()
        self.state.touch("tiled_scene.control", {"layer_id": target.id(), "action": action})
        return self._tile_layer(target)

    def temporal(
        self,
        layer,
        action="inspect",
        enabled=None,
        start=None,
        end=None,
    ):
        target = self.layer_resolver(layer)
        properties = target.temporalProperties()
        if properties is None:
            raise RuntimeError("Layer does not expose temporal properties")
        if action == "inspect":
            return self._temporal(target, properties)
        if action == "set_active":
            if enabled is None:
                raise ValueError("enabled is required")
            properties.setIsActive(bool(enabled))
        elif action == "set_fixed_range":
            if not start or not end:
                raise ValueError("start and end are required")
            date_range = QgsDateTimeRange(_date_time(start), _date_time(end))
            _call_required(properties, "setFixedTemporalRange", date_range)
            properties.setIsActive(True if enabled is None else bool(enabled))
        else:
            raise ValueError("Unknown temporal action")
        self.state.touch("layer.temporal", {"layer_id": target.id(), "action": action})
        return self._temporal(target, properties)

    def elevation(
        self,
        layer,
        action="inspect",
        enabled=None,
        z_scale=None,
        z_offset=None,
        extrusion=None,
    ):
        target = self.layer_resolver(layer)
        properties = target.elevationProperties()
        if properties is None:
            raise RuntimeError("Layer does not expose elevation properties")
        if action == "inspect":
            return self._elevation(target, properties)
        if action != "set":
            raise ValueError("Unknown elevation action")
        if enabled is not None:
            _call_first(properties, ("setEnabled", "setIsEnabled"), bool(enabled))
        if z_scale is not None:
            _call_required(properties, "setZScale", float(z_scale))
        if z_offset is not None:
            _call_required(properties, "setZOffset", float(z_offset))
        if extrusion is not None:
            _call_required(properties, "setExtrusionEnabled", bool(extrusion))
        self.state.touch("layer.elevation", {"layer_id": target.id()})
        return self._elevation(target, properties)

    def _expect(self, reference, class_name):
        target = self.layer_resolver(reference)
        if type(target).__name__ != class_name:
            raise ValueError("{} layer is required".format(class_name))
        return target

    @staticmethod
    def _layer(layer):
        return {
            "layer": layer_summary(layer),
            "class": type(layer).__name__,
            "opacity": _safe_value(layer, "opacity"),
            "blend_mode": _safe_value(layer, "blendMode"),
            "scale_based_visibility": layer.hasScaleBasedVisibility(),
            "minimum_scale": layer.minimumScale(),
            "maximum_scale": layer.maximumScale(),
            "auto_refresh_interval": _safe_value(layer, "autoRefreshInterval"),
            "auto_refresh_enabled": _safe_value(layer, "isAutoRefreshEnabled"),
        }

    @staticmethod
    def _raster_style(layer):
        renderer = layer.renderer()
        return {
            "layer": layer_summary(layer),
            "renderer": renderer_summary(layer),
            "renderer_class": type(renderer).__name__ if renderer else None,
            "opacity": renderer.opacity() if renderer else None,
            "uses_transparency": _safe_value(renderer, "usesTransparency") if renderer else None,
            "band": _safe_value(renderer, "inputBand") if renderer else None,
            "red_band": _safe_value(renderer, "redBand") if renderer else None,
            "green_band": _safe_value(renderer, "greenBand") if renderer else None,
            "blue_band": _safe_value(renderer, "blueBand") if renderer else None,
        }

    @staticmethod
    def _mesh(layer):
        provider = layer.dataProvider()
        groups = []
        count = int(_safe_value(provider, "datasetGroupCount") or 0)
        for index in range(count):
            metadata = provider.datasetGroupMetadata(index)
            groups.append(
                {
                    "index": index,
                    "name": _safe_value(metadata, "name"),
                    "scalar": _safe_value(metadata, "isScalar"),
                    "data_type": _safe_value(metadata, "dataType"),
                    "minimum": _safe_value(metadata, "minimum"),
                    "maximum": _safe_value(metadata, "maximum"),
                    "dataset_count": _safe_value(provider, "datasetCount", index),
                }
            )
        settings = layer.rendererSettings()
        return {
            "layer": layer_summary(layer),
            "dataset_groups": groups,
            "active_scalar": _dataset_index(_safe_value(settings, "activeScalarDataset")),
            "active_vector": _dataset_index(_safe_value(settings, "activeVectorDataset")),
            "mesh_frame_count": _safe_value(provider, "vertexCount"),
            "face_count": _safe_value(provider, "faceCount"),
            "edge_count": _safe_value(provider, "edgeCount"),
        }

    @staticmethod
    def _point_cloud(layer):
        provider = layer.dataProvider()
        attributes = []
        collection = _safe_raw(provider, "attributes")
        raw_attributes = _safe_raw(collection, "attributes") if collection is not None else []
        for attribute in raw_attributes or []:
            attributes.append(
                {
                    "name": _safe_value(attribute, "name"),
                    "type": _safe_value(attribute, "type"),
                    "size": _safe_value(attribute, "size"),
                }
            )
        renderer = layer.renderer()
        return {
            "layer": layer_summary(layer),
            "point_count": _safe_value(provider, "pointCount"),
            "attributes": attributes,
            "renderer_class": type(renderer).__name__ if renderer else None,
            "renderer": json_safe(_safe_value(renderer, "toSld")) if renderer else None,
            "statistics": json_safe(_safe_value(provider, "metadataStatistics")),
        }

    @staticmethod
    def _tile_layer(layer):
        provider = layer.dataProvider()
        renderer = _safe_raw(layer, "renderer")
        return {
            "layer": layer_summary(layer),
            "provider_class": type(provider).__name__ if provider else None,
            "renderer_class": type(renderer).__name__ if renderer else None,
            "minimum_zoom": _safe_value(layer, "minimumZoom"),
            "maximum_zoom": _safe_value(layer, "maximumZoom"),
            "opacity": _safe_value(layer, "opacity"),
            "metadata": json_safe(_safe_value(provider, "htmlMetadata")),
        }

    @staticmethod
    def _temporal(layer, properties):
        date_range = _call_first_value(properties, ("fixedTemporalRange", "temporalExtent"))
        return {
            "layer_id": layer.id(),
            "active": bool(_safe_value(properties, "isActive")),
            "mode": _safe_value(properties, "mode"),
            "flags": _safe_value(properties, "flags"),
            "range": _date_range(date_range),
        }

    @staticmethod
    def _elevation(layer, properties):
        return {
            "layer_id": layer.id(),
            "class": type(properties).__name__,
            "enabled": _call_first_value(properties, ("isEnabled", "enabled")),
            "z_scale": _safe_value(properties, "zScale"),
            "z_offset": _safe_value(properties, "zOffset"),
            "extrusion_enabled": _safe_value(properties, "extrusionEnabled"),
            "flags": _safe_value(properties, "flags"),
        }


def _public_callables(value, query, limit):
    names = []
    for name in dir(value):
        if name.startswith("_") or (query and query not in name.casefold()):
            continue
        try:
            candidate = getattr(value, name)
        except Exception:
            continue
        if callable(candidate):
            names.append(name)
        if len(names) >= limit:
            break
    return names


def _safe_raw(value, method, *args):
    if value is None:
        return None
    candidate = getattr(value, method, None)
    if not callable(candidate):
        return None
    try:
        return candidate(*args)
    except Exception:
        return None


def _safe_value(value, method, *args):
    return json_safe(_safe_raw(value, method, *args))


def _call_required(value, method, *args):
    candidate = getattr(value, method, None)
    if not callable(candidate):
        raise RuntimeError("{} does not support {}".format(type(value).__name__, method))
    return candidate(*args)


def _call_first(value, methods, *args):
    for method in methods:
        candidate = getattr(value, method, None)
        if callable(candidate):
            return candidate(*args)
    raise RuntimeError("{} does not support {}".format(type(value).__name__, " or ".join(methods)))


def _call_first_value(value, methods):
    for method in methods:
        result = _safe_raw(value, method)
        if result is not None:
            return result
    return None


def _valid_band(layer, band):
    band = int(band)
    if band < 1 or band > layer.bandCount():
        raise ValueError("Invalid raster band")


def _date_time(value):
    result = QDateTime.fromString(str(value), Qt.ISODate)
    if not result.isValid():
        raise ValueError("Datetime must use ISO 8601 format")
    return result


def _date_range(value):
    if value is None:
        return None
    begin = _safe_raw(value, "begin")
    end = _safe_raw(value, "end")
    return {
        "begin": begin.toString(Qt.ISODate) if begin is not None else None,
        "end": end.toString(Qt.ISODate) if end is not None else None,
        "infinite": bool(_safe_value(value, "isInfinite")),
    }


def _dataset_index(value):
    if value is None:
        return None
    return {
        "group": _safe_value(value, "group"),
        "dataset": _safe_value(value, "dataset"),
        "valid": _safe_value(value, "isValid"),
    }
