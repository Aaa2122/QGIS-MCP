from __future__ import annotations

import re
import sys
import time

import qgis.utils
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsGpsdConnection,
    QgsGpsDetector,
    QgsProject,
    QgsProjectServerValidator,
    QgsSettings,
)
from qgis.gui import QgsGui
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QMetaObject, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import QAction, QApplication

from .compat import unsafe_python_3d_creation
from .serialize import json_safe


def _bounded_text(value, limit):
    compact = " ".join(str(value or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"

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
        self._3d_aliases = {}
        self._3d_pending = {}
        self._3d_native_views = {}

    def plugins(
        self,
        action="list",
        plugin=None,
        query="",
        limit=300,
        confirmed=False,
        expected_version=None,
        experimental=False,
        allow_untrusted=False,
        compact=False,
    ):
        if action == "list":
            return self._plugins(query, limit, compact=compact)
        if action == "refresh_catalog":
            from pyplugin_installer import instance

            instance().fetchAvailablePlugins(reloadMode=True)
            qgis.utils.updateAvailablePlugins()
            self.state.touch("ecosystem.plugins", {"action": action})
            return self._plugins(query, limit)
        if action == "show_manager":
            self.iface.pluginManagerInterface().showPluginManager()
            return {"shown": True, "query": str(query or "")}
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
        elif action == "install":
            self._install_official_plugin(
                package,
                confirmed=confirmed,
                expected_version=expected_version,
                experimental=experimental,
                allow_untrusted=allow_untrusted,
            )
        else:
            raise ValueError("Unknown plugin action")
        self.state.touch("ecosystem.plugins", {"plugin": package, "action": action})
        return self._plugins(package, limit)

    @staticmethod
    def _install_official_plugin(
        package,
        *,
        confirmed,
        expected_version,
        experimental,
        allow_untrusted,
    ):
        if not confirmed:
            raise ValueError("Explicit user confirmation is required for plugin installation")
        from pyplugin_installer import instance
        from pyplugin_installer.installer_data import officialRepo, repositories
        from pyplugin_installer.installer_data import plugins as installer_plugins

        manager = instance()
        repositories.setInspectionFilter(officialRepo[0])
        try:
            manager.fetchAvailablePlugins(reloadMode=True)
        finally:
            repositories.setInspectionFilter()
        available = installer_plugins.all()
        candidate = available.get(package)
        if candidate is None:
            raise KeyError("Plugin is not available from an enabled QGIS repository")
        if candidate.get("zip_repository") != officialRepo[0]:
            raise ValueError("MCP installation is restricted to the official QGIS repository")
        if candidate.get("deprecated"):
            raise ValueError("Deprecated plugins cannot be installed through MCP")
        if not candidate.get("trusted") and not allow_untrusted:
            raise ValueError("Untrusted plugin installation requires extra confirmation")
        version_key = (
            "version_available_experimental" if experimental else "version_available_stable"
        )
        available_version = str(candidate.get(version_key) or candidate.get("version_available") or "")
        if expected_version and available_version != str(expected_version):
            raise ValueError(
                "Plugin proposal is stale: expected {}, repository now offers {}".format(
                    expected_version, available_version or "an unknown version"
                )
            )
        manager.installPlugin(package, quiet=True, stable=not bool(experimental))
        qgis.utils.updateAvailablePlugins()
        if package not in qgis.utils.available_plugins:
            raise RuntimeError("QGIS did not report the plugin as installed")
        installed_version = str(qgis.utils.pluginMetadata(package, "version") or "")
        if expected_version and installed_version != str(expected_version):
            raise RuntimeError(
                "QGIS installed version {} instead of the confirmed version {}".format(
                    installed_version or "unknown", expected_version
                )
            )

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
        scene_mode="local",
    ):
        if _unsafe_python_3d_creation():
            return self._views_3d_native_windows(
                action=action,
                name=name,
                background_color=background_color,
                show_labels=show_labels,
                field_of_view=field_of_view,
                movement_speed=movement_speed,
                terrain_enabled=terrain_enabled,
                skybox_enabled=skybox_enabled,
                eye_dome_lighting=eye_dome_lighting,
                layers=layers,
                scene_mode=scene_mode,
            )
        canvases = list(self.iface.mapCanvases3D())
        if action == "list":
            views = []
            for item in canvases:
                summary = self._view_3d(item)
                summary["aliases"] = sorted(
                    alias
                    for alias, actual in self._3d_aliases.items()
                    if actual == summary["name"]
                )
                views.append(summary)
            return {
                "views": views,
                "creation": {
                    "programmatic_supported": True,
                    "route": "pyqgis",
                    "stabilization_seconds": 0,
                },
                "pending_creations": [],
            }
        if action == "create":
            if not name:
                raise ValueError("name is required")
            if any(_canvas_name(item) == str(name) for item in canvases):
                raise ValueError("A 3D view with this name already exists")
            scene_mode = str(scene_mode).casefold()
            if scene_mode not in {"local", "globe"}:
                raise ValueError("scene_mode must be local or globe")
            qgis_scene_mode = (
                Qgis.SceneMode.Globe if scene_mode == "globe" else Qgis.SceneMode.Local
            )
            canvas = self.iface.createNewMapCanvas3D(str(name), qgis_scene_mode)
            if canvas is None:
                raise RuntimeError("Could not create the 3D map view")
            actual_name = _canvas_name(canvas)
        else:
            canvas = _find_3d_canvas(canvases, name)
            if action == "close":
                actual_name = _canvas_name(canvas)
                self.iface.closeMapCanvas3D(actual_name)
                self._3d_aliases = {
                    alias: actual
                    for alias, actual in self._3d_aliases.items()
                    if actual != actual_name
                }
                self._3d_pending.pop(str(name), None)
                self.state.touch(
                    "ecosystem.3d", {"name": actual_name, "action": action}
                )
                return {"closed": actual_name}
            if action != "configure":
                raise ValueError("Unknown 3D view action")
        self._configure_3d_canvas(
            canvas,
            {
                "background_color": background_color,
                "show_labels": show_labels,
                "field_of_view": field_of_view,
                "movement_speed": movement_speed,
                "terrain_enabled": terrain_enabled,
                "skybox_enabled": skybox_enabled,
                "eye_dome_lighting": eye_dome_lighting,
                "layers": layers,
            },
        )
        self.state.touch("ecosystem.3d", {"name": _canvas_name(canvas), "action": action})
        result = self._view_3d(canvas)
        if action == "create":
            result.update(
                {
                    "created": True,
                    "queued": False,
                    "requested_name": str(name),
                    "creation_route": "pyqgis",
                    "scene_mode": scene_mode,
                }
            )
        return result

    def _views_3d_native_windows(
        self,
        action,
        name,
        background_color,
        show_labels,
        field_of_view,
        movement_speed,
        terrain_enabled,
        skybox_enabled,
        eye_dome_lighting,
        layers,
        scene_mode,
    ):
        """Manage QGIS 3.44 Windows 3D views without mapCanvases3D().

        The SIP wrapper which converts QgisInterface::mapCanvases3D() to a
        Python list can freeze this QGIS/Qt3D combination. QGIS still exposes
        the native canvas as a QWindow, so discover it through Qt and cast the
        single window only after its C++ initialization has finished.
        """
        now = time.time()
        requested_name = str(name or "")
        requested_pending = self._3d_pending.get(requested_name)
        stabilizing = [
            pending
            for pending in self._3d_pending.values()
            if pending["status"] == "initializing"
            and now < pending["ready_after"]
        ]
        if action == "list" and stabilizing:
            return {
                "views": [],
                "creation": self._native_3d_creation_metadata(),
                "pending_creations": [
                    _public_pending_3d(pending, now) for pending in stabilizing
                ],
            }
        if (
            action in {"configure", "close"}
            and requested_pending
            and requested_pending["status"] == "initializing"
            and now < requested_pending["ready_after"]
        ):
            raise RuntimeError(
                "3D view is initializing; retry after {} ms".format(
                    max(0, int((requested_pending["ready_after"] - now) * 1000))
                )
            )

        snapshot = self._native_3d_snapshot()
        self._refresh_pending_native_3d(snapshot)
        snapshot = self._native_3d_snapshot()
        self._associate_native_3d_views(snapshot)

        if action == "list":
            return {
                "views": self._native_3d_view_summaries(snapshot),
                "creation": self._native_3d_creation_metadata(),
                "pending_creations": [
                    _public_pending_3d(pending, now)
                    for pending in self._3d_pending.values()
                    if pending["status"] in {"queued", "initializing", "failed"}
                ],
            }

        if action == "create":
            if not requested_name:
                raise ValueError("name is required")
            existing_names = {
                item["name"].casefold() for item in snapshot["containers"].values()
            }
            existing_names.update(alias.casefold() for alias in self._3d_aliases)
            if requested_name.casefold() in existing_names:
                raise ValueError("A 3D view with this name already exists")
            normalized_scene_mode = str(scene_mode).casefold()
            if normalized_scene_mode not in {"local", "globe"}:
                raise ValueError("scene_mode must be local or globe")
            self._queue_native_3d_creation(
                requested_name,
                normalized_scene_mode,
                {
                    "background_color": background_color,
                    "show_labels": show_labels,
                    "field_of_view": field_of_view,
                    "movement_speed": movement_speed,
                    "terrain_enabled": terrain_enabled,
                    "skybox_enabled": skybox_enabled,
                    "eye_dome_lighting": eye_dome_lighting,
                    "layers": layers,
                },
                snapshot,
            )
            self.state.touch(
                "ecosystem.3d",
                {"name": requested_name, "action": "create_queued"},
            )
            return {
                "created": False,
                "accepted": True,
                "queued": True,
                "status": "initializing",
                "requested_name": requested_name,
                "creation_route": "queued_cpp_slot_qwindow",
                "scene_mode": normalized_scene_mode,
                "next_action": (
                    "Poll action=list or retry configure with the requested name; "
                    "the native view is resolved automatically."
                ),
            }

        pending = self._3d_pending.get(requested_name)
        if pending and pending["status"] in {"queued", "initializing"}:
            raise RuntimeError("3D view creation is still pending; retry after action=list")
        if pending and pending["status"] == "failed":
            raise RuntimeError(
                "3D view creation failed: {}".format(pending.get("error"))
            )
        actual_name = self._3d_aliases.get(requested_name, requested_name)
        container = next(
            (
                item
                for item in snapshot["containers"].values()
                if item["name"].casefold() == actual_name.casefold()
            ),
            None,
        )
        if container is None:
            available = sorted(
                item["name"] for item in snapshot["containers"].values()
            )
            raise KeyError("3D view not found; available views: {}".format(available))

        if action == "close":
            if not container["shell"].close():
                raise RuntimeError("QGIS rejected the 3D view close request")
            self._3d_native_views.pop(actual_name, None)
            self._3d_aliases = {
                alias: actual
                for alias, actual in self._3d_aliases.items()
                if actual != actual_name
            }
            self._3d_pending.pop(requested_name, None)
            self.state.touch(
                "ecosystem.3d", {"name": actual_name, "action": "close"}
            )
            return {"closed": actual_name}
        if action != "configure":
            raise ValueError("Unknown 3D view action")

        canvas = self._native_3d_canvas(actual_name, snapshot)
        self._configure_3d_canvas(
            canvas,
            {
                "background_color": background_color,
                "show_labels": show_labels,
                "field_of_view": field_of_view,
                "movement_speed": movement_speed,
                "terrain_enabled": terrain_enabled,
                "skybox_enabled": skybox_enabled,
                "eye_dome_lighting": eye_dome_lighting,
                "layers": layers,
            },
        )
        self.state.touch(
            "ecosystem.3d", {"name": actual_name, "action": "configure"}
        )
        return self._view_3d(canvas, actual_name)

    @staticmethod
    def _native_3d_creation_metadata():
        return {
            "programmatic_supported": True,
            "windows_qgis_344_route": "queued_cpp_slot_qwindow",
            "stabilization_seconds": 5,
            "avoids_unsafe_api": "QgisInterface.mapCanvases3D",
        }

    @staticmethod
    def _native_3d_snapshot():
        containers = {}
        for widget in QApplication.allWidgets():
            if widget.metaObject().className() != "Qgs3DMapCanvasWidget":
                continue
            shell = _native_3d_shell(widget)
            address = _qt_address(widget)
            top_handle = widget.window().windowHandle()
            containers[address] = {
                "address": address,
                "widget": widget,
                "shell": shell,
                "shell_address": _qt_address(shell),
                "top_handle_address": (
                    _qt_address(top_handle) if top_handle is not None else None
                ),
                "name": _canvas_name(widget),
            }
        canvases = {}
        for window in QApplication.allWindows():
            if window.metaObject().className() != "Qgs3DMapCanvas":
                continue
            address = _qt_address(window)
            parent = window.parent()
            canvases[address] = {
                "address": address,
                "window": window,
                "parent_address": _qt_address(parent) if parent is not None else None,
            }
        return {"containers": containers, "canvases": canvases}

    def _associate_native_3d_views(self, snapshot):
        current_canvases = set(snapshot["canvases"])
        current_containers = set(snapshot["containers"])
        self._3d_native_views = {
            name: record
            for name, record in self._3d_native_views.items()
            if record["canvas_address"] in current_canvases
            and record["container_address"] in current_containers
        }
        used_canvases = {
            record["canvas_address"] for record in self._3d_native_views.values()
        }
        used_containers = {
            record["container_address"] for record in self._3d_native_views.values()
        }
        for container_address, container in snapshot["containers"].items():
            if container_address in used_containers:
                continue
            candidates = [
                canvas_address
                for canvas_address, canvas in snapshot["canvases"].items()
                if canvas_address not in used_canvases
                and canvas["parent_address"] == container["top_handle_address"]
            ]
            if len(candidates) == 1:
                self._3d_native_views[container["name"]] = {
                    "container_address": container_address,
                    "shell_address": container["shell_address"],
                    "canvas_address": candidates[0],
                }
                used_containers.add(container_address)
                used_canvases.add(candidates[0])
        unresolved_containers = [
            address
            for address in snapshot["containers"]
            if address not in used_containers
        ]
        unresolved_canvases = [
            address for address in snapshot["canvases"] if address not in used_canvases
        ]
        if len(unresolved_containers) == 1 and len(unresolved_canvases) == 1:
            container_address = unresolved_containers[0]
            container = snapshot["containers"][container_address]
            self._3d_native_views[container["name"]] = {
                "container_address": container_address,
                "shell_address": container["shell_address"],
                "canvas_address": unresolved_canvases[0],
            }

    def _native_3d_canvas(self, actual_name, snapshot):
        self._associate_native_3d_views(snapshot)
        record = self._3d_native_views.get(actual_name)
        if record is None:
            raise RuntimeError(
                "The 3D view is open, but its native canvas cannot be matched safely"
            )
        canvas_entry = snapshot["canvases"].get(record["canvas_address"])
        if canvas_entry is None:
            raise RuntimeError("The native 3D canvas is no longer available")
        return _cast_native_3d_window(canvas_entry["window"])

    def _native_3d_view_summaries(self, snapshot):
        summaries = []
        for container in sorted(
            snapshot["containers"].values(), key=lambda item: item["name"].casefold()
        ):
            actual_name = container["name"]
            try:
                canvas = self._native_3d_canvas(actual_name, snapshot)
                summary = self._view_3d(canvas, actual_name)
                summary["settings_available"] = True
            except RuntimeError as exc:
                summary = {
                    "name": actual_name,
                    "settings_available": False,
                    "settings_error": str(exc),
                }
            summary["aliases"] = sorted(
                alias
                for alias, actual in self._3d_aliases.items()
                if actual == actual_name
            )
            summary["status"] = "ready"
            summaries.append(summary)
        return summaries

    def _queue_native_3d_creation(
        self, requested_name, scene_mode, configuration, snapshot=None
    ):
        active_pending = [
            item
            for item in self._3d_pending.values()
            if item["status"] in {"queued", "initializing"}
        ]
        if active_pending:
            raise RuntimeError(
                "Another native 3D view creation is still pending; wait for it first"
            )
        extent = self.iface.mapCanvas().projectExtent()
        if extent.isEmpty() or not extent.isFinite():
            raise RuntimeError(
                "The project extent is invalid. Add or activate a spatial layer before "
                "creating a 3D view."
            )
        method = "new3DMapCanvasGlobe" if scene_mode == "globe" else "new3DMapCanvas"
        main_window = self.iface.mainWindow()
        signature = "{}()".format(method)
        if main_window.metaObject().indexOfMethod(signature) < 0:
            raise RuntimeError("The native QGIS 3D creation slot is unavailable")
        snapshot = snapshot or self._native_3d_snapshot()
        queued_at = time.time()
        pending = {
            "requested_name": requested_name,
            "scene_mode": scene_mode,
            "status": "queued",
            "queued_at": queued_at,
            "ready_after": queued_at + 5,
            "_before_container_addresses": set(snapshot["containers"]),
            "_before_canvas_addresses": set(snapshot["canvases"]),
            "_configuration": configuration,
        }
        self._3d_pending[requested_name] = pending
        try:
            invoked = QMetaObject.invokeMethod(
                main_window, method, Qt.ConnectionType.QueuedConnection
            )
        except Exception:
            self._3d_pending.pop(requested_name, None)
            raise
        if invoked is False:
            self._3d_pending.pop(requested_name, None)
            raise RuntimeError("QGIS rejected the queued 3D creation request")
        pending["status"] = "initializing"

    def _refresh_pending_native_3d(self, snapshot):
        for requested_name, pending in list(self._3d_pending.items()):
            if pending["status"] not in {"queued", "initializing"}:
                continue
            if time.time() < pending["ready_after"]:
                continue
            created_containers = [
                item
                for address, item in snapshot["containers"].items()
                if address not in pending["_before_container_addresses"]
            ]
            created_canvases = [
                item
                for address, item in snapshot["canvases"].items()
                if address not in pending["_before_canvas_addresses"]
            ]
            if len(created_containers) == 1 and len(created_canvases) == 1:
                container = created_containers[0]
                canvas_entry = created_canvases[0]
                actual_name = container["name"]
                self._3d_aliases[requested_name] = actual_name
                self._3d_native_views[actual_name] = {
                    "container_address": container["address"],
                    "shell_address": container["shell_address"],
                    "canvas_address": canvas_entry["address"],
                }
                pending.update(
                    {
                        "status": "succeeded",
                        "actual_name": actual_name,
                        "finished_at": time.time(),
                    }
                )
                try:
                    canvas = _cast_native_3d_window(canvas_entry["window"])
                    self._configure_3d_canvas(canvas, pending["_configuration"])
                except Exception as exc:
                    pending["configuration_error"] = "{}: {}".format(
                        type(exc).__name__, exc
                    )
                self.state.touch(
                    "ecosystem.3d",
                    {"name": actual_name, "action": "created_native"},
                )
            elif len(created_containers) > 1 or len(created_canvases) > 1:
                pending.update(
                    {
                        "status": "failed",
                        "error": "Multiple new 3D views appeared; creation is ambiguous",
                    }
                )
            elif time.time() - pending["queued_at"] > 30:
                pending.update(
                    {
                        "status": "failed",
                        "error": "QGIS did not expose the queued 3D view within 30 seconds",
                    }
                )

    def _configure_3d_canvas(self, canvas, configuration):
        settings = canvas.mapSettings()
        if settings is None:
            raise RuntimeError("3D map settings are unavailable")
        background_color = configuration.get("background_color")
        show_labels = configuration.get("show_labels")
        field_of_view = configuration.get("field_of_view")
        movement_speed = configuration.get("movement_speed")
        terrain_enabled = configuration.get("terrain_enabled")
        skybox_enabled = configuration.get("skybox_enabled")
        eye_dome_lighting = configuration.get("eye_dome_lighting")
        layers = configuration.get("layers")
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
    def _plugins(query, limit, compact=False):
        qgis.utils.updateAvailablePlugins()
        packages = sorted(set(qgis.utils.available_plugins) | set(qgis.utils.plugins))
        query = str(query or "").casefold().strip()
        query_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", query)
            if len(token) > 2
        }
        items = []
        for package in packages:
            metadata = {
                key: qgis.utils.pluginMetadata(package, key)
                for key in (
                    "name",
                    "description",
                    "about",
                    "version",
                    "author",
                    "category",
                    "tags",
                    "experimental",
                    "deprecated",
                    "homepage",
                    "repository",
                    "tracker",
                    "qgisMinimumVersion",
                    "qgisMaximumVersion",
                    "external_dependencies",
                    "plugin_dependencies",
                    "update_date",
                )
            }
            haystack = "{} {} {} {} {} {}".format(
                package,
                metadata["name"],
                metadata["description"],
                metadata["about"],
                metadata["tags"],
                metadata["category"],
            )
            normalized_haystack = haystack.casefold()
            matched_tokens = query_tokens & set(re.findall(r"[a-z0-9]+", normalized_haystack))
            if query and query not in normalized_haystack and not matched_tokens:
                continue
            if compact:
                metadata = {
                    "name": metadata["name"],
                    "description": _bounded_text(metadata["description"], 300),
                    "about": _bounded_text(metadata["about"], 500),
                    "version": metadata["version"],
                    "tags": _bounded_text(metadata["tags"], 200),
                    "category": metadata["category"],
                    "experimental": metadata["experimental"],
                    "deprecated": metadata["deprecated"],
                    "plugin_dependencies": _bounded_text(
                        metadata["plugin_dependencies"], 200
                    ),
                }
            items.append(
                {
                    "package": package,
                    "loaded": package in qgis.utils.plugins,
                    "active": package in qgis.utils.active_plugins,
                    "metadata": metadata,
                    "relevance": len(matched_tokens) + (3 if query and query in normalized_haystack else 0),
                }
            )
        items.sort(
            key=lambda item: (
                -int(item["relevance"]),
                not bool(item["active"]),
                str(item["package"]).casefold(),
            )
        )
        items = items[: max(1, min(int(limit), 1000))]
        return {
            "plugins": items,
            "count": len(items),
            "qgis_version": Qgis.QGIS_VERSION,
        }

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
    def _view_3d(canvas, name=None):
        settings = canvas.mapSettings()
        if settings is None:
            raise RuntimeError("3D map settings are unavailable")
        extent = settings.extent()
        return {
            "name": str(name or _canvas_name(canvas)),
            "background_color": settings.backgroundColor().name(
                QColor.NameFormat.HexArgb
            ),
            "show_labels": settings.showLabels(),
            "field_of_view": settings.fieldOfView(),
            "movement_speed": settings.cameraMovementSpeed(),
            "terrain_enabled": settings.terrainRenderingEnabled(),
            "skybox_enabled": settings.isSkyboxEnabled(),
            "eye_dome_lighting": settings.eyeDomeLightingEnabled(),
            "layers": [layer.id() for layer in settings.layers()],
            "extent": [
                extent.xMinimum(),
                extent.yMinimum(),
                extent.xMaximum(),
                extent.yMaximum(),
            ],
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


def _public_pending_3d(pending, now=None):
    result = {
        key: value for key, value in pending.items() if not key.startswith("_")
    }
    if now is not None and pending.get("status") == "initializing":
        result["retry_after_ms"] = max(
            0, int((pending.get("ready_after", now) - now) * 1000)
        )
    return result


def _qt_address(obj):
    return int(sip.unwrapinstance(obj))


def _native_3d_shell(widget):
    current = widget
    seen = set()
    while current is not None and _qt_address(current) not in seen:
        seen.add(_qt_address(current))
        title = current.windowTitle()
        if title:
            return current
        current = current.parentWidget()
    return widget.window()


def _cast_native_3d_window(window):
    from qgis._3d import Qgs3DMapCanvas

    return sip.cast(window, Qgs3DMapCanvas)


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
    # Qgs3DMapCanvas itself has no public canvas-name accessor in QGIS 3.44.
    # The view name is held by the containing dock/dialog, so inspect window
    # titles before falling back to generic QObject names.
    current = canvas
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        candidate = getattr(current, "windowTitle", None)
        if callable(candidate):
            value = candidate()
            if value:
                return str(value)
        parent_widget = getattr(current, "parentWidget", None)
        current = parent_widget() if callable(parent_widget) else None
    for method in ("name", "objectName"):
        candidate = getattr(canvas, method, None)
        if callable(candidate):
            value = candidate()
            if value:
                return str(value)
    return "3D view"


def _find_3d_canvas(canvases, name):
    if not canvases:
        raise KeyError("3D view not found; no 3D views are open")
    if name is None:
        if len(canvases) == 1:
            return canvases[0]
        raise KeyError("3D view name is required because multiple views are open")
    wanted = str(name).strip().casefold()
    exact = [item for item in canvases if _canvas_name(item).casefold() == wanted]
    if len(exact) == 1:
        return exact[0]
    aliases = [
        item
        for item in canvases
        if wanted in {value.casefold() for value in _canvas_aliases(item)}
    ]
    if len(aliases) == 1:
        return aliases[0]
    available = sorted({_canvas_name(item) for item in canvases})
    if len(aliases) > 1:
        raise KeyError("3D view name is ambiguous; available views: {}".format(available))
    raise KeyError("3D view not found; available views: {}".format(available))


def _canvas_aliases(canvas):
    values = set()
    current = canvas
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for method in ("windowTitle", "objectName", "name"):
            candidate = getattr(current, method, None)
            if callable(candidate):
                value = str(candidate() or "").strip()
                if value:
                    values.add(value)
                    for separator in (" — ", " - ", " | "):
                        if separator in value:
                            values.add(value.split(separator, 1)[0].strip())
        parent_widget = getattr(current, "parentWidget", None)
        current = parent_widget() if callable(parent_widget) else None
    return values


def _unsafe_python_3d_creation():
    version_int = int(getattr(Qgis, "QGIS_VERSION_INT", 0))
    return unsafe_python_3d_creation(version_int, sys.platform)
