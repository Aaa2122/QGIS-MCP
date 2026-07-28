from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from qgis.PyQt.QtCore import QObject, QTimer


class WorkflowManager(QObject):
    def __init__(
        self,
        dispatch,
        checkpoints,
        state,
        log,
        root=None,
        mutation_methods=None,
    ):
        super().__init__()
        self.dispatch = dispatch
        self.checkpoints = checkpoints
        self.state = state
        self.log = log
        self.mutation_methods = set(mutation_methods or ())
        self.root = Path(root or Path.home() / ".qgis-mcp" / "workflows")
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.run_due)
        self.timer.start()
        if self._mark_interrupted():
            QTimer.singleShot(0, self.resume_interrupted)

    def close(self):
        self.timer.stop()

    def execute(
        self,
        action,
        workflow_id=None,
        name=None,
        steps=None,
        interval_seconds=None,
        enabled=False,
        atomic=True,
        resume=False,
        resume_on_restart=True,
    ):
        if action == "create":
            return self.create(
                name,
                steps,
                interval_seconds,
                enabled,
                atomic,
                resume_on_restart,
            )
        if action == "list":
            return {"workflows": [self._public(item) for item in self._all()]}
        if action == "inspect":
            return self._public(self._get(workflow_id), include_steps=True)
        if action in {"run", "resume"}:
            return self.run(workflow_id, resume=resume or action == "resume")
        if action in {"enable", "disable"}:
            workflow = self._get(workflow_id)
            workflow["enabled"] = action == "enable"
            workflow["next_run_at"] = time.time() if action == "enable" else None
            self._save(workflow)
            return self._public(workflow)
        if action == "delete":
            return {"workflow_id": workflow_id, "deleted": self.delete(workflow_id)}
        raise ValueError("Unknown workflow action")

    def create(
        self,
        name,
        steps,
        interval_seconds=None,
        enabled=False,
        atomic=True,
        resume_on_restart=True,
    ):
        if not name:
            raise ValueError("name is required")
        if not isinstance(steps, list) or not steps or len(steps) > 100:
            raise ValueError("steps must contain between 1 and 100 calls")
        normalized = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not step.get("method"):
                raise ValueError("Workflow step {} requires a method".format(index))
            if step["method"] == "workflow.execute":
                raise ValueError("A workflow cannot invoke another workflow")
            normalized.append(
                {
                    "method": str(step["method"]),
                    "params": dict(step.get("params") or {}),
                    "continue_on_error": bool(step.get("continue_on_error", False)),
                }
            )
        interval = None if interval_seconds is None else max(60, int(interval_seconds))
        workflow_id = uuid.uuid4().hex
        now = time.time()
        workflow = {
            "workflow_id": workflow_id,
            "name": str(name),
            "created_at": now,
            "updated_at": now,
            "enabled": bool(enabled),
            "interval_seconds": interval,
            "next_run_at": now if enabled else None,
            "atomic": bool(atomic),
            "resume_on_restart": bool(resume_on_restart),
            "steps": normalized,
            "status": "ready",
            "current_step": 0,
            "run_count": 0,
            "last_started_at": None,
            "last_finished_at": None,
            "last_results": [],
            "active_run_id": None,
            "last_run_id": None,
        }
        self._save(workflow)
        self.state.touch("workflow.created", {"workflow_id": workflow_id})
        return self._public(workflow, include_steps=True)

    def run(self, workflow_id, resume=False):
        workflow = self._get(workflow_id)
        if workflow["status"] == "running":
            raise ValueError("Workflow is already running")
        start_index = int(workflow.get("current_step", 0)) if resume else 0
        if start_index >= len(workflow["steps"]):
            start_index = 0
        if resume and workflow.get("active_run_id"):
            run_id = workflow["active_run_id"]
        else:
            run_id = uuid.uuid4().hex
        workflow["status"] = "running"
        workflow["active_run_id"] = run_id
        workflow["current_step"] = start_index
        workflow["last_started_at"] = time.time()
        workflow["last_results"] = workflow.get("last_results", [])[:start_index] if resume else []
        self._save(workflow)
        checkpoint = None
        if workflow.get("atomic"):
            checkpoint = self.checkpoints.create(
                "Workflow {}".format(workflow["name"]), internal=True
            )
        failed = False
        try:
            for index in range(start_index, len(workflow["steps"])):
                step = workflow["steps"][index]
                entry = {"index": index, "method": step["method"], "started_at": time.time()}
                workflow["current_step"] = index
                self._save(workflow)
                step_failed = False
                try:
                    params = dict(step["params"])
                    if step["method"] in self.mutation_methods:
                        params.setdefault(
                            "idempotency_key",
                            "workflow:{}:{}:{}".format(workflow_id, run_id, index),
                        )
                    result = self.dispatch(step["method"], params)
                    entry["result"] = self._bounded(result)
                    workflow["current_step"] = index + 1
                except Exception as exc:
                    failed = True
                    step_failed = True
                    entry["error"] = {
                        "code": getattr(exc, "code", -32010),
                        "message": getattr(exc, "message", str(exc)),
                        "data": self._bounded(getattr(exc, "data", None)),
                    }
                    workflow["current_step"] = (
                        index + 1 if step["continue_on_error"] else index
                    )
                entry["finished_at"] = time.time()
                workflow["last_results"].append(entry)
                workflow["updated_at"] = time.time()
                self._save(workflow)
                if step_failed and (
                    workflow.get("atomic") or not step["continue_on_error"]
                ):
                    break
            if failed and checkpoint:
                self.checkpoints.restore(checkpoint["checkpoint_id"])
                workflow["current_step"] = 0
                workflow["active_run_id"] = None
            workflow["status"] = "failed" if failed else "completed"
            if not failed:
                workflow["last_run_id"] = run_id
                workflow["active_run_id"] = None
            workflow["run_count"] = int(workflow.get("run_count", 0)) + 1
            workflow["last_finished_at"] = time.time()
            workflow["next_run_at"] = (
                time.time() + workflow["interval_seconds"]
                if workflow.get("enabled") and workflow.get("interval_seconds")
                else None
            )
            self._save(workflow)
        finally:
            if checkpoint:
                self.checkpoints.delete(checkpoint["checkpoint_id"])
        self.state.touch(
            "workflow.finished",
            {"workflow_id": workflow_id, "status": workflow["status"]},
        )
        return self._public(workflow, include_steps=True)

    def resume_interrupted(self):
        for workflow in self._all():
            if workflow.get("status") != "interrupted" or not workflow.get(
                "resume_on_restart", True
            ):
                continue
            try:
                self.run(workflow["workflow_id"], resume=True)
            except Exception as exc:
                self.log.add(
                    "workflow.recovery_error",
                    str(exc),
                    "error",
                    {"workflow_id": workflow["workflow_id"]},
                )

    def _mark_interrupted(self):
        recoverable = False
        for workflow in self._all():
            if workflow.get("status") != "running":
                continue
            workflow["status"] = "interrupted"
            workflow["interrupted_at"] = time.time()
            self._save(workflow)
            recoverable = recoverable or workflow.get("resume_on_restart", True)
        return recoverable

    def run_due(self):
        now = time.time()
        for workflow in self._all():
            if (
                workflow.get("enabled")
                and workflow.get("next_run_at") is not None
                and float(workflow["next_run_at"]) <= now
                and workflow.get("status") != "running"
            ):
                try:
                    self.run(workflow["workflow_id"])
                except Exception as exc:
                    self.log.add(
                        "workflow.schedule_error",
                        str(exc),
                        "error",
                        {"workflow_id": workflow["workflow_id"]},
                    )

    def delete(self, workflow_id):
        path = self._path(workflow_id)
        if not path.is_file():
            raise ValueError("Workflow not found: {}".format(workflow_id))
        path.unlink()
        self.state.touch("workflow.deleted", {"workflow_id": workflow_id})
        return True

    def _all(self):
        if not self.root.exists():
            return []
        values = []
        for path in self.root.glob("*.json"):
            try:
                values.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(values, key=lambda item: item.get("updated_at", 0), reverse=True)

    def _get(self, workflow_id):
        path = self._path(workflow_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Workflow not found: {}".format(workflow_id)) from exc

    def _path(self, workflow_id):
        if not workflow_id or not re.fullmatch(r"[0-9a-f]{32}", str(workflow_id)):
            raise ValueError("Invalid workflow ID")
        return self.root / "{}.json".format(workflow_id)

    def _save(self, workflow):
        self.root.mkdir(parents=True, exist_ok=True)
        workflow["updated_at"] = time.time()
        path = self._path(workflow["workflow_id"])
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(self.root))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(workflow, stream, ensure_ascii=False, indent=2, default=str)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _public(workflow, include_steps=False):
        hidden = {"steps"} if not include_steps else set()
        return {key: value for key, value in workflow.items() if key not in hidden}

    @staticmethod
    def _bounded(value):
        encoded = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        if len(encoded) <= 65536:
            return value
        return {"truncated": True, "estimated_bytes": len(encoded)}
