from __future__ import annotations

import json
import os
import sys
import time
import unittest

import qgis.utils
from qgis.core import QgsProcessing, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QProcess, QProcessEnvironment
from qgis_agent_mcp.dispatcher import DispatchError


class QgisRuntimeTest(unittest.TestCase):
    def test_01_plugin_boots_in_qgis_ltr(self):
        plugin = qgis.utils.plugins.get("qgis_agent_mcp")
        self.assertIsNotNone(plugin, "qgis_setup.sh did not load the plugin")
        self.assertIsNotNone(plugin.dispatcher)
        self.assertTrue(plugin.bridge.server.isListening())
        self.assertGreater(plugin.bridge.port, 0)
        self.assertTrue(os.path.isfile(os.environ["QGIS_MCP_CONNECTION_FILE"]))

    def test_02_mcp_stdio_reaches_live_pyqgis_session(self):
        layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "mcp-e2e", "memory")
        self.assertTrue(layer.isValid())
        QgsProject.instance().addMapLayer(layer)
        process = QProcess()
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONPATH", os.environ["PYTHONPATH"])
        environment.insert("QGIS_MCP_CONNECTION_FILE", os.environ["QGIS_MCP_CONNECTION_FILE"])
        process.setProcessEnvironment(environment)
        process.setProgram(sys.executable)
        process.setArguments(["-m", "qgis_mcp"])
        process.start()
        self.assertTrue(process.waitForStarted(5000), process.errorString())
        try:
            initialized = _rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "clientInfo": {"name": "qgis-ltr-smoke", "version": "1"},
                    },
                },
            )
            self.assertEqual(initialized["result"]["serverInfo"]["name"], "qgis-agent-mcp")
            snapshot = _rpc(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "qgis_session_snapshot",
                        "arguments": {"detail": "summary"},
                    },
                },
            )
            layers = snapshot["result"]["structuredContent"]["layers"]
            self.assertIn("mcp-e2e", {item["name"] for item in layers})
        finally:
            process.terminate()
            if not process.waitForFinished(3000):
                process.kill()
                process.waitForFinished(3000)
            QgsProject.instance().removeMapLayer(layer.id())

    def test_03_revision_preconditions_and_idempotency_are_enforced(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        revision = dispatcher.state.revision
        params = {
            "action": "refresh",
            "idempotency_key": "qgis-ltr-refresh-1",
            "if_revision": revision,
        }
        first = dispatcher.dispatch("project.action", params)
        after_first = dispatcher.state.revision
        second = dispatcher.dispatch("project.action", params)
        self.assertEqual(first, second)
        self.assertEqual(dispatcher.state.revision, after_first)
        with self.assertRaises(DispatchError) as raised:
            dispatcher.dispatch(
                "project.action",
                {
                    "action": "refresh",
                    "idempotency_key": "qgis-ltr-refresh-stale",
                    "if_revision": revision,
                },
            )
        self.assertEqual(raised.exception.code, -32040)

    def test_04_processing_schema_and_temporary_output_retention(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        description = dispatcher.capabilities_describe("processing", "native:buffer")
        self.assertEqual(description["input_schema"]["type"], "object")
        self.assertIn("INPUT", description["input_schema"]["properties"])
        self.assertIn("OUTPUT", description["output_schema"]["properties"])

        source = QgsVectorLayer("Point?crs=EPSG:4326", "processing-source", "memory")
        QgsProject.instance().addMapLayer(source)
        try:
            operation = dispatcher.processing_start(
                "native:buffer",
                {
                    "INPUT": source.id(),
                    "DISTANCE": 10,
                    "SEGMENTS": 5,
                    "DISSOLVE": False,
                    "END_CAP_STYLE": 0,
                    "JOIN_STYLE": 0,
                    "MITER_LIMIT": 2,
                    "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
                },
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
                operation = dispatcher.operation_control(operation["id"])
                if operation["status"] not in {"queued", "running", "cancelling"}:
                    break
                time.sleep(0.02)
            self.assertEqual(operation["status"], "succeeded", operation)
            retained = operation["retained_outputs"]["OUTPUT"]
            self.assertEqual(retained["kind"], "layer")
            self.assertEqual(retained["ownership"], "operation_store")
            self.assertEqual(
                dispatcher._layer(retained["layer_id"]).id(), retained["layer_id"]
            )
        finally:
            QgsProject.instance().removeMapLayer(source.id())

    def test_05_binary_artifacts_are_bounded_and_chunked(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        screenshot = dispatcher.ui_screenshot(max_width=256, as_artifact=True)
        artifact = screenshot["artifact"]
        chunk = dispatcher.artifact_read(artifact["artifact_id"], length=128)
        self.assertLessEqual(chunk["length"], 128)
        self.assertEqual(chunk["encoding"], "base64")
        self.assertTrue(dispatcher.artifact_release(artifact["artifact_id"])["released"])


def _rpc(process, request, timeout_ms=15000):
    process.write((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
    process.waitForBytesWritten(2000)
    deadline = time.monotonic() + timeout_ms / 1000
    buffered = bytearray()
    while time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
        process.waitForReadyRead(50)
        buffered.extend(bytes(process.readAllStandardOutput()))
        while b"\n" in buffered:
            line, _, remainder = buffered.partition(b"\n")
            buffered = bytearray(remainder)
            if not line.strip():
                continue
            response = json.loads(line)
            if response.get("id") == request["id"]:
                if "error" in response:
                    raise AssertionError(response["error"])
                return response
        if process.state() == QProcess.NotRunning:
            error = bytes(process.readAllStandardError()).decode("utf-8", "replace")
            raise AssertionError("MCP process exited early: " + error)
    error = bytes(process.readAllStandardError()).decode("utf-8", "replace")
    raise AssertionError("Timed out waiting for MCP response: " + error)
