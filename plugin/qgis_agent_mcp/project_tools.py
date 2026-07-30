from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from qgis.core import (
    QgsBookmark,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDistanceArea,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsGeometry,
    QgsMapThemeCollection,
    QgsPointXY,
    QgsProject,
    QgsProviderRegistry,
    QgsRaster,
    QgsRectangle,
    QgsReferencedRectangle,
    QgsUnitTypes,
    QgsVectorLayer,
)

from .serialize import feature_summary, json_safe, layer_summary


class ProjectTools:
    def __init__(self, iface, state, layer_resolver):
        self.iface = iface
        self.state = state
        self.layer_resolver = layer_resolver
        self._crs_cache = None

    def project(self, action="status", path=None, save_changes=False):
        project = QgsProject.instance()
        if action == "status":
            return self._project_status(project)
        if action == "new":
            if save_changes and project.isDirty() and not project.write():
                raise RuntimeError("Could not save the current project")
            project.clear()
        elif action == "open":
            if not path:
                raise ValueError("path is required")
            if save_changes and project.isDirty() and not project.write():
                raise RuntimeError("Could not save the current project")
            if not project.read(str(Path(path).expanduser())):
                raise ValueError("QGIS could not open the project")
        elif action in {"save", "save_as"}:
            if action == "save_as" and not path:
                raise ValueError("path is required")
            ok = project.write(str(Path(path).expanduser())) if path else project.write()
            if not ok:
                raise RuntimeError("QGIS could not save the project")
        elif action == "close":
            if save_changes and project.isDirty() and not project.write():
                raise RuntimeError("Could not save the current project")
            project.clear()
        else:
            raise ValueError("Unknown project management action")
        self.state.touch("project.{}".format(action), {"path": path})
        return self._project_status(project)

    def project_properties(
        self,
        action="get",
        title=None,
        home_path=None,
        crs=None,
        ellipsoid=None,
        variables=None,
    ):
        project = QgsProject.instance()
        if action == "get":
            return self._project_properties(project)
        if action != "set":
            raise ValueError("Unknown project properties action")
        if title is not None:
            project.setTitle(str(title))
        if home_path is not None:
            project.setPresetHomePath(str(Path(home_path).expanduser()))
        if crs is not None:
            target_crs = _crs(crs)
            project.setCrs(target_crs)
        if ellipsoid is not None:
            project.setEllipsoid(str(ellipsoid))
        if variables is not None:
            if not isinstance(variables, dict):
                raise ValueError("variables must be an object")
            project.setCustomVariables(dict(variables))
        self.state.touch("project.properties", None)
        return self._project_properties(project)

    def repair(self, action="inspect", repairs=None):
        project = QgsProject.instance()
        broken = [
            {
                "layer_id": layer.id(),
                "name": layer.name(),
                "provider": layer.providerType(),
                "source": _redact_source(layer.source()),
            }
            for layer in project.mapLayers().values()
            if not layer.isValid() or _missing_local_source(layer)
        ]
        if action == "inspect":
            return {"broken_layers": broken, "count": len(broken)}
        if action != "apply":
            raise ValueError("Unknown project repair action")
        if not isinstance(repairs, list) or not repairs:
            raise ValueError("repairs must be a non-empty array")
        results = []
        for repair in repairs:
            layer = self.layer_resolver(repair.get("layer"))
            source = repair.get("source")
            if not source:
                raise ValueError("Every repair requires source")
            provider = repair.get("provider") or layer.providerType()
            name = repair.get("name") or layer.name()
            layer.setDataSource(str(source), str(name), str(provider))
            results.append({**layer_summary(layer), "valid": layer.isValid()})
        self.state.touch("project.repaired", {"layer_ids": [item["id"] for item in results]})
        return {"repairs": results, "all_valid": all(item["valid"] for item in results)}

    def source(
        self,
        layer,
        action="inspect",
        source=None,
        provider=None,
        name=None,
        subset=None,
    ):
        target = self.layer_resolver(layer)
        if action == "inspect":
            dependencies = getattr(target, "dependencies", lambda: set())()
            return {
                "layer": layer_summary(target),
                "source": _redact_source(target.source()),
                "provider": target.providerType(),
                "valid": target.isValid(),
                "read_only": bool(_optional_call(target, "readOnly", "isReadOnly", default=False)),
                "subset": target.subsetString() if hasattr(target, "subsetString") else None,
                "dependencies": [str(item) for item in dependencies],
            }
        if action == "rebind":
            if not source:
                raise ValueError("source is required")
            target.setDataSource(str(source), name or target.name(), provider or target.providerType())
        elif action == "reload":
            target.reload()
        elif action == "set_subset":
            if not hasattr(target, "setSubsetString"):
                raise ValueError("Layer provider does not support subset strings")
            if not target.setSubsetString(subset or ""):
                raise ValueError("Provider rejected the subset string")
        else:
            raise ValueError("Unknown source action")
        target.triggerRepaint()
        self.state.touch("layer.source", {"layer_id": target.id(), "action": action})
        return {"layer": layer_summary(target), "valid": target.isValid(), "source": _redact_source(target.source())}

    def canvas(
        self,
        action="status",
        view=None,
        extent=None,
        crs=None,
        scale=None,
        rotation=None,
        center=None,
    ):
        if action == "list_views":
            return {"views": [self._canvas_status(canvas) for canvas in self._canvases()]}
        canvas = self._canvas(view)
        if action == "status":
            return self._canvas_status(canvas)
        if action == "set_extent":
            if not isinstance(extent, list) or len(extent) != 4:
                raise ValueError("extent must contain xmin, ymin, xmax, ymax")
            rectangle = QgsRectangle(*[float(value) for value in extent])
            if crs:
                rectangle = QgsCoordinateTransform(
                    _crs(crs), canvas.mapSettings().destinationCrs(), QgsProject.instance()
                ).transformBoundingBox(rectangle)
            canvas.setExtent(rectangle)
        elif action == "set_center":
            if not isinstance(center, list) or len(center) != 2:
                raise ValueError("center must contain x and y")
            point = QgsPointXY(float(center[0]), float(center[1]))
            if crs:
                point = QgsCoordinateTransform(
                    _crs(crs), canvas.mapSettings().destinationCrs(), QgsProject.instance()
                ).transform(point)
            canvas.setCenter(point)
        elif action == "set_scale":
            if scale is None or float(scale) <= 0:
                raise ValueError("scale must be positive")
            canvas.zoomScale(float(scale))
        elif action == "set_rotation":
            canvas.setRotation(float(rotation or 0))
        elif action == "set_crs":
            if not crs:
                raise ValueError("crs is required")
            canvas.setDestinationCrs(_crs(crs))
        elif action == "zoom_full":
            canvas.zoomToFullExtent()
        elif action == "zoom_selected":
            layer = self.iface.activeLayer()
            if not isinstance(layer, QgsVectorLayer) or not layer.selectedFeatureCount():
                raise ValueError("The active vector layer has no selection")
            canvas.zoomToSelected(layer)
        elif action == "refresh":
            canvas.refresh()
        else:
            raise ValueError("Unknown canvas action")
        self.state.touch("canvas.{}".format(action), {"view": view})
        return self._canvas_status(canvas)

    def identify(self, point, crs=None, layers=None, tolerance=0.0, limit_per_layer=20):
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("point must contain x and y")
        source_crs = _crs(crs) if crs else self.iface.mapCanvas().mapSettings().destinationCrs()
        selected_layers = (
            [self.layer_resolver(item) for item in layers]
            if layers
            else list(QgsProject.instance().mapLayers().values())
        )
        results = []
        for layer in selected_layers:
            transform = QgsCoordinateTransform(source_crs, layer.crs(), QgsProject.instance())
            layer_point = transform.transform(QgsPointXY(float(point[0]), float(point[1])))
            if isinstance(layer, QgsVectorLayer):
                tol = max(0.0, float(tolerance))
                rectangle = QgsRectangle(
                    layer_point.x() - tol,
                    layer_point.y() - tol,
                    layer_point.x() + tol,
                    layer_point.y() + tol,
                )
                request = QgsFeatureRequest().setFilterRect(rectangle)
                request.setFlags(QgsFeatureRequest.Flag.ExactIntersect)
                fields = list(layer.fields())
                features = []
                for feature in layer.getFeatures(request):
                    features.append(feature_summary(feature, fields, True))
                    if len(features) >= min(int(limit_per_layer), 100):
                        break
                if features:
                    results.append({"layer": layer_summary(layer), "features": features})
            elif layer.dataProvider() is not None:
                identified = layer.dataProvider().identify(
                    layer_point, QgsRaster.IdentifyFormat.IdentifyFormatValue
                )
                if identified.isValid():
                    results.append(
                        {
                            "layer": layer_summary(layer),
                            "values": json_safe(identified.results()),
                        }
                    )
        return {"point": point, "crs": source_crs.authid(), "results": results}

    def measure(self, action, geometry_wkt=None, points=None, crs=None, ellipsoid=None):
        distance = QgsDistanceArea()
        project = QgsProject.instance()
        source_crs = _crs(crs) if crs else project.crs()
        distance.setSourceCrs(source_crs, project.transformContext())
        distance.setEllipsoid(ellipsoid or project.ellipsoid() or "WGS84")
        if action == "bearing":
            if not isinstance(points, list) or len(points) != 2:
                raise ValueError("bearing requires two points")
            first = QgsPointXY(*[float(value) for value in points[0]])
            second = QgsPointXY(*[float(value) for value in points[1]])
            return {"action": action, "radians": distance.bearing(first, second), "crs": source_crs.authid()}
        if not geometry_wkt:
            raise ValueError("geometry_wkt is required")
        geometry = QgsGeometry.fromWkt(geometry_wkt)
        if geometry.isNull():
            raise ValueError("Invalid WKT geometry")
        if action == "length":
            value = distance.measureLength(geometry)
            units = distance.lengthUnits()
        elif action == "perimeter":
            value = distance.measurePerimeter(geometry)
            units = distance.lengthUnits()
        elif action == "area":
            value = distance.measureArea(geometry)
            units = distance.areaUnits()
        else:
            raise ValueError("Unknown measurement action")
        return {"action": action, "value": value, "unit": _unit_name(units), "crs": source_crs.authid()}

    def bookmarks(self, action="list", bookmark_id=None, name=None, extent=None, crs=None, group=None, rotation=0):
        manager = QgsProject.instance().bookmarkManager()
        if action == "list":
            return {"bookmarks": [self._bookmark(item) for item in manager.bookmarks()]}
        if action == "add":
            if not name or not isinstance(extent, list) or len(extent) != 4:
                raise ValueError("name and extent are required")
            bookmark = QgsBookmark()
            bookmark.setName(str(name))
            bookmark.setGroup(str(group or ""))
            bookmark.setRotation(float(rotation))
            bookmark.setExtent(QgsReferencedRectangle(QgsRectangle(*extent), _crs(crs or QgsProject.instance().crs())))
            identifier, ok = manager.addBookmark(bookmark)
            if not ok:
                raise RuntimeError("Could not add bookmark")
            self.state.touch("bookmark.added", {"id": identifier})
            return self._bookmark(manager.bookmarkById(identifier))
        if action == "remove":
            if not bookmark_id or not manager.removeBookmark(str(bookmark_id)):
                raise KeyError("Bookmark not found")
            self.state.touch("bookmark.removed", {"id": bookmark_id})
            return {"removed": True, "id": str(bookmark_id)}
        if action == "zoom":
            bookmark = manager.bookmarkById(str(bookmark_id))
            if not bookmark.id():
                raise KeyError("Bookmark not found")
            referenced = bookmark.extent()
            target = QgsCoordinateTransform(
                referenced.crs(), self.iface.mapCanvas().mapSettings().destinationCrs(), QgsProject.instance()
            ).transformBoundingBox(referenced)
            self.iface.mapCanvas().setExtent(target)
            self.iface.mapCanvas().setRotation(bookmark.rotation())
            self.iface.mapCanvas().refresh()
            return self._bookmark(bookmark)
        raise ValueError("Unknown bookmark action")

    def themes(self, action="list", name=None):
        project = QgsProject.instance()
        collection = project.mapThemeCollection()
        if action == "list":
            return {
                "themes": [
                    {"name": item, "visible_layer_ids": list(collection.mapThemeVisibleLayerIds(item))}
                    for item in collection.mapThemes()
                ]
            }
        if not name:
            raise ValueError("name is required")
        if action == "capture":
            model = self.iface.layerTreeView().layerTreeModel()
            record = QgsMapThemeCollection.createThemeFromCurrentState(project.layerTreeRoot(), model)
            collection.insert(str(name), record)
        elif action == "apply":
            if not collection.hasMapTheme(str(name)):
                raise KeyError("Map theme not found")
            self.iface.mapCanvas().setTheme(str(name))
        elif action == "remove":
            if not collection.hasMapTheme(str(name)):
                raise KeyError("Map theme not found")
            collection.removeMapTheme(str(name))
        else:
            raise ValueError("Unknown map theme action")
        self.state.touch("map_theme.{}".format(action), {"name": name})
        return {"name": str(name), "action": action}

    def crs(self, action="describe", value=None, query=None, limit=20, source=None, target=None, points=None, extent=None, geometry_wkt=None, layer=None):
        if action == "describe":
            return self._crs_summary(_crs(value))
        if action == "search":
            query = str(query or value or "").casefold()
            values = self._all_crs()
            matches = [item for item in values if query in item["search"]]
            return {"items": [{key: value for key, value in item.items() if key != "search"} for item in matches[: min(int(limit), 100)]]}
        if action == "assign_layer":
            target_layer = self.layer_resolver(layer)
            target_layer.setCrs(_crs(target))
            self.state.touch("layer.crs", {"layer_id": target_layer.id()})
            return layer_summary(target_layer)
        transform = QgsCoordinateTransform(_crs(source), _crs(target), QgsProject.instance())
        if action == "transform_points":
            if not isinstance(points, list):
                raise ValueError("points is required")
            converted = []
            for item in points[:10000]:
                point = transform.transform(QgsPointXY(float(item[0]), float(item[1])))
                converted.append([point.x(), point.y()])
            return {"points": converted, "source": _crs(source).authid(), "target": _crs(target).authid()}
        if action == "transform_extent":
            if not isinstance(extent, list) or len(extent) != 4:
                raise ValueError("extent is required")
            rectangle = transform.transformBoundingBox(QgsRectangle(*extent))
            return {"extent": [rectangle.xMinimum(), rectangle.yMinimum(), rectangle.xMaximum(), rectangle.yMaximum()]}
        if action == "transform_geometry":
            geometry = QgsGeometry.fromWkt(geometry_wkt or "")
            if geometry.isNull():
                raise ValueError("geometry_wkt is invalid")
            geometry.transform(transform)
            return {"geometry_wkt": geometry.asWkt(), "target": _crs(target).authid()}
        raise ValueError("Unknown CRS action")

    def expression(self, action="validate", expression=None, layer=None, feature_id=None, limit=100):
        if action == "functions":
            functions = []
            for function in QgsExpression.Functions():
                functions.append(
                    {
                        "name": function.name(),
                        "groups": list(function.groups()),
                        "params": json_safe(function.params()),
                    }
                )
            return {"functions": functions[: min(int(limit), 1000)], "count": len(functions)}
        if not expression:
            raise ValueError("expression is required")
        parsed = QgsExpression(str(expression))
        result = {
            "valid": not parsed.hasParserError(),
            "parser_error": parsed.parserErrorString() or None,
            "referenced_columns": sorted(parsed.referencedColumns()),
        }
        if action == "validate" or not result["valid"]:
            return result
        if action != "evaluate":
            raise ValueError("Unknown expression action")
        context = QgsExpressionContext()
        context.appendScope(QgsExpressionContextUtils.globalScope())
        context.appendScope(QgsExpressionContextUtils.projectScope(QgsProject.instance()))
        target_layer = self.layer_resolver(layer) if layer else None
        if target_layer is not None:
            context.appendScope(QgsExpressionContextUtils.layerScope(target_layer))
            if feature_id is not None and isinstance(target_layer, QgsVectorLayer):
                feature = target_layer.getFeature(int(feature_id))
                if not feature.isValid():
                    raise KeyError("Feature not found")
                context.setFeature(feature)
        value = parsed.evaluate(context)
        result.update(
            {
                "value": json_safe(value),
                "evaluation_error": parsed.evalErrorString() or None,
                "valid": not parsed.hasEvalError(),
            }
        )
        return result

    def metadata(self, action="get", layer=None, values=None):
        project = QgsProject.instance()
        target = self.layer_resolver(layer) if layer else None
        metadata = target.metadata() if target is not None else project.metadata()
        if action == "get":
            return self._metadata_summary(metadata, target)
        if action != "set" or not isinstance(values, dict):
            raise ValueError("values is required for metadata set")
        setters = {
            "title": "setTitle",
            "identifier": "setIdentifier",
            "abstract": "setAbstract",
            "language": "setLanguage",
            "type": "setType",
            "categories": "setCategories",
            "history": "setHistory",
        }
        for key, value in values.items():
            method = setters.get(key)
            if method is None:
                raise ValueError("Unsupported metadata field: {}".format(key))
            getattr(metadata, method)(value)
        if target is not None:
            target.setMetadata(metadata)
            resource = {"layer_id": target.id()}
        else:
            project.setMetadata(metadata)
            resource = {"project": True}
        self.state.touch("metadata.updated", resource)
        return self._metadata_summary(metadata, target)

    def connections(self, action="list", provider=None, name=None):
        registry = QgsProviderRegistry.instance()
        if action == "providers":
            return {"providers": sorted(registry.providerList())}
        provider_ids = [str(provider)] if provider else registry.providerList()
        results = []
        for provider_id in provider_ids:
            metadata = registry.providerMetadata(provider_id)
            if metadata is None or not hasattr(metadata, "connections"):
                continue
            try:
                connections = metadata.connections(False)
            except Exception:
                connections = {}
            for connection_name, connection in connections.items():
                results.append(
                    {
                        "provider": provider_id,
                        "name": str(connection_name),
                        "type": type(connection).__name__,
                        "capabilities": _enum_value(_optional_call(connection, "capabilities")),
                    }
                )
        if action == "list":
            return {"connections": results}
        if action == "describe":
            match = next(
                (
                    item
                    for item in results
                    if item["provider"] == str(provider) and item["name"] == str(name)
                ),
                None,
            )
            if match is None:
                raise KeyError("Provider connection not found")
            return match
        raise ValueError("Unknown connection action")

    @staticmethod
    def _project_status(project):
        return {
            "file": project.fileName() or None,
            "title": project.title(),
            "dirty": project.isDirty(),
            "layer_count": len(project.mapLayers()),
            "crs": project.crs().authid() if project.crs().isValid() else None,
        }

    @staticmethod
    def _project_properties(project):
        temporal = _optional_call(project, "timeSettings")
        return {
            "title": project.title(),
            "file": project.fileName() or None,
            "home_path": project.homePath(),
            "preset_home_path": project.presetHomePath(),
            "crs": ProjectTools._crs_summary(project.crs()),
            "vertical_crs": ProjectTools._crs_summary(project.verticalCrs()) if hasattr(project, "verticalCrs") and project.verticalCrs().isValid() else None,
            "ellipsoid": project.ellipsoid(),
            "variables": json_safe(project.customVariables()),
            "transaction_mode": _enum_value(_optional_call(project, "transactionMode")),
            "temporal_settings_available": temporal is not None,
        }

    def _canvases(self):
        canvases = list(getattr(self.iface, "mapCanvases", lambda: [])())
        main = self.iface.mapCanvas()
        return canvases if main in canvases else [main, *canvases]

    def _canvas(self, reference):
        if reference is None or reference in {"main", "main_map"}:
            return self.iface.mapCanvas()
        for canvas in self._canvases():
            if canvas.objectName() == str(reference) or canvas.windowTitle() == str(reference):
                return canvas
        raise KeyError("Map view not found")

    @staticmethod
    def _canvas_status(canvas):
        extent = canvas.extent()
        return {
            "id": canvas.objectName() or "main",
            "title": canvas.windowTitle(),
            "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
            "center": [extent.center().x(), extent.center().y()],
            "scale": canvas.scale(),
            "rotation": canvas.rotation(),
            "crs": canvas.mapSettings().destinationCrs().authid(),
            "drawing": canvas.isDrawing(),
            "layer_ids": [layer.id() for layer in canvas.layers()],
        }

    @staticmethod
    def _bookmark(bookmark):
        extent = bookmark.extent()
        return {
            "id": bookmark.id(),
            "name": bookmark.name(),
            "group": bookmark.group(),
            "rotation": bookmark.rotation(),
            "crs": extent.crs().authid(),
            "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
        }

    def _all_crs(self):
        if self._crs_cache is None:
            items = []
            for srs_id in QgsCoordinateReferenceSystem.validSrsIds():
                crs = QgsCoordinateReferenceSystem.fromSrsId(srs_id)
                if not crs.isValid():
                    continue
                summary = self._crs_summary(crs)
                summary["search"] = "{} {} {}".format(summary["authid"], summary["description"], summary["projection"]).casefold()
                items.append(summary)
            self._crs_cache = items
        return self._crs_cache

    @staticmethod
    def _crs_summary(crs):
        return {
            "authid": crs.authid(),
            "description": crs.description(),
            "srs_id": crs.srsid(),
            "geographic": crs.isGeographic(),
            "units": _unit_name(crs.mapUnits()),
            "projection": crs.projectionAcronym(),
            "ellipsoid": crs.ellipsoidAcronym(),
            "wkt": crs.toWkt() if crs.isValid() else None,
            "valid": crs.isValid(),
        }

    @staticmethod
    def _metadata_summary(metadata, layer):
        result = {
            "scope": "layer" if layer is not None else "project",
            "title": metadata.title(),
            "identifier": metadata.identifier(),
            "abstract": metadata.abstract(),
            "language": metadata.language(),
            "type": metadata.type(),
            "categories": list(metadata.categories()),
            "history": list(metadata.history()),
        }
        if layer is not None:
            result["layer_id"] = layer.id()
            result["licenses"] = list(metadata.licenses())
            result["rights"] = list(metadata.rights())
        return result


def _crs(value):
    if isinstance(value, QgsCoordinateReferenceSystem):
        crs = value
    elif isinstance(value, int) or str(value or "").isdigit():
        crs = QgsCoordinateReferenceSystem.fromEpsgId(int(value))
    else:
        text = str(value or "")
        crs = QgsCoordinateReferenceSystem(text)
        if not crs.isValid() and text:
            crs.createFromWkt(text)
    if not crs.isValid():
        raise ValueError("Invalid coordinate reference system")
    return crs


def _unit_name(value):
    try:
        return QgsUnitTypes.toString(value)
    except Exception:
        return str(value)


def _optional_call(target, *names, default=None):
    for name in names:
        value = getattr(target, name, None)
        if callable(value):
            try:
                return value()
            except (RuntimeError, TypeError):
                continue
    return default


def _enum_value(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _missing_local_source(layer):
    if layer.providerType() not in {"ogr", "gdal", "delimitedtext"}:
        return False
    source = str(layer.source()).split("|", 1)[0].removeprefix("file://")
    return bool(source and os.path.isabs(source) and not Path(source).exists())


def _redact_source(source):
    text = str(source or "")
    try:
        parts = urlsplit(text)
        if parts.scheme in {"http", "https"}:
            query = []
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                query.append((key, "***" if key.casefold() in {"token", "key", "password", "apikey", "api_key"} else value))
            host = parts.hostname or ""
            if parts.port:
                host += ":{}".format(parts.port)
            return urlunsplit((parts.scheme, host, parts.path, urlencode(query), ""))
    except ValueError:
        pass
    lowered = text.casefold()
    if "password=" in lowered:
        pieces = []
        for token in text.split():
            pieces.append("password='***'" if token.casefold().startswith("password=") else token)
        return " ".join(pieces)
    return text
