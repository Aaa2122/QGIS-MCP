from __future__ import annotations

import qgis.utils
from qgis.core import (
    QgsApplication,
    QgsGpsdConnection,
    QgsGpsDetector,
    QgsProject,
    QgsProjectServerValidator,
    QgsSettings,
)
from qgis.gui import QgsGui
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QAction

from .serialize import json_safe

_SECRET_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authcfg",
)


class EcosystemTools:
    def __init__(self, iface, state):
        self.iface = iface
        self.state = state

    def plugins(self, action="list", plugin=None, query="", limit=300):
        if action == "list":
            return self._plugins(query, limit)
        package = str(plugin or "").strip()
        if not package:
            raise ValueError("plugin is required")
        if action in {"disable", "reload"} and package == "qgis_agent_mcp":
            raise ValueError("The active QGIS MCP plugin cannot disable or reload itself")
        if action == "enable":
            if not qgis.utils.loadPlugin(package):
                raise RuntimeError("Could not load plugin {}".format(package))
            if not qgis.utils.startPlugin(package):
                qgis.utils.unloadPlugin(package)
                raise RuntimeError("Could not start plugin {}".format(package))
        elif action == "disable":
            if package not in qgis.utils.active_plugins:
                raise KeyError("Plugin is not active")
            qgis.utils.unloadPlugin(package)
        elif action == "reload":
            if package not in qgis.utils.active_plugins:
                raise KeyError("Plugin is not active")
            qgis.utils.reloadPlugin(package)
        elif action == "refresh_catalog":
            qgis.utils.updateAvailablePlugins()
        elif action == "show_manager":
            self.iface.pluginManagerInterface().showPluginManager()
        else:
            raise ValueError("Unknown plugin action")
        self.state.touch("ecosystem.plugins", {"plugin": package, "action": action})
        return self._plugins(package, limit)

    def settings(
        self,
        action="list",
        key=None,
        value=None,
        prefix="",
        query="",
        include_values=False,
        limit=300,
    ):
        settings = QgsSettings()
        if action == "list":
            prefix = str(prefix or "").strip("/")
            query = str(query or "").casefold()
            keys = []
            for item in settings.allKeys():
                if prefix and not item.startswith(prefix):
                    continue
                if query and query not in item.casefold():
                    continue
                entry = {"key": item}
                if include_values:
                    entry.update(_setting_value(item, settings.value(item)))
                keys.append(entry)
                if len(keys) >= max(1, min(int(limit), 2000)):
                    break
            return {"settings": keys, "count": len(keys), "prefix": prefix}
        normalized = str(key or "").strip("/")
        if not normalized:
            raise ValueError("key is required")
        if action == "get":
            return {"key": normalized, **_setting_value(normalized, settings.value(normalized))}
        if action == "set":
            settings.setValue(normalized, value)
        elif action == "remove":
            settings.remove(normalized)
        else:
            raise ValueError("Unknown settings action")
        settings.sync()
        self.state.touch("ecosystem.settings", {"key": normalized, "action": action})
        return {
            "key": normalized,
            "action": action,
            "present": settings.contains(normalized),
            **(_setting_value(normalized, settings.value(normalized)) if action == "set" else {}),
        }

    def shortcuts(self, action="list", name=None, sequence=None, query="", limit=300):
        manager = QgsGui.shortcutsManager()
        if manager is None:
            raise RuntimeError("QGIS shortcuts manager is unavailable")
        if action == "list":
            query = str(query or "").casefold()
            items = []
            for obj in manager.listAll():
                item = _shortcut(obj, manager)
                haystack = "{} {} {}".format(item["name"], item["text"], item["sequence"])
                if query and query not in haystack.casefold():
                    continue
                items.append(item)
                if len(items) >= max(1, min(int(limit), 1000)):
                    break
            return {"shortcuts": items, "count": len(items)}
        target = manager.objectForSettingKey(str(name or ""))
        if target is None:
            target = manager.actionByName(str(name or "")) or manager.shortcutByName(
                str(name or "")
            )
        if target is None:
            raise KeyError("Shortcut action not found")
        if action == "set":
            if sequence is None:
                raise ValueError("sequence is required")
            ok = manager.setObjectKeySequence(target, str(sequence))
        elif action == "clear":
            ok = manager.setObjectKeySequence(target, "")
        elif action == "reset":
            ok = manager.setObjectKeySequence(target, manager.objectDefaultKeySequence(target))
        else:
            raise ValueError("Unknown shortcut action")
        if not ok:
            raise RuntimeError("QGIS rejected the shortcut")
        self.state.touch("ecosystem.shortcuts", {"name": name, "action": action})
        return _shortcut(target, manager)

    def gps(
        self,
        action="status",
        connection_index=None,
        host="127.0.0.1",
        port=2947,
        device="",
    ):
        registry = QgsApplication.gpsConnectionRegistry()
        if registry is None:
            raise RuntimeError("QGIS GPS connection registry is unavailable")
        if action in {"status", "ports"}:
            return self._gps(registry, include_ports=action == "ports")
        if action == "connect_gpsd":
            connection = QgsGpsdConnection(str(host), int(port), str(device or ""))
            registry.registerConnection(connection)
            if not connection.connect():
                registry.unregisterConnection(connection)
                raise RuntimeError("Could not connect to gpsd")
        else:
            connections = registry.connectionList()
            if connection_index is None:
                raise ValueError("connection_index is required")
            index = int(connection_index)
            if index < 0 or index >= len(connections):
                raise KeyError("GPS connection not found")
            connection = connections[index]
            if action == "connect":
                if not connection.connect():
                    raise RuntimeError("GPS connection failed")
            elif action == "disconnect":
                connection.close()
            elif action == "unregister":
                connection.close()
                registry.unregisterConnection(connection)
            else:
                raise ValueError("Unknown GPS action")
        self.state.touch("ecosystem.gps", {"action": action})
        return self._gps(registry)

    def views_3d(
        self,
        action="list",
        name=None,
        background_color=None,
        show_labels=None,
        field_of_view=None,
        movement_speed=None,
        terrain_enabled=None,
        skybox_enabled=None,
        eye_dome_lighting=None,
        layers=None,
    ):
        canvases = list(self.iface.mapCanvases3D())
        if action == "list":
            return {"views": [self._view_3d(item) for item in canvases]}
        if action == "create":
            if not name:
                raise ValueError("name is required")
            if any(_canvas_name(item) == str(name) for item in canvases):
                raise ValueError("A 3D view with this name already exists")
            canvas = self.iface.createNewMapCanvas3D(str(name))
            if canvas is None:
                raise RuntimeError("Could not create the 3D map view")
        else:
            canvas = next((item for item in canvases if _canvas_name(item) == str(name)), None)
            if canvas is None:
                raise KeyError("3D view not found")
            if action == "close":
                canvas.close()
                self.state.touch("ecosystem.3d", {"name": name, "action": action})
                return {"closed": str(name)}
            if action != "configure":
                raise ValueError("Unknown 3D view action")
        settings = canvas.mapSettings()
        if settings is None:
            raise RuntimeError("3D map settings are unavailable")
        if background_color is not None:
            settings.setBackgroundColor(QColor(str(background_color)))
        if show_labels is not None:
            settings.setShowLabels(bool(show_labels))
        if field_of_view is not None:
            settings.setFieldOfView(float(field_of_view))
        if movement_speed is not None:
            settings.setCameraMovementSpeed(float(movement_speed))
        if terrain_enabled is not None:
            settings.setTerrainRenderingEnabled(bool(terrain_enabled))
        if skybox_enabled is not None:
            settings.setIsSkyboxEnabled(bool(skybox_enabled))
        if eye_dome_lighting is not None:
            settings.setEyeDomeLightingEnabled(bool(eye_dome_lighting))
        if layers is not None:
            settings.setLayers([self._layer(reference) for reference in layers])
        self.state.touch("ecosystem.3d", {"name": _canvas_name(canvas), "action": action})
        return self._view_3d(canvas)

    def server(
        self,
        action="validate",
        layer=None,
        short_name=None,
        title=None,
        wfs_title=None,
        abstract=None,
        keywords=None,
        attribution=None,
        attribution_url=None,
        data_url=None,
        data_url_format=None,
        legend_url=None,
        legend_url_format=None,
    ):
        project = QgsProject.instance()
        if action == "validate":
            valid, results = QgsProjectServerValidator.validate(project)
            return {
                "valid": bool(valid),
                "issues": [
                    {
                        "error": int(item.error),
                        "message": QgsProjectServerValidator.displayValidationError(item.error),
                        "identifier": json_safe(item.identifier),
                    }
                    for item in results
                ],
            }
        target = self._layer(layer)
        properties = target.serverProperties()
        if properties is None:
            raise RuntimeError("Layer server properties are unavailable")
        if action == "inspect_layer":
            return self._server_layer(target, properties)
        if action != "set_layer":
            raise ValueError("Unknown QGIS Server action")
        setters = {
            "setShortName": short_name,
            "setTitle": title,
            "setWfsTitle": wfs_title,
            "setAbstract": abstract,
            "setKeywordList": keywords,
            "setAttribution": attribution,
            "setAttributionUrl": attribution_url,
            "setDataUrl": data_url,
            "setDataUrlFormat": data_url_format,
            "setLegendUrl": legend_url,
            "setLegendUrlFormat": legend_url_format,
        }
        for method, value in setters.items():
            if value is not None:
                getattr(properties, method)(str(value))
        self.state.touch("ecosystem.server", {"layer_id": target.id()})
        return self._server_layer(target, properties)

    def offline(self, action="status", action_name=None):
        actions = []
        for candidate in self.iface.mainWindow().findChildren(QAction):
            text = candidate.text().replace("&", "")
            name = candidate.objectName()
            haystack = "{} {}".format(name, text).casefold()
            if any(term in haystack for term in ("offline", "synchron", "hors ligne")):
                actions.append(
                    {
                        "name": name,
                        "text": text,
                        "enabled": candidate.isEnabled(),
                        "checked": candidate.isChecked(),
                    }
                )
        if action == "status":
            return {
                "project_path": QgsProject.instance().fileName(),
                "actions": actions,
                "available": bool(actions),
            }
        if action != "trigger":
            raise ValueError("Unknown offline action")
        selected = next((item for item in actions if item["name"] == str(action_name)), None)
        if selected is None:
            raise KeyError("Offline action not found")
        action_object = self.iface.mainWindow().findChild(QAction, selected["name"])
        if action_object is None or not action_object.isEnabled():
            raise RuntimeError("Offline action is unavailable")
        action_object.trigger()
        self.state.touch("ecosystem.offline", {"action_name": action_name})
        return {"triggered": selected}

    def _layer(self, reference):
        project = QgsProject.instance()
        if reference in project.mapLayers():
            return project.mapLayer(reference)
        matches = project.mapLayersByName(str(reference or ""))
        if len(matches) != 1:
            raise KeyError("Layer reference is missing or ambiguous")
        return matches[0]

    @staticmethod
    def _plugins(query, limit):
        qgis.utils.updateAvailablePlugins()
        packages = sorted(set(qgis.utils.available_plugins) | set(qgis.utils.plugins))
        query = str(query or "").casefold()
        items = []
        for package in packages:
            metadata = {
                key: qgis.utils.pluginMetadata(package, key)
                for key in ("name", "description", "version", "author", "category", "experimental")
            }
            haystack = "{} {} {}".format(package, metadata["name"], metadata["description"])
            if query and query not in haystack.casefold():
                continue
            items.append(
                {
                    "package": package,
                    "loaded": package in qgis.utils.plugins,
                    "active": package in qgis.utils.active_plugins,
                    "metadata": metadata,
                }
            )
            if len(items) >= max(1, min(int(limit), 1000)):
                break
        return {"plugins": items, "count": len(items)}

    @staticmethod
    def _gps(registry, include_ports=False):
        connections = []
        for index, connection in enumerate(registry.connectionList()):
            info = connection.currentGPSInformation()
            location = connection.lastValidLocation()
            connections.append(
                {
                    "index": index,
                    "class": type(connection).__name__,
                    "status": int(connection.status()),
                    "valid": bool(info.isValid()),
                    "fix_mode": info.fixMode,
                    "fix_type": info.fixType,
                    "quality": info.qualityDescription(),
                    "satellites_used": info.satellitesUsed,
                    "longitude": location.x(),
                    "latitude": location.y(),
                    "elevation": info.elevation,
                    "speed": info.speed,
                    "direction": info.direction,
                }
            )
        result = {"connections": connections, "count": len(connections)}
        if include_ports:
            result["ports"] = [
                {"port": port, "description": description}
                for port, description in QgsGpsDetector.availablePorts()
            ]
        return result

    @staticmethod
    def _view_3d(canvas):
        settings = canvas.mapSettings()
        extent = settings.extent()
        return {
            "name": _canvas_name(canvas),
            "background_color": settings.backgroundColor().name(QColor.HexArgb),
            "show_labels": settings.showLabels(),
            "field_of_view": settings.fieldOfView(),
            "movement_speed": settings.cameraMovementSpeed(),
            "terrain_enabled": settings.terrainRenderingEnabled(),
            "skybox_enabled": settings.isSkyboxEnabled(),
            "eye_dome_lighting": settings.eyeDomeLightingEnabled(),
            "layers": [layer.id() for layer in settings.layers()],
            "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
        }

    @staticmethod
    def _server_layer(layer, properties):
        return {
            "layer_id": layer.id(),
            "short_name": properties.shortName(),
            "title": properties.title(),
            "wfs_title": properties.wfsTitle(),
            "abstract": properties.abstract(),
            "keywords": properties.keywordList(),
            "attribution": properties.attribution(),
            "attribution_url": properties.attributionUrl(),
            "data_url": properties.dataUrl(),
            "data_url_format": properties.dataUrlFormat(),
            "legend_url": properties.legendUrl(),
            "legend_url_format": properties.legendUrlFormat(),
        }


def _setting_value(key, value):
    lowered = str(key).casefold()
    if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
        return {"value": None, "redacted": True, "type": type(value).__name__}
    return {"value": json_safe(value), "redacted": False, "type": type(value).__name__}


def _shortcut(obj, manager):
    sequence = ""
    if hasattr(obj, "shortcut"):
        sequence = obj.shortcut().toString()
    elif hasattr(obj, "key"):
        sequence = obj.key().toString()
    return {
        "name": manager.objectSettingKey(obj),
        "object_name": obj.objectName(),
        "text": obj.text().replace("&", "") if hasattr(obj, "text") else "",
        "sequence": sequence,
        "enabled": obj.isEnabled() if hasattr(obj, "isEnabled") else True,
    }


def _canvas_name(canvas):
    for method in ("name", "windowTitle", "objectName"):
        candidate = getattr(canvas, method, None)
        if callable(candidate):
            value = candidate()
            if value:
                return str(value)
    return "3D view"
