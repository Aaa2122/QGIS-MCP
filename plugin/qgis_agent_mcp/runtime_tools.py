from __future__ import annotations

import os
import platform
import sys
import time
from collections import Counter
from pathlib import Path

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsAuthMethodConfig,
    QgsProject,
    QgsProviderRegistry,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import PYQT_VERSION_STR, QT_VERSION_STR

from .serialize import layer_summary


class RuntimeTools:
    """Operational controls which make autonomous calls observable and reversible."""

    def __init__(
        self,
        iface,
        state,
        log,
        operations,
        layer_resolver,
        method_names,
        mutation_methods,
        data_manager=None,
        layout_manager=None,
        diagnostics=None,
    ):
        self.iface = iface
        self.state = state
        self.log = log
        self.operations = operations
        self.layer_resolver = layer_resolver
        self.method_names = method_names
        self.mutation_methods = mutation_methods
        self.data_manager = data_manager
        self.layout_manager = layout_manager
        self.persistent_diagnostics = diagnostics

    def runtime(self, action="status"):
        if action == "status":
            return self._runtime_status()
        if action == "providers":
            registry = QgsProviderRegistry.instance()
            processing = QgsApplication.processingRegistry()
            providers = []
            for provider in processing.providers():
                providers.append(
                    {
                        "id": provider.id(),
                        "name": provider.name(),
                        "active": bool(provider.isActive()),
                        "algorithm_count": len(provider.algorithms()),
                    }
                )
            return {
                "data_providers": sorted(registry.providerList()),
                "processing_providers": providers,
            }
        if action == "compatibility":
            version_int = int(getattr(Qgis, "QGIS_VERSION_INT", 0))
            major = version_int // 10000 if version_int else _major(Qgis.QGIS_VERSION)
            return {
                "qgis_version": Qgis.QGIS_VERSION,
                "qgis_version_int": version_int,
                "qgis_major": major,
                "supported": major in {3, 4},
                "compatibility_profile": "qgis4" if major >= 4 else "qgis3-ltr",
                "qt_version": QT_VERSION_STR,
                "pyqt_version": PYQT_VERSION_STR,
                "feature_flags": {
                    "temporal_controller": hasattr(self.iface.mapCanvas(), "temporalController"),
                    "elevation_controller": hasattr(self.iface.mapCanvas(), "zRange"),
                    "project_transaction_mode": hasattr(QgsProject.instance(), "transactionMode"),
                    "processing_tasks": hasattr(QgsApplication, "taskManager"),
                },
            }
        raise ValueError("Unknown runtime action")

    def tasks(self, action="list", task_id=None):
        manager = QgsApplication.taskManager()
        tasks = list(manager.tasks())
        if action == "list":
            return {"tasks": [self._task_summary(task) for task in tasks]}
        if task_id is None:
            raise ValueError("task_id is required")
        task = next((item for item in tasks if str(item.id()) == str(task_id)), None)
        if task is None:
            raise KeyError("QGIS task not found")
        if action == "status":
            return self._task_summary(task)
        if action == "cancel":
            task.cancel()
            self.state.touch("task.cancelled", {"id": str(task.id())})
            return self._task_summary(task)
        raise ValueError("Unknown task action")

    def events(self, after_revision=0, until_revision=None, event_types=None, limit=200):
        after_revision = max(0, int(after_revision))
        limit = max(1, min(int(limit), 1000))
        wanted = set(event_types or [])
        changes = []
        for item in self.state.changes_since(after_revision):
            if until_revision is not None and item["revision"] > int(until_revision):
                continue
            if wanted and not any(
                item["event"] == value or item["event"].startswith(value + ".")
                for value in wanted
            ):
                continue
            changes.append(item)
        return {
            "after_revision": after_revision,
            "current_revision": self.state.revision,
            "events": changes[:limit],
            "has_more": len(changes) > limit,
        }

    def render(self, action="status", enabled=None):
        canvas = self.iface.mapCanvas()
        if action == "refresh":
            canvas.refresh()
            self.state.touch("canvas.refresh", None)
        elif action == "refresh_all":
            refresh_all = getattr(canvas, "refreshAllLayers", None)
            (refresh_all or canvas.refresh)()
            self.state.touch("canvas.refresh_all", None)
        elif action == "cancel":
            stop = getattr(canvas, "stopRendering", None)
            if stop is not None:
                stop()
            self.state.touch("canvas.render_cancelled", None)
        elif action == "set_enabled":
            if enabled is None:
                raise ValueError("enabled is required")
            canvas.setRenderFlag(bool(enabled))
            self.state.touch("canvas.render_enabled", {"enabled": bool(enabled)})
        elif action != "status":
            raise ValueError("Unknown render action")
        return {
            "drawing": bool(canvas.isDrawing()),
            "enabled": bool(canvas.renderFlag()),
            "scale": canvas.scale(),
            "layer_count": len(canvas.layers()),
        }

    def transaction(self, action="status", layers=None, stop_editing=True):
        targets = self._vector_layers(layers)
        if action == "status":
            return self._edit_status(targets)
        if not targets:
            raise ValueError("No vector layer is available")
        results = []
        started = []
        if action == "start":
            for layer in targets:
                if layer.isEditable():
                    results.append({"layer_id": layer.id(), "ok": True, "already_editable": True})
                    continue
                ok = bool(layer.startEditing())
                results.append({"layer_id": layer.id(), "ok": ok})
                if ok:
                    started.append(layer)
                else:
                    for opened in started:
                        opened.rollBack()
                    raise RuntimeError("Could not start editing layer {}".format(layer.name()))
        elif action in {"commit", "save"}:
            keep_open = action == "save" or not bool(stop_editing)
            for layer in targets:
                if not layer.isEditable():
                    results.append({"layer_id": layer.id(), "ok": True, "editable": False})
                    continue
                ok = bool(layer.commitChanges(not keep_open))
                item = {"layer_id": layer.id(), "ok": ok}
                if not ok:
                    item["errors"] = list(layer.commitErrors())
                results.append(item)
            if not all(item["ok"] for item in results):
                raise RuntimeError("One or more layers could not be committed")
        elif action == "rollback":
            for layer in targets:
                ok = True if not layer.isEditable() else bool(layer.rollBack(bool(stop_editing)))
                results.append({"layer_id": layer.id(), "ok": ok})
        else:
            raise ValueError("Unknown transaction action")
        self.state.touch(
            "transaction.{}".format(action),
            {"layer_ids": [layer.id() for layer in targets]},
        )
        return {
            "action": action,
            "results": results,
            "atomic_across_providers": False,
            "layers": self._edit_status(targets)["layers"],
        }

    def undo(self, action="status", layer=None, steps=1):
        target = self.layer_resolver(layer) if layer else self.iface.activeLayer()
        if not isinstance(target, QgsVectorLayer):
            raise ValueError("A vector layer is required")
        stack = target.undoStack()
        steps = max(1, min(int(steps), 100))
        if action == "undo":
            for _ in range(steps):
                if not stack.canUndo():
                    break
                stack.undo()
            self.state.touch("vector.undo", {"layer_id": target.id(), "steps": steps})
        elif action == "redo":
            for _ in range(steps):
                if not stack.canRedo():
                    break
                stack.redo()
            self.state.touch("vector.redo", {"layer_id": target.id(), "steps": steps})
        elif action != "status":
            raise ValueError("Unknown undo action")
        return {
            "layer_id": target.id(),
            "can_undo": bool(stack.canUndo()),
            "can_redo": bool(stack.canRedo()),
            "undo_text": stack.undoText(),
            "redo_text": stack.redoText(),
            "count": stack.count(),
            "index": stack.index(),
        }

    def preflight(self, calls, require_saved_project=False):
        if not isinstance(calls, list) or not calls:
            raise ValueError("calls must be a non-empty array")
        if len(calls) > 100:
            raise ValueError("A preflight is limited to 100 calls")
        available = set(self.method_names())
        errors = []
        warnings = []
        normalized = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or not isinstance(call.get("method"), str):
                errors.append({"index": index, "message": "method is required"})
                continue
            method = call["method"]
            params = call.get("params") or {}
            if method not in available:
                errors.append({"index": index, "method": method, "message": "unknown method"})
            if not isinstance(params, dict):
                errors.append({"index": index, "method": method, "message": "params must be an object"})
                params = {}
            if method in {"ui.invoke", "capabilities.invoke"}:
                warnings.append({"index": index, "method": method, "message": "escape hatch requires elevated trust"})
            normalized.append(
                {
                    "index": index,
                    "method": method,
                    "mutation": method in self.mutation_methods,
                    "parameter_names": sorted(params),
                }
            )
        project = QgsProject.instance()
        if require_saved_project and not project.fileName():
            errors.append({"message": "project must be saved before execution"})
        return {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "calls": normalized,
            "current_revision": self.state.revision,
            "project_file": project.fileName() or None,
            "mutation_count": sum(item["mutation"] for item in normalized),
        }

    def diff(self, from_revision, to_revision=None, limit=1000):
        from_revision = max(0, int(from_revision))
        to_revision = self.state.revision if to_revision is None else int(to_revision)
        if to_revision < from_revision:
            raise ValueError("to_revision must be greater than or equal to from_revision")
        changes = [
            item
            for item in self.state.changes_since(from_revision)
            if item["revision"] <= to_revision
        ]
        limit = max(1, min(int(limit), 1000))
        return {
            "from_revision": from_revision,
            "to_revision": to_revision,
            "event_counts": dict(Counter(item["event"] for item in changes)),
            "changed_resources": sorted(
                {uri for item in changes for uri in item.get("resources", {})}
            ),
            "changes": changes[:limit],
            "has_more": len(changes) > limit,
        }

    def diagnostics(self, include_logs=True):
        project = QgsProject.instance()
        invalid_layers = []
        missing_local_sources = []
        for layer in project.mapLayers().values():
            if not layer.isValid():
                invalid_layers.append(layer_summary(layer))
            provider = layer.providerType()
            source_path = str(layer.source()).split("|", 1)[0]
            if provider in {"ogr", "gdal", "delimitedtext"} and source_path:
                candidate = Path(source_path.removeprefix("file://"))
                if candidate.is_absolute() and not candidate.exists():
                    missing_local_sources.append(
                        {"layer_id": layer.id(), "name": layer.name(), "path": str(candidate)}
                    )
        failed_operations = [
            item for item in self.operations.list_public() if item.get("status") == "failed"
        ]
        result = {
            "healthy": not invalid_layers and not missing_local_sources and not failed_operations,
            "project": {
                "file": project.fileName() or None,
                "dirty": project.isDirty(),
                "layer_count": len(project.mapLayers()),
            },
            "invalid_layers": invalid_layers,
            "missing_local_sources": missing_local_sources,
            "failed_operations": failed_operations,
            "rendering": bool(self.iface.mapCanvas().isDrawing()),
            "runtime": self._runtime_status(),
        }
        if include_logs:
            result["recent_errors"] = self.log.read(level="error", limit=50)["events"]
        if self.persistent_diagnostics is not None:
            result["persistent_diagnostics"] = self.persistent_diagnostics.snapshot()
            if result["persistent_diagnostics"]["previous_interruption"]:
                result["healthy"] = False
        return result

    def permissions(self):
        policy = getattr(self.data_manager, "policy", None)
        output_policy = getattr(self.layout_manager, "output_policy", None)
        return {
            "python_execution": False,
            "network": {
                "private_hosts_allowed": bool(getattr(policy, "allow_private", False)),
                "allowed_hosts": sorted(getattr(policy, "allowed_hosts", set())),
            },
            "filesystem": {
                "output_roots": [str(path) for path in getattr(output_policy, "roots", [])],
            },
            "credentials": "opaque_authcfg_references_only",
            "plugin_installation": {
                "allowed": True,
                "official_repository_only": True,
                "proposal_required": True,
                "explicit_user_confirmation_required": True,
                "untrusted_plugins_require_extra_confirmation": True,
            },
        }

    def auth(self, action="list", authcfg=None):
        if action not in {"list", "describe"}:
            raise ValueError("Unknown auth action")
        manager = QgsApplication.authManager()
        identifiers = list(getattr(manager, "configIds", lambda: [])())
        configurations = []
        for identifier in identifiers:
            config = QgsAuthMethodConfig()
            try:
                loaded = bool(manager.loadAuthenticationConfig(identifier, config, False))
            except TypeError:
                loaded = bool(manager.loadAuthenticationConfig(identifier, config))
            item = {
                "id": str(identifier),
                "loaded": loaded,
                "name": config.name() if loaded else None,
                "method": config.method() if loaded else None,
                "uri": config.uri() if loaded else None,
                "version": config.version() if loaded else None,
                "secrets_exposed": False,
            }
            configurations.append(item)
        if action == "describe":
            match = next((item for item in configurations if item["id"] == str(authcfg)), None)
            if match is None:
                raise KeyError("Authentication configuration not found")
            return match
        return {
            "master_password_set": bool(manager.masterPasswordIsSet()),
            "configurations": configurations,
        }

    def _runtime_status(self):
        application = QgsApplication.instance()
        return {
            "qgis_version": Qgis.QGIS_VERSION,
            "qgis_release_name": getattr(Qgis, "QGIS_RELEASE_NAME", None),
            "qgis_version_int": int(getattr(Qgis, "QGIS_VERSION_INT", 0)),
            "qt_version": QT_VERSION_STR,
            "pyqt_version": PYQT_VERSION_STR,
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "prefix_path": QgsApplication.prefixPath(),
            "settings_directory": QgsApplication.qgisSettingsDirPath(),
            "locale": _optional_call(QgsApplication, "locale"),
            "pid": os.getpid(),
            "application_running": application is not None,
            "uptime_seconds": round(time.time() - self.state.started_at, 3),
        }

    @staticmethod
    def _task_summary(task):
        status = task.status()
        try:
            status_value = int(status)
        except (TypeError, ValueError):
            status_value = str(status)
        return {
            "id": str(task.id()),
            "description": task.description(),
            "status": status_value,
            "status_name": getattr(status, "name", str(status)),
            "progress": float(task.progress()),
            "can_cancel": bool(getattr(task, "canCancel", lambda: True)()),
        }

    def _vector_layers(self, references):
        if references is None:
            return [
                layer
                for layer in QgsProject.instance().mapLayers().values()
                if isinstance(layer, QgsVectorLayer)
            ]
        if not isinstance(references, list):
            references = [references]
        layers = [self.layer_resolver(reference) for reference in references]
        invalid = [layer.name() for layer in layers if not isinstance(layer, QgsVectorLayer)]
        if invalid:
            raise ValueError("Not vector layers: {}".format(", ".join(invalid)))
        return layers

    @staticmethod
    def _edit_status(layers):
        return {
            "layers": [
                {
                    "layer_id": layer.id(),
                    "name": layer.name(),
                    "editable": bool(layer.isEditable()),
                    "modified": bool(layer.isModified()),
                    "read_only": bool(
                        _optional_call(layer, "readOnly", "isReadOnly", default=False)
                    ),
                    "undo_count": layer.undoStack().count(),
                }
                for layer in layers
            ]
        }


def _major(version):
    try:
        return int(str(version).split(".", 1)[0])
    except (TypeError, ValueError):
        return 0


def _optional_call(target, *names, default=None):
    for name in names:
        value = getattr(target, name, None)
        if callable(value):
            try:
                return value()
            except (RuntimeError, TypeError):
                continue
    return default
