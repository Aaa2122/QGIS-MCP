from __future__ import annotations

import base64
import datetime
import math

from qgis.core import (
    QgsFieldConstraints,
    QgsMapLayer,
    QgsPointCloudLayer,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QByteArray, QDate, QDateTime, QTime


def json_safe(value, depth=0):
    if depth > 8:
        return repr(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, QDateTime):
        return value.toString("yyyy-MM-ddTHH:mm:ss.zzz")
    if isinstance(value, QDate):
        return value.toString("yyyy-MM-dd")
    if isinstance(value, QTime):
        return value.toString("HH:mm:ss.zzz")
    if isinstance(value, QByteArray):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, QgsMapLayer):
        return layer_summary(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, depth + 1) for item in value]
    if hasattr(value, "toString"):
        try:
            return value.toString()
        except Exception:
            return repr(value)
    return repr(value)


def extent_summary(extent):
    if extent is None or extent.isNull():
        return None
    return {
        "xmin": extent.xMinimum(),
        "ymin": extent.yMinimum(),
        "xmax": extent.xMaximum(),
        "ymax": extent.yMaximum(),
    }


def layer_summary(layer, detail="standard"):
    base = {
        "id": layer.id(),
        "name": layer.name(),
        "type": layer.type().name if hasattr(layer.type(), "name") else int(layer.type()),
        "valid": layer.isValid(),
        "provider": layer.providerType(),
        "crs": layer.crs().authid() if layer.crs().isValid() else None,
    }
    if detail == "summary":
        if isinstance(layer, QgsVectorLayer):
            base["geometry_type"] = QgsWkbTypes.displayString(layer.wkbType())
        elif isinstance(layer, QgsRasterLayer):
            base["band_count"] = layer.bandCount()
        return base
    base.update(
        {
            "source": (
                layer.publicSource() if hasattr(layer, "publicSource") else layer.source()
            ),
            "extent": extent_summary(layer.extent()),
            "opacity": layer.opacity(),
        }
    )
    if isinstance(layer, QgsVectorLayer):
        base.update(
            {
                "geometry_type": QgsWkbTypes.displayString(layer.wkbType()),
                "feature_count": layer.featureCount(),
                "selected_count": layer.selectedFeatureCount(),
                "editable": layer.isEditable(),
                "modified": layer.isModified(),
                "fields": len(layer.fields()),
            }
        )
    elif isinstance(layer, QgsRasterLayer):
        base.update(
            {
                "width": layer.width(),
                "height": layer.height(),
                "band_count": layer.bandCount(),
            }
        )
    elif isinstance(layer, QgsPointCloudLayer):
        provider = layer.dataProvider()
        point_count = getattr(provider, "pointCount", None)
        base.update(
            {
                "point_count": point_count() if callable(point_count) else None,
                "renderer_class": (
                    type(layer.renderer()).__name__ if layer.renderer() is not None else None
                ),
            }
        )
    return base


def field_schema(field):
    return {
        "name": field.name(),
        "type": field.typeName(),
        "length": field.length(),
        "precision": field.precision(),
        "alias": field.alias(),
        "comment": field.comment(),
        "nullable": field.constraints().constraints()
        & QgsFieldConstraints.Constraint.ConstraintNotNull
        == 0,
    }


def feature_summary(feature, fields, include_geometry=False):
    attributes = {
        field.name(): json_safe(feature[field.name()])
        for field in fields
        if feature.fields().indexOf(field.name()) >= 0
    }
    value = {"id": feature.id(), "attributes": attributes}
    if include_geometry:
        geometry = feature.geometry()
        if geometry and not geometry.isNull():
            value["geometry"] = {
                "wkb_type": QgsWkbTypes.displayString(geometry.wkbType()),
                "bbox": extent_summary(geometry.boundingBox()),
                "wkt": geometry.asWkt(8)
                if geometry.constGet() and geometry.constGet().nCoordinates() <= 1000
                else None,
            }
    return value


def renderer_summary(layer):
    renderer = layer.renderer() if hasattr(layer, "renderer") else None
    if renderer is None:
        return None
    renderer_type = getattr(renderer, "type", None)
    dump = getattr(renderer, "dump", None)
    result = {
        "type": renderer_type() if callable(renderer_type) else type(renderer).__name__,
        "class": type(renderer).__name__,
        # Vector renderers expose dump(), but raster renderers generally do not.
        # Keep the field stable for MCP clients instead of raising AttributeError.
        "dump": dump() if callable(dump) else None,
    }
    for key, method in (
        ("opacity", "opacity"),
        ("band", "inputBand"),
        ("red_band", "redBand"),
        ("green_band", "greenBand"),
        ("blue_band", "blueBand"),
    ):
        candidate = getattr(renderer, method, None)
        if callable(candidate):
            try:
                result[key] = json_safe(candidate())
            except Exception:
                result[key] = None
    try:
        legend_items = getattr(renderer, "legendSymbolItems", None)
        result["legend_items"] = [
            {"label": item.label(), "rule_key": item.ruleKey()}
            for item in (legend_items()[:100] if callable(legend_items) else [])
        ]
    except Exception:
        result["legend_items"] = []
    return result
