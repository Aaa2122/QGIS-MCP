from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from qgis.core import (
    QgsApplication,
    QgsMapLayer,
    QgsMapLayerStore,
    QgsProcessingAlgorithm,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingOutputLayerDefinition,
    QgsProcessingUtils,
    QgsProject,
)
from qgis.PyQt.QtCore import QTimer

from .serialize import json_safe


class OperationManager:
    def __init__(self, log, state, artifacts=None):
        self.log = log
        self.state = state
        self.artifacts = artifacts
        self._operations = {}
        self._result_layers = QgsMapLayerStore()

    def start_processing(
        self,
        algorithm_id,
        parameters,
        retain_outputs=True,
        add_to_project=False,
        allow_main_thread=False,
    ):
        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(algorithm_id)
        if algorithm is None:
            raise KeyError("Processing algorithm not found: {}".format(algorithm_id))
        operation_id = "op_" + uuid.uuid4().hex
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()
        parameters = dict(parameters)
        output_targets = _output_targets(algorithm, parameters)
        conflicts = _loaded_output_conflicts(output_targets)
        if conflicts:
            details = ", ".join(
                "{} ({})".format(item["layer_name"], item["path"])
                for item in conflicts
            )
            raise RuntimeError(
                "Refusing to overwrite Processing output files currently loaded "
                "in QGIS: {}. Choose a new output path or remove the corresponding "
                "layer first.".format(details)
            )
        if add_to_project:
            parameters = _request_project_loading(algorithm, parameters)
        no_threading = bool(
            algorithm.flags() & QgsProcessingAlgorithm.Flag.FlagNoThreading
        )
        if no_threading and not allow_main_thread:
            raise RuntimeError(
                "This Processing algorithm must run on QGIS's main thread and may "
                "freeze the interface. Retry with allow_main_thread=true only after "
                "saving the project."
            )
        operation = {
            "id": operation_id,
            "kind": "processing",
            "algorithm": algorithm_id,
            "status": "queued",
            "progress": 0.0,
            "created": time.time(),
            "updated": time.time(),
            "parameters": json_safe(parameters),
            "retain_outputs": bool(retain_outputs),
            "add_to_project": bool(add_to_project),
            "execution": "main_thread" if no_threading else "background_task",
            "allow_main_thread": bool(allow_main_thread),
            "_output_targets": output_targets,
            "_output_baseline": {
                name: _file_fingerprint(path) for name, path in output_targets.items()
            },
            "_context": context,
            "_feedback": feedback,
        }
        feedback.progressChanged.connect(
            lambda progress, _id=operation_id: self._progress(_id, progress)
        )
        if no_threading:
            operation["_task"] = None
            self._operations[operation_id] = operation
            QTimer.singleShot(
                0,
                lambda _id=operation_id, _algorithm=algorithm_id, _params=dict(
                    parameters
                ): self._run_main_thread(_id, _algorithm, _params),
            )
            self.log.add(
                "operation",
                "Queued {} on the QGIS main thread".format(algorithm_id),
                data={"id": operation_id},
            )
            self.state.touch("operation.started", {"id": operation_id})
            return self.public(operation)

        task = QgsProcessingAlgRunnerTask(algorithm, parameters, context, feedback)
        operation["_task"] = task
        self._operations[operation_id] = operation
        dependent_layers = []
        project = QgsProject.instance()
        for value in parameters.values():
            if isinstance(value, QgsMapLayer):
                dependent_layers.append(value)
            elif isinstance(value, str):
                layer = project.mapLayer(value)
                if layer is not None:
                    dependent_layers.append(layer)
        if dependent_layers:
            task.setDependentLayers(dependent_layers)
        task.progressChanged.connect(
            lambda progress, _id=operation_id: self._progress(_id, progress)
        )
        task.begun.connect(lambda _id=operation_id: self._status(_id, "running"))
        task.executed.connect(
            lambda successful, results, _id=operation_id: self._finished(
                _id, successful, results
            )
        )
        QgsApplication.taskManager().addTask(task)
        self.log.add("operation", "Started {}".format(algorithm_id), data={"id": operation_id})
        self.state.touch("operation.started", {"id": operation_id})
        return self.public(operation)

    def _run_main_thread(self, operation_id, algorithm_id, parameters):
        operation = self._operations.get(operation_id)
        if operation is None or operation["status"] == "cancelling":
            return
        self._status(operation_id, "running")
        try:
            import processing

            results = processing.run(
                algorithm_id,
                parameters,
                context=operation["_context"],
                feedback=operation["_feedback"],
            )
            successful = not operation["_feedback"].isCanceled()
            self._finished(operation_id, successful, results)
        except Exception as exc:
            operation["error"] = {
                "message": str(exc),
                "exception": type(exc).__name__,
            }
            self._finished(operation_id, False, {})

    def control(self, operation_id, action="status"):
        operation = self._operations.get(operation_id)
        if operation is None:
            raise KeyError("Operation not found")
        if action == "cancel":
            if operation["status"] in {"queued", "running"}:
                if operation["_task"] is not None:
                    operation["_task"].cancel()
                operation["_feedback"].cancel()
                self._status(operation_id, "cancelling")
            return self.public(operation)
        if action != "status":
            raise ValueError("Unknown operation action")
        return self.public(operation)

    def list_public(self):
        return [self.public(value) for value in self._operations.values()]

    def _progress(self, operation_id, progress):
        operation = self._operations.get(operation_id)
        if operation is not None:
            operation["progress"] = float(progress)
            operation["updated"] = time.time()

    def _status(self, operation_id, status):
        operation = self._operations.get(operation_id)
        if operation is not None:
            operation["status"] = status
            operation["updated"] = time.time()

    def _finished(self, operation_id, successful, results):
        operation = self._operations.get(operation_id)
        if operation is None:
            return
        try:
            operation["feedback_log"] = operation["_feedback"].textLog()
        except Exception:
            operation["feedback_log"] = ""
        validation = _validate_processing_result(operation, results)
        if successful and not validation["passed"]:
            successful = False
            operation["error"] = {
                "message": "Processing reported completion, but output validation failed",
                "exception": "ProcessingOutputValidationError",
                "issues": validation["issues"],
            }
        operation["validation"] = validation
        operation["status"] = "succeeded" if successful else (
            "cancelled"
            if operation["_feedback"].isCanceled()
            or (operation["_task"] is not None and operation["_task"].isCanceled())
            else "failed"
        )
        operation["progress"] = 100.0 if successful else operation["progress"]
        operation["updated"] = time.time()
        operation["result"] = json_safe(results)
        operation["retained_outputs"] = self._retain_outputs(
            operation, results, allow_project_add=successful
        )
        operation["project_outputs"] = [
            value
            for value in operation["retained_outputs"].values()
            if value.get("kind") == "layer" and value.get("ownership") == "project"
        ]
        self.log.add(
            "operation",
            "{} {}".format(operation["algorithm"], operation["status"]),
            "info" if successful else "error",
            {"id": operation_id, "result": operation["result"]},
        )
        self.state.touch(
            "operation.finished",
            {"id": operation_id, "status": operation["status"]},
        )
        # Terminal operations only need their public summaries. Keeping the task,
        # feedback and processing context alive can retain provider-owned result
        # layers and their file handles after a project output is removed.
        operation["_task"] = None
        operation["_feedback"] = None
        operation["_context"] = None

    def _retain_outputs(self, operation, results, allow_project_add=True):
        retained = {}
        context = operation["_context"]
        project = QgsProject.instance()
        algorithm = QgsApplication.processingRegistry().algorithmById(
            operation["algorithm"]
        )
        definitions = {
            output.name(): output for output in algorithm.outputDefinitions()
        } if algorithm is not None else {}
        for name, value in (results or {}).items():
            layer = value if isinstance(value, QgsMapLayer) else None
            if layer is None and isinstance(value, str):
                try:
                    layer = context.getMapLayer(value)
                except Exception:
                    layer = None
                if layer is None:
                    try:
                        layer = QgsProcessingUtils.mapLayerFromString(
                            value,
                            context,
                            True,
                            _layer_hint(definitions.get(name)),
                        )
                    except Exception:
                        layer = None
            keep_layer = operation["retain_outputs"] or (
                operation["add_to_project"] and allow_project_add
            )
            if layer is not None and layer.isValid() and keep_layer:
                try:
                    owned = context.takeResultLayer(layer.id()) or layer
                except Exception:
                    owned = layer
                if operation["add_to_project"] and allow_project_add:
                    if project.mapLayer(owned.id()) is None:
                        project.addMapLayer(owned)
                    if project.mapLayer(owned.id()) is None:
                        raise RuntimeError(
                            "QGIS did not add Processing output {} to the project".format(
                                name
                            )
                        )
                elif self._result_layers.mapLayer(owned.id()) is None:
                    self._result_layers.addMapLayer(owned)
                retained[name] = {
                    "kind": "layer",
                    "layer_id": owned.id(),
                    "name": owned.name(),
                    "temporary": bool(
                        getattr(owned, "isTemporary", lambda: not bool(owned.source()))()
                    ),
                    "resource_uri": "qgis://layers/{}".format(owned.id()),
                    "ownership": (
                        "project"
                        if operation["add_to_project"] and allow_project_add
                        else "operation_store"
                    ),
                    "added_to_project": bool(
                        operation["add_to_project"] and allow_project_add
                    ),
                }
                self.state.touch("layer.retained", {"layer_id": owned.id()})
                continue
            if (
                self.artifacts is not None
                and operation["retain_outputs"]
                and isinstance(value, str)
                and Path(value).is_file()
            ):
                try:
                    retained[name] = {
                        "kind": "artifact",
                        **self.artifacts.put_file(
                            value,
                            metadata={"operation_id": operation["id"], "output": name},
                        ),
                    }
                except (OSError, ValueError) as exc:
                    retained[name] = {
                        "kind": "file",
                        "path": value,
                        "retained": False,
                        "reason": str(exc),
                    }
        return retained

    def map_layer(self, layer_id):
        return self._result_layers.mapLayer(layer_id)

    def retained_layers(self):
        return list(self._result_layers.mapLayers().values())

    @staticmethod
    def public(operation):
        return {
            key: value for key, value in operation.items() if not key.startswith("_")
        }


_FEEDBACK_FAILURES = (
    (re.compile(r"WinError\s*32", re.IGNORECASE), "output_file_locked"),
    (re.compile(r"permission denied", re.IGNORECASE), "permission_denied"),
    (re.compile(r"access is denied", re.IGNORECASE), "access_denied"),
    (
        re.compile(r"process cannot access the file", re.IGNORECASE),
        "output_file_locked",
    ),
    (re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE), "traceback"),
)


def _output_targets(algorithm, parameters):
    targets = {}
    for definition in algorithm.parameterDefinitions():
        is_destination = getattr(definition, "isDestination", None)
        if not callable(is_destination) or not is_destination():
            continue
        name = definition.name()
        path = _local_output_path(parameters.get(name))
        if path is not None:
            targets[name] = path
    return targets


def _request_project_loading(algorithm, parameters):
    prepared = dict(parameters)
    project = QgsProject.instance()
    for definition in algorithm.parameterDefinitions():
        is_destination = getattr(definition, "isDestination", None)
        if not callable(is_destination) or not is_destination():
            continue
        class_name = type(definition).__name__.casefold()
        if not any(
            token in class_name
            for token in ("featuresink", "vector", "raster", "pointcloud", "mesh")
        ):
            continue
        name = definition.name()
        value = prepared.get(name)
        if isinstance(value, str) and value:
            prepared[name] = QgsProcessingOutputLayerDefinition(value, project)
    return prepared


def _local_output_path(value):
    if isinstance(value, QgsProcessingOutputLayerDefinition):
        sink = value.sink
        static_value = getattr(sink, "staticValue", None)
        value = static_value() if callable(static_value) else None
    if not isinstance(value, (str, os.PathLike)):
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"temporary_output", "memory:"}:
        return None
    text = text.split("|", 1)[0]
    parsed = urlparse(text)
    if parsed.scheme == "file":
        text = unquote(parsed.path.lstrip("/")) if os.name == "nt" else unquote(parsed.path)
    elif parsed.scheme and len(parsed.scheme) > 1:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        return None
    return path.resolve(strict=False)


def _loaded_output_conflicts(targets):
    by_path = {_canonical_path(path): name for name, path in targets.items() if path.exists()}
    conflicts = []
    if not by_path:
        return conflicts
    for layer in QgsProject.instance().mapLayers().values():
        source = str(layer.source() or "").split("|", 1)[0]
        source_path = _local_output_path(source)
        if source_path is None:
            continue
        canonical = _canonical_path(source_path)
        if canonical in by_path:
            conflicts.append(
                {
                    "output": by_path[canonical],
                    "path": str(source_path),
                    "layer_id": layer.id(),
                    "layer_name": layer.name(),
                }
            )
    return conflicts


def _canonical_path(path):
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _file_fingerprint(path):
    path = Path(path)
    if not path.is_file():
        return {"exists": False}
    try:
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            digest.update(stream.read(65536))
            if stat.st_size > 65536:
                stream.seek(max(0, stat.st_size - 65536))
                digest.update(stream.read(65536))
        return {
            "exists": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sample_sha256": digest.hexdigest(),
        }
    except OSError as exc:
        return {"exists": True, "readable": False, "error": str(exc)}


def _validate_processing_result(operation, results):
    issues = []
    feedback = operation.get("feedback_log", "")
    for pattern, code in _FEEDBACK_FAILURES:
        if pattern.search(feedback):
            issues.append(
                {
                    "code": "feedback.{}".format(code),
                    "message": "Processing feedback contains {}".format(code.replace("_", " ")),
                }
            )
    output_state = {}
    for name, path in operation.get("_output_targets", {}).items():
        before = operation.get("_output_baseline", {}).get(name, {"exists": False})
        after = _file_fingerprint(path)
        output_state[name] = {"path": str(path), "before": before, "after": after}
        if not after.get("exists"):
            issues.append(
                {
                    "code": "output.missing",
                    "output": name,
                    "message": "Expected output file was not created: {}".format(path),
                }
            )
        elif before.get("exists") and before == after:
            issues.append(
                {
                    "code": "output.unchanged",
                    "output": name,
                    "message": "Existing output file was not modified: {}".format(path),
                }
            )
    return {"passed": not issues, "issues": issues, "outputs": output_state}


def _layer_hint(definition):
    name = type(definition).__name__.casefold() if definition is not None else ""
    hints = QgsProcessingUtils.LayerHint
    if "raster" in name:
        return hints.Raster
    if "vector" in name:
        return hints.Vector
    if "pointcloud" in name:
        return hints.PointCloud
    if "mesh" in name:
        return hints.Mesh
    return hints.UnknownType
