from __future__ import annotations

import time
import uuid
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsMapLayer,
    QgsMapLayerStore,
    QgsProcessingAlgorithm,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
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
        self, algorithm_id, parameters, retain_outputs=True, add_to_project=False
    ):
        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(algorithm_id)
        if algorithm is None:
            raise KeyError("Processing algorithm not found: {}".format(algorithm_id))
        operation_id = "op_" + uuid.uuid4().hex
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = QgsProcessingFeedback()
        no_threading = bool(
            algorithm.flags() & QgsProcessingAlgorithm.Flag.FlagNoThreading
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
        operation["status"] = "succeeded" if successful else (
            "cancelled"
            if operation["_feedback"].isCanceled()
            or (operation["_task"] is not None and operation["_task"].isCanceled())
            else "failed"
        )
        operation["progress"] = 100.0 if successful else operation["progress"]
        operation["updated"] = time.time()
        operation["result"] = json_safe(results)
        operation["retained_outputs"] = self._retain_outputs(operation, results)
        try:
            operation["feedback_log"] = operation["_feedback"].textLog()
        except Exception:
            operation["feedback_log"] = ""
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

    def _retain_outputs(self, operation, results):
        retained = {}
        context = operation["_context"]
        project = QgsProject.instance()
        for name, value in (results or {}).items():
            layer = value if isinstance(value, QgsMapLayer) else None
            if layer is None and isinstance(value, str):
                try:
                    layer = context.getMapLayer(value)
                except Exception:
                    layer = None
            if layer is not None and operation["retain_outputs"]:
                try:
                    owned = context.takeResultLayer(layer.id()) or layer
                except Exception:
                    owned = layer
                if operation["add_to_project"]:
                    if project.mapLayer(owned.id()) is None:
                        project.addMapLayer(owned)
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
                    "ownership": "project" if operation["add_to_project"] else "operation_store",
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
