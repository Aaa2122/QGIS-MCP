from __future__ import annotations

import json
import os
import statistics
import time
import unittest
from pathlib import Path

import qgis.utils
from qgis.core import Qgis, QgsProject, QgsVectorLayer


class LargeProjectBenchmark(unittest.TestCase):
    def test_snapshot_latency_with_500_layers(self):
        project = QgsProject.instance()
        layers = [
            QgsVectorLayer(
                "Point?crs=EPSG:4326&field=value:integer",
                "benchmark-{:04d}".format(index),
                "memory",
            )
            for index in range(500)
        ]
        project.addMapLayers(layers, False)
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        try:
            durations = []
            for _ in range(7):
                started = time.perf_counter()
                snapshot = dispatcher.session_snapshot("summary")
                durations.append((time.perf_counter() - started) * 1000)
                self.assertGreaterEqual(snapshot["project"]["layer_count"], 500)
            ordered = sorted(durations)
            result = {
                "qgis_version": Qgis.QGIS_VERSION,
                "layer_count": 500,
                "samples_ms": [round(value, 3) for value in durations],
                "median_ms": round(statistics.median(durations), 3),
                "p95_ms": round(ordered[-1], 3),
                "budget_ms": float(os.environ.get("QGIS_MCP_BENCHMARK_BUDGET_MS", "5000")),
            }
            output = Path(
                os.environ.get(
                    "QGIS_MCP_BENCHMARK_OUTPUT", "benchmark-results/qgis-ltr.json"
                )
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            self.assertLess(result["p95_ms"], result["budget_ms"])
        finally:
            project.removeMapLayers([layer.id() for layer in layers])
