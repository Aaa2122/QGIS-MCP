from __future__ import annotations

from collections import Counter

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDefaultValue,
    QgsEditorWidgetSetup,
    QgsExpression,
    QgsFeatureRequest,
    QgsField,
    QgsFieldConstraints,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRasterBandStats,
    QgsRasterLayer,
    QgsRectangle,
    QgsRelation,
    QgsSnappingConfig,
    QgsVectorLayer,
    QgsVectorLayerJoinInfo,
)
from qgis.PyQt.QtCore import QVariant

from .serialize import field_schema, json_safe, layer_summary


class VectorRasterTools:
    def __init__(self, state, layer_resolver):
        self.state = state
        self.layer_resolver = layer_resolver

    def vector_schema(
        self,
        layer,
        action="inspect",
        field=None,
        name=None,
        field_type="string",
        length=0,
        precision=0,
        alias=None,
        default_expression=None,
        apply_default_on_update=False,
        constraint=None,
        constraint_expression=None,
        constraint_description=None,
        constraint_strength="hard",
        widget_type=None,
        widget_config=None,
    ):
        target = self._vector(layer)
        if action == "inspect":
            return self._schema(target)
        if not target.isEditable() and not target.startEditing():
            raise RuntimeError("Layer could not enter edit mode")
        if action == "add":
            if not name:
                raise ValueError("name is required")
            created = QgsField(
                str(name),
                _field_type(field_type),
                len=max(0, int(length)),
                prec=max(0, int(precision)),
            )
            if not target.addAttribute(created):
                raise RuntimeError("Could not add field")
            target.updateFields()
            field = name
        elif action == "delete":
            index = self._field_index(target, field)
            if not target.deleteAttribute(index):
                raise RuntimeError("Could not delete field")
        elif action == "rename":
            index = self._field_index(target, field)
            if not name or not target.renameAttribute(index, str(name)):
                raise RuntimeError("Could not rename field")
            field = name
        elif action != "configure":
            raise ValueError("Unknown vector schema action")

        if action in {"add", "rename", "configure"}:
            index = self._field_index(target, field)
            if alias is not None:
                target.setFieldAlias(index, str(alias))
            if default_expression is not None:
                expression = QgsExpression(str(default_expression))
                if expression.hasParserError():
                    raise ValueError("Invalid default expression: {}".format(expression.parserErrorString()))
                target.setDefaultValueDefinition(
                    index,
                    QgsDefaultValue(str(default_expression), bool(apply_default_on_update)),
                )
            if constraint:
                flag = _constraint(constraint)
                strength = (
                    QgsFieldConstraints.ConstraintStrengthSoft
                    if constraint_strength == "soft"
                    else QgsFieldConstraints.ConstraintStrengthHard
                )
                target.setFieldConstraint(index, flag, strength)
                if constraint == "expression":
                    if not constraint_expression:
                        raise ValueError("constraint_expression is required")
                    target.setConstraintExpression(
                        index,
                        str(constraint_expression),
                        str(constraint_description or ""),
                    )
            if widget_type:
                target.setEditorWidgetSetup(
                    index, QgsEditorWidgetSetup(str(widget_type), dict(widget_config or {}))
                )
        target.updateFields()
        self.state.touch("vector.schema", {"layer_id": target.id(), "action": action})
        return self._schema(target)

    def vector_statistics(self, layer, action="summary", field=None, expression=None, limit=1000):
        target = self._vector(layer)
        if action == "summary":
            return {
                "layer": layer_summary(target),
                "fields": [field_schema(item) for item in target.fields()],
                "selected_count": target.selectedFeatureCount(),
                "geometry_type": target.wkbType(),
                "editable": target.isEditable(),
            }
        if field is None:
            raise ValueError("field is required")
        index = self._field_index(target, field)
        if action == "unique":
            values = list(target.uniqueValues(index, min(int(limit), 10000)))
            return {"field": target.fields()[index].name(), "values": json_safe(values), "count": len(values)}
        if action == "value_counts":
            counts = Counter()
            request = QgsFeatureRequest().setSubsetOfAttributes(
                [target.fields()[index].name()], target.fields()
            )
            if expression:
                parsed = QgsExpression(expression)
                if parsed.hasParserError():
                    raise ValueError("Invalid expression")
                request.setFilterExpression(expression)
            for feature in target.getFeatures(request):
                counts[str(feature[index])] += 1
            return {
                "field": target.fields()[index].name(),
                "items": [{"value": key, "count": value} for key, value in counts.most_common(min(int(limit), 1000))],
                "distinct_count": len(counts),
            }
        if action == "numeric":
            values = []
            request = QgsFeatureRequest().setSubsetOfAttributes(
                [target.fields()[index].name()], target.fields()
            )
            for feature in target.getFeatures(request):
                try:
                    values.append(float(feature[index]))
                except (TypeError, ValueError):
                    continue
            if not values:
                return {"field": target.fields()[index].name(), "count": 0}
            values.sort()
            return {
                "field": target.fields()[index].name(),
                "count": len(values),
                "min": values[0],
                "max": values[-1],
                "sum": sum(values),
                "mean": sum(values) / len(values),
                "median": values[len(values) // 2],
            }
        raise ValueError("Unknown vector statistics action")

    def geometry_edit(
        self,
        layer,
        feature_ids,
        action,
        geometry_wkt=None,
        dx=0,
        dy=0,
        angle=0,
        center=None,
        tolerance=0,
    ):
        target = self._vector(layer)
        identifiers = [int(value) for value in feature_ids or []]
        if not identifiers:
            raise ValueError("feature_ids is required")
        if not target.isEditable() and not target.startEditing():
            raise RuntimeError("Layer could not enter edit mode")
        changed = []
        target.beginEditCommand("QGIS MCP geometry {}".format(action))
        try:
            for identifier in identifiers:
                feature = target.getFeature(identifier)
                if not feature.isValid():
                    raise KeyError("Feature not found: {}".format(identifier))
                geometry = QgsGeometry(feature.geometry())
                if action == "set":
                    geometry = QgsGeometry.fromWkt(geometry_wkt or "")
                    if geometry.isNull():
                        raise ValueError("Invalid geometry_wkt")
                elif action == "translate":
                    geometry.translate(float(dx), float(dy))
                elif action == "rotate":
                    origin = (
                        QgsPointXY(float(center[0]), float(center[1]))
                        if isinstance(center, list) and len(center) == 2
                        else geometry.boundingBox().center()
                    )
                    geometry.rotate(float(angle), origin)
                elif action == "simplify":
                    geometry = geometry.simplify(float(tolerance))
                elif action == "make_valid":
                    geometry = geometry.makeValid()
                else:
                    raise ValueError("Unknown geometry edit action")
                if geometry.isNull() or not target.changeGeometry(identifier, geometry):
                    raise RuntimeError("Provider rejected geometry for feature {}".format(identifier))
                changed.append(identifier)
            target.endEditCommand()
        except Exception:
            target.destroyEditCommand()
            raise
        self.state.touch("vector.geometry", {"layer_id": target.id(), "feature_ids": changed})
        return {"layer_id": target.id(), "changed_feature_ids": changed, "editable": target.isEditable()}

    def indexes(self, layer, action="inspect", field=None):
        target = self._vector(layer)
        provider = target.dataProvider()
        if action == "inspect":
            return {
                "layer_id": target.id(),
                "spatial_index_presence": _enum_value(provider.hasSpatialIndex()),
                "provider": target.providerType(),
            }
        if action == "create_spatial":
            ok = bool(provider.createSpatialIndex())
        elif action == "create_attribute":
            ok = bool(provider.createAttributeIndex(self._field_index(target, field)))
        else:
            raise ValueError("Unknown index action")
        if not ok:
            raise RuntimeError("Provider could not create the requested index")
        self.state.touch("vector.index", {"layer_id": target.id(), "action": action})
        return {"layer_id": target.id(), "action": action, "created": True}

    def joins(
        self,
        layer,
        action="list",
        join_layer=None,
        target_field=None,
        join_field=None,
        prefix=None,
        memory_cache=True,
        editable=False,
        upsert=False,
    ):
        target = self._vector(layer)
        if action == "list":
            return {"layer_id": target.id(), "joins": [self._join(item) for item in target.vectorJoins()]}
        if action == "add":
            joined = self._vector(join_layer)
            self._field_index(target, target_field)
            self._field_index(joined, join_field)
            info = QgsVectorLayerJoinInfo()
            info.setJoinLayer(joined)
            info.setJoinLayerId(joined.id())
            info.setTargetFieldName(str(target_field))
            info.setJoinFieldName(str(join_field))
            info.setPrefix(str(prefix)) if prefix is not None else None
            info.setUsingMemoryCache(bool(memory_cache))
            info.setEditable(bool(editable))
            info.setUpsertOnEdit(bool(upsert))
            if not target.addJoin(info):
                raise RuntimeError("Could not add vector join")
        elif action == "remove":
            if not join_layer or not target.removeJoin(str(join_layer)):
                raise KeyError("Vector join not found")
        else:
            raise ValueError("Unknown join action")
        self.state.touch("vector.join", {"layer_id": target.id(), "action": action})
        return {"layer_id": target.id(), "joins": [self._join(item) for item in target.vectorJoins()]}

    def relations(
        self,
        action="list",
        relation_id=None,
        name=None,
        referenced_layer=None,
        referencing_layer=None,
        field_pairs=None,
    ):
        manager = QgsProject.instance().relationManager()
        if action == "list":
            return {"relations": [self._relation(item) for item in manager.relations().values()]}
        if action == "add":
            parent = self._vector(referenced_layer)
            child = self._vector(referencing_layer)
            relation = QgsRelation()
            relation.setId(str(relation_id or "mcp_{}_{}".format(parent.id()[:8], child.id()[:8])))
            relation.setName(str(name or relation.id()))
            relation.setReferencedLayer(parent.id())
            relation.setReferencingLayer(child.id())
            pairs = field_pairs.items() if isinstance(field_pairs, dict) else (field_pairs or [])
            for pair in pairs:
                child_field, parent_field = pair
                self._field_index(child, child_field)
                self._field_index(parent, parent_field)
                relation.addFieldPair(str(child_field), str(parent_field))
            if not relation.isValid():
                raise ValueError("Relation definition is invalid")
            manager.addRelation(relation)
            relation_id = relation.id()
        elif action == "remove":
            if not relation_id or str(relation_id) not in manager.relations():
                raise KeyError("Relation not found")
            manager.removeRelation(str(relation_id))
        else:
            raise ValueError("Unknown relation action")
        self.state.touch("project.relation", {"relation_id": relation_id, "action": action})
        return {"relations": [self._relation(item) for item in manager.relations().values()]}

    def snapping(
        self,
        action="get",
        enabled=None,
        mode=None,
        types=None,
        tolerance=None,
        units=None,
        intersection=None,
        self_snapping=None,
    ):
        project = QgsProject.instance()
        config = project.snappingConfig()
        if action == "get":
            return self._snapping(config)
        if action != "set":
            raise ValueError("Unknown snapping action")
        if enabled is not None:
            config.setEnabled(bool(enabled))
        if mode is not None:
            config.setMode(_snapping_mode(mode))
        if types is not None:
            config.setTypeFlag(_snapping_types(types))
        if tolerance is not None:
            config.setTolerance(float(tolerance))
        if units is not None:
            config.setUnits(_map_tool_unit(units))
        if intersection is not None:
            config.setIntersectionSnapping(bool(intersection))
        if self_snapping is not None and hasattr(config, "setSelfSnapping"):
            config.setSelfSnapping(bool(self_snapping))
        project.setSnappingConfig(config)
        self.state.touch("project.snapping", None)
        return self._snapping(project.snappingConfig())

    def select(self, layer, action, expression=None, feature_ids=None, extent=None, crs=None):
        target = self._vector(layer)
        if action == "all":
            target.selectAll()
        elif action == "invert":
            target.invertSelection()
        elif action == "clear":
            target.removeSelection()
        elif action == "expression":
            if not expression:
                raise ValueError("expression is required")
            target.selectByExpression(str(expression), QgsVectorLayer.SetSelection)
        elif action == "ids":
            target.selectByIds([int(value) for value in feature_ids or []])
        elif action == "rect":
            if not isinstance(extent, list) or len(extent) != 4:
                raise ValueError("extent is required")
            rectangle = QgsRectangle(*extent)
            if crs:
                rectangle = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem(str(crs)), target.crs(), QgsProject.instance()
                ).transformBoundingBox(rectangle)
            target.selectByRect(rectangle, QgsVectorLayer.SetSelection)
        else:
            raise ValueError("Unknown selection action")
        self.state.touch("selection.advanced", {"layer_id": target.id(), "action": action})
        return {
            "layer_id": target.id(),
            "selected_count": target.selectedFeatureCount(),
            "feature_ids": list(target.selectedFeatureIds())[:1000],
        }

    def raster(self, layer, action="inspect", band=1, point=None, crs=None, sample_size=0, bins=256):
        target = self.layer_resolver(layer)
        if not isinstance(target, QgsRasterLayer):
            raise ValueError("Raster layer is required")
        provider = target.dataProvider()
        band = int(band)
        if band < 1 or band > target.bandCount():
            raise ValueError("Invalid raster band")
        if action == "inspect":
            return {
                "layer": layer_summary(target),
                "width": target.width(),
                "height": target.height(),
                "band_count": target.bandCount(),
                "bands": [
                    {
                        "band": index,
                        "name": target.bandName(index),
                        "data_type": _enum_value(provider.dataType(index)),
                        "source_data_type": _enum_value(provider.sourceDataType(index)),
                        "has_nodata": bool(provider.sourceHasNoDataValue(index)),
                        "nodata": provider.sourceNoDataValue(index) if provider.sourceHasNoDataValue(index) else None,
                    }
                    for index in range(1, target.bandCount() + 1)
                ],
                "pixel_size": [target.rasterUnitsPerPixelX(), target.rasterUnitsPerPixelY()],
            }
        if action == "sample":
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("point is required")
            raster_point = QgsPointXY(float(point[0]), float(point[1]))
            if crs:
                raster_point = QgsCoordinateTransform(
                    QgsCoordinateReferenceSystem(str(crs)), target.crs(), QgsProject.instance()
                ).transform(raster_point)
            value, ok = provider.sample(raster_point, band)
            return {"band": band, "value": json_safe(value), "valid": bool(ok), "point": [raster_point.x(), raster_point.y()]}
        if action == "statistics":
            stats = provider.bandStatistics(
                band,
                QgsRasterBandStats.All,
                target.extent(),
                max(0, int(sample_size)),
            )
            return {
                "band": band,
                "count": stats.elementCount,
                "min": stats.minimumValue,
                "max": stats.maximumValue,
                "mean": stats.mean,
                "stddev": stats.stdDev,
                "sum": stats.sum,
                "range": stats.range,
            }
        if action == "histogram":
            histogram = provider.histogram(band, int(bins), target.extent(), max(0, int(sample_size)))
            return {
                "band": band,
                "bin_count": histogram.binCount,
                "minimum": histogram.minimum,
                "maximum": histogram.maximum,
                "counts": list(histogram.histogramVector),
                "valid": bool(histogram.valid),
            }
        raise ValueError("Unknown raster action")

    def _vector(self, reference):
        layer = self.layer_resolver(reference)
        if not isinstance(layer, QgsVectorLayer):
            raise ValueError("Vector layer is required")
        return layer

    @staticmethod
    def _field_index(layer, reference):
        if isinstance(reference, int):
            index = reference
        else:
            index = layer.fields().indexOf(str(reference or ""))
        if index < 0 or index >= len(layer.fields()):
            raise KeyError("Field not found")
        return index

    @staticmethod
    def _schema(layer):
        fields = []
        for index, field in enumerate(layer.fields()):
            constraints = layer.fieldConstraintsAndStrength(index)
            constraint_items = (
                constraints.items() if isinstance(constraints, dict) else constraints
            )
            fields.append(
                {
                    **field_schema(field),
                    "index": index,
                    "alias": layer.attributeAlias(index),
                    "default": layer.defaultValueDefinition(index).expression(),
                    "default_apply_on_update": layer.defaultValueDefinition(index).applyOnUpdate(),
                    "widget": {
                        "type": layer.editorWidgetSetup(index).type(),
                        "config": json_safe(layer.editorWidgetSetup(index).config()),
                    },
                    "constraints": [
                        {"type": _enum_value(flag), "strength": _enum_value(strength)}
                        for flag, strength in constraint_items
                    ],
                }
            )
        return {"layer_id": layer.id(), "editable": layer.isEditable(), "fields": fields}

    @staticmethod
    def _join(info):
        return {
            "join_layer_id": info.joinLayerId(),
            "target_field": info.targetFieldName(),
            "join_field": info.joinFieldName(),
            "prefix": info.prefix(),
            "memory_cache": info.isUsingMemoryCache(),
            "editable": info.isEditable(),
            "upsert": info.hasUpsertOnEdit(),
        }

    @staticmethod
    def _relation(relation):
        return {
            "id": relation.id(),
            "name": relation.name(),
            "valid": relation.isValid(),
            "referenced_layer": relation.referencedLayerId(),
            "referencing_layer": relation.referencingLayerId(),
            "field_pairs": json_safe(relation.fieldPairs()),
        }

    @staticmethod
    def _snapping(config):
        return {
            "enabled": config.enabled(),
            "mode": _enum_value(config.mode()),
            "types": _enum_value(config.typeFlag()),
            "tolerance": config.tolerance(),
            "units": _enum_value(config.units()),
            "intersection": config.intersectionSnapping(),
            "self_snapping": config.selfSnapping() if hasattr(config, "selfSnapping") else None,
        }


def _field_type(value):
    mapping = {
        "string": QVariant.String,
        "text": QVariant.String,
        "integer": QVariant.Int,
        "int": QVariant.Int,
        "integer64": QVariant.LongLong,
        "long": QVariant.LongLong,
        "double": QVariant.Double,
        "float": QVariant.Double,
        "boolean": QVariant.Bool,
        "bool": QVariant.Bool,
        "date": QVariant.Date,
        "datetime": QVariant.DateTime,
        "time": QVariant.Time,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unsupported field type")
    return result


def _constraint(value):
    mapping = {
        "not_null": QgsFieldConstraints.ConstraintNotNull,
        "unique": QgsFieldConstraints.ConstraintUnique,
        "expression": QgsFieldConstraints.ConstraintExpression,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown field constraint")
    return result


def _snapping_mode(value):
    mapping = {
        "active_layer": QgsSnappingConfig.ActiveLayer,
        "all_layers": QgsSnappingConfig.AllLayers,
        "advanced": QgsSnappingConfig.AdvancedConfiguration,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown snapping mode")
    return result


def _snapping_types(values):
    flags = Qgis.SnappingType.NoSnap
    mapping = {
        "vertex": Qgis.SnappingType.Vertex,
        "segment": Qgis.SnappingType.Segment,
        "area": Qgis.SnappingType.Area,
        "centroid": Qgis.SnappingType.Centroid,
        "middle": Qgis.SnappingType.MiddleOfSegment,
        "endpoint": Qgis.SnappingType.LineEndpoint,
    }
    for value in values:
        if str(value).casefold() not in mapping:
            raise ValueError("Unknown snapping type")
        flags |= mapping[str(value).casefold()]
    return flags


def _map_tool_unit(value):
    mapping = {
        "pixels": Qgis.MapToolUnit.Pixels,
        "project": Qgis.MapToolUnit.Project,
        "layer": Qgis.MapToolUnit.Layer,
    }
    result = mapping.get(str(value).casefold())
    if result is None:
        raise ValueError("Unknown snapping unit")
    return result


def _enum_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)
