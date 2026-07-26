from __future__ import annotations

import statistics
import time

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProject,
    QgsProjectServerValidator,
    QgsProviderRegistry,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, qVersion

from .serialize import layer_summary


class QaTools:
    def __init__(self, iface, state, verifier, method_provider):
        self.iface = iface
        self.state = state
        self.verifier = verifier
        self.method_provider = method_provider

    def compatibility(self, detail="standard"):
        registry = QgsProviderRegistry.instance()
        processing = QgsApplication.processingRegistry()
        providers = []
        for key in registry.providerList():
            metadata = registry.providerMetadata(key)
            providers.append(
                {
                    "key": key,
                    "description": metadata.description() if metadata else None,
                    "connection_api": bool(
                        metadata and callable(getattr(metadata, "connections", None))
                    ),
                }
            )
        processing_providers = []
        algorithm_count = 0
        for provider in processing.providers():
            algorithms = list(provider.algorithms())
            algorithm_count += len(algorithms)
            processing_providers.append(
                {
                    "id": provider.id(),
                    "name": provider.name(),
                    "active": provider.isActive(),
                    "algorithm_count": len(algorithms),
                }
            )
        methods = self.method_provider()
        result = {
            "qgis": {
                "version": Qgis.QGIS_VERSION,
                "version_int": Qgis.QGIS_VERSION_INT,
                "release_name": Qgis.QGIS_RELEASE_NAME,
                "profile_path": QgsApplication.qgisSettingsDirPath(),
            },
            "qt": {
                "runtime": qVersion(),
                "compiled": QT_VERSION_STR,
                "pyqt": PYQT_VERSION_STR,
            },
            "bridge": {
                "method_count": len(methods),
                "methods": sorted(methods) if detail == "full" else None,
                "state_revision": self.state.revision,
            },
            "providers": providers,
            "processing": {
                "providers": processing_providers,
                "algorithm_count": algorithm_count,
            },
            "layer_classes": sorted(
                {
                    type(layer).__name__
                    for layer in QgsProject.instance().mapLayers().values()
                }
            ),
        }
        if detail == "summary":
            result["providers"] = [item["key"] for item in providers]
            result["processing"] = {
                "provider_count": len(processing_providers),
                "algorithm_count": algorithm_count,
            }
        return result

    def project_audit(
        self,
        geometry_sample=100,
        require_layout=False,
        require_saved=False,
        include_server=True,
        include_metadata=True,
    ):
        project = QgsProject.instance()
        base = self.verifier.verify(
            geometry_sample=geometry_sample,
            require_layout=require_layout,
            require_saved=require_saved,
        )
        issues = list(base["issues"])
        names = {}
        for layer in project.mapLayers().values():
            names.setdefault(layer.name(), []).append(layer.id())
            if include_metadata:
                metadata = layer.metadata()
                if not metadata.title().strip():
                    issues.append(
                        _issue(
                            "warning",
                            "metadata.title_missing",
                            "Layer metadata has no title",
                            layer.id(),
                        )
                    )
                if not metadata.abstract().strip():
                    issues.append(
                        _issue(
                            "info",
                            "metadata.abstract_missing",
                            "Layer metadata has no abstract",
                            layer.id(),
                        )
                    )
            if isinstance(layer, QgsVectorLayer):
                duplicate_fields = _duplicates([field.name() for field in layer.fields()])
                for name in duplicate_fields:
                    issues.append(
                        _issue(
                            "error",
                            "schema.duplicate_field",
                            "Duplicate field name: {}".format(name),
                            layer.id(),
                        )
                    )
        for name, layer_ids in names.items():
            if len(layer_ids) > 1:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "layer.duplicate_name",
                        "message": "Multiple layers use the name: {}".format(name),
                        "layer_ids": layer_ids,
                    }
                )
        server = None
        if include_server:
            valid, results = QgsProjectServerValidator.validate(project)
            server = {
                "valid": bool(valid),
                "issues": [
                    {
                        "error": int(item.error),
                        "message": QgsProjectServerValidator.displayValidationError(item.error),
                        "identifier": str(item.identifier),
                    }
                    for item in results
                ],
            }
            for item in server["issues"]:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "server.validation",
                        "message": item["message"],
                        "identifier": item["identifier"],
                    }
                )
        errors = sum(item["severity"] == "error" for item in issues)
        warnings = sum(item["severity"] == "warning" for item in issues)
        return {
            "status": "failed" if errors else "passed_with_warnings" if warnings else "passed",
            "errors": errors,
            "warnings": warnings,
            "info": sum(item["severity"] == "info" for item in issues),
            "issues": issues,
            "server": server,
            "project": {
                "file": project.fileName(),
                "dirty": project.isDirty(),
                "layer_count": len(project.mapLayers()),
                "layout_count": len(project.layoutManager().layouts()),
            },
        }

    def benchmark(self, action="run", iterations=20, include_layers=True):
        if action not in {"run", "baseline"}:
            raise ValueError("Unknown benchmark action")
        iterations = max(1, min(int(iterations), 200))
        project = QgsProject.instance()
        samples = []
        layer_samples = []
        layers = list(project.mapLayers().values())
        for _ in range(iterations):
            started = time.perf_counter()
            _ = project.layerTreeRoot().dump()
            _ = project.crs().authid()
            samples.append((time.perf_counter() - started) * 1000)
            if include_layers:
                started = time.perf_counter()
                _ = [layer_summary(layer) for layer in layers]
                layer_samples.append((time.perf_counter() - started) * 1000)
        result = {
            "iterations": iterations,
            "layer_count": len(layers),
            "project_snapshot_ms": _latencies(samples),
            "layer_summary_ms": _latencies(layer_samples) if layer_samples else None,
        }
        if action == "baseline":
            result["baseline"] = {
                "qgis_version": Qgis.QGIS_VERSION,
                "project_file": project.fileName(),
                "created_at": time.time(),
            }
        return result

    def self_test(self):
        checks = []

        def check(name, callback):
            started = time.perf_counter()
            try:
                value = callback()
                checks.append(
                    {
                        "name": name,
                        "ok": True,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "value": value,
                    }
                )
            except Exception as exc:
                checks.append(
                    {
                        "name": name,
                        "ok": False,
                        "latency_ms": (time.perf_counter() - started) * 1000,
                        "error": str(exc),
                    }
                )

        check("iface", lambda: type(self.iface).__name__)
        check("project", lambda: len(QgsProject.instance().mapLayers()))
        check("canvas", lambda: [self.iface.mapCanvas().width(), self.iface.mapCanvas().height()])
        check("providers", lambda: len(QgsProviderRegistry.instance().providerList()))
        check(
            "processing",
            lambda: sum(
                len(provider.algorithms())
                for provider in QgsApplication.processingRegistry().providers()
            ),
        )
        check("bridge_methods", lambda: len(self.method_provider()))
        return {
            "ok": all(item["ok"] for item in checks),
            "checks": checks,
            "qgis_version": Qgis.QGIS_VERSION,
        }


def _issue(severity, code, message, layer_id=None):
    result = {"severity": severity, "code": code, "message": message}
    if layer_id:
        result["layer_id"] = layer_id
    return result


def _duplicates(values):
    return sorted({value for value in values if values.count(value) > 1})


def _latencies(samples):
    ordered = sorted(samples)
    return {
        "minimum": ordered[0],
        "mean": statistics.fmean(ordered),
        "median": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "maximum": ordered[-1],
    }
