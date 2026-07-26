from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from qgis.core import QgsProject, QgsVectorLayer

from .data_sources import DataAcquisitionManager


class CheckpointManager:
    def __init__(self, state, root=None, max_checkpoints=20):
        self.state = state
        self.root = Path(root or Path.home() / ".qgis-mcp" / "checkpoints")
        self.max_checkpoints = int(max_checkpoints)

    def execute(self, action, checkpoint_id=None, name=None):
        if action == "create":
            return self.create(name)
        if action == "list":
            return {"checkpoints": self.list()}
        if action == "restore":
            return self.restore(checkpoint_id)
        if action == "delete":
            return {"checkpoint_id": checkpoint_id, "deleted": self.delete(checkpoint_id)}
        raise ValueError("Unknown checkpoint action")

    def create(self, name=None, internal=False):
        project = QgsProject.instance()
        checkpoint_id = uuid.uuid4().hex
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or "checkpoint")).strip("-")[:80]
        safe_name = safe_name or "checkpoint"
        directory = self.root / checkpoint_id
        directory.mkdir(parents=True, exist_ok=False)
        project_path = directory / "{}.qgz".format(safe_name)
        original_file = project.fileName()
        original_dirty = project.isDirty()
        if not project.write(str(project_path)):
            raise RuntimeError("QGIS could not write the checkpoint")
        project.setFileName(original_file)
        project.setDirty(original_dirty)
        metadata = {
            "checkpoint_id": checkpoint_id,
            "name": name or "Checkpoint",
            "created_at": time.time(),
            "project_path": str(project_path),
            "original_file": original_file,
            "original_dirty": original_dirty,
            "layer_count": len(project.mapLayers()),
            "internal": bool(internal),
        }
        self._write_json(directory / "metadata.json", metadata)
        self._prune()
        self.state.touch("checkpoint.created", {"checkpoint_id": checkpoint_id})
        return metadata

    def list(self):
        values = []
        if not self.root.exists():
            return values
        for metadata_path in self.root.glob("*/metadata.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if Path(metadata["project_path"]).is_file():
                    values.append(metadata)
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
        return sorted(values, key=lambda item: item["created_at"], reverse=True)

    def restore(self, checkpoint_id):
        metadata = self._get(checkpoint_id)
        project = QgsProject.instance()
        if not project.read(metadata["project_path"]):
            raise RuntimeError("QGIS could not restore the checkpoint")
        project.setFileName(metadata.get("original_file") or "")
        project.setDirty(True)
        self.state.touch("checkpoint.restored", {"checkpoint_id": checkpoint_id})
        return {
            "checkpoint_id": checkpoint_id,
            "restored": True,
            "project_file": project.fileName(),
            "layer_count": len(project.mapLayers()),
        }

    def delete(self, checkpoint_id):
        metadata = self._get(checkpoint_id)
        directory = Path(metadata["project_path"]).parent.resolve()
        if directory.parent != self.root.resolve():
            raise ValueError("Unsafe checkpoint path")
        for path in directory.iterdir():
            if path.is_file():
                path.unlink()
        directory.rmdir()
        self.state.touch("checkpoint.deleted", {"checkpoint_id": checkpoint_id})
        return True

    def _get(self, checkpoint_id):
        if not checkpoint_id or not re.fullmatch(r"[0-9a-f]{32}", str(checkpoint_id)):
            raise ValueError("Invalid checkpoint ID")
        path = self.root / str(checkpoint_id) / "metadata.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Checkpoint not found: {}".format(checkpoint_id)) from exc

    def _prune(self):
        for metadata in self.list()[self.max_checkpoints :]:
            self.delete(metadata["checkpoint_id"])

    @staticmethod
    def _write_json(path, value):
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class ProjectVerifier:
    def verify(self, geometry_sample=100, require_layout=False, require_saved=False):
        project = QgsProject.instance()
        issues = []
        if require_saved and not project.fileName():
            issues.append(self._issue("error", "project.unsaved", "Project has no file path"))
        if not project.crs().isValid():
            issues.append(self._issue("error", "project.crs", "Project CRS is invalid"))
        if not project.mapLayers():
            issues.append(self._issue("warning", "project.empty", "Project contains no layers"))
        if require_layout and not project.layoutManager().layouts():
            issues.append(self._issue("error", "layout.missing", "Project contains no print layout"))
        for layer in project.mapLayers().values():
            if not layer.isValid():
                issues.append(self._issue("error", "layer.invalid", "Layer is invalid", layer.id()))
                continue
            if not layer.crs().isValid():
                issues.append(self._issue("warning", "layer.crs", "Layer CRS is invalid or missing", layer.id()))
            if isinstance(layer, QgsVectorLayer):
                if layer.isEditable() and layer.isModified():
                    issues.append(self._issue("warning", "layer.uncommitted", "Layer has uncommitted edits", layer.id()))
                invalid_count = 0
                checked = 0
                for feature in layer.getFeatures():
                    if checked >= max(0, min(int(geometry_sample), 1000)):
                        break
                    geometry = feature.geometry()
                    if geometry and not geometry.isNull() and not geometry.isGeosValid():
                        invalid_count += 1
                    checked += 1
                if invalid_count:
                    issues.append(
                        self._issue(
                            "warning",
                            "geometry.invalid",
                            "{} invalid geometries in a sample of {}".format(invalid_count, checked),
                            layer.id(),
                        )
                    )
            provenance = DataAcquisitionManager.provenance(layer)
            if provenance.get("kind") in {"download", "wms", "wfs", "wmts", "xyz"} and not provenance.get("url"):
                issues.append(self._issue("warning", "provenance.incomplete", "Remote layer lacks a recorded source URL", layer.id()))
        errors = sum(item["severity"] == "error" for item in issues)
        warnings = sum(item["severity"] == "warning" for item in issues)
        return {
            "status": "failed" if errors else "passed_with_warnings" if warnings else "passed",
            "errors": errors,
            "warnings": warnings,
            "issues": issues,
            "checked_layers": len(project.mapLayers()),
            "checked_layouts": len(project.layoutManager().layouts()),
        }

    @staticmethod
    def _issue(severity, code, message, layer_id=None):
        value = {"severity": severity, "code": code, "message": message}
        if layer_id:
            value["layer_id"] = layer_id
        return value
