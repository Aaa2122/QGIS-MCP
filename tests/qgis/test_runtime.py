from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import qgis.utils
from qgis.core import QgsApplication, QgsProcessing, QgsProject, QgsVectorLayer
from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QProcess, QProcessEnvironment
from qgis_agent_mcp.autonomy import DataCache, NetworkPolicy
from qgis_agent_mcp.dispatcher import DispatchError
from qgis_agent_mcp.onboarding import RuntimeManager, health_check


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
        environment.insert(
            "PYTHONPATH",
            os.environ.get("QGIS_MCP_TEST_PYTHONPATH", os.environ["PYTHONPATH"]),
        )
        environment.insert("QGIS_MCP_CONNECTION_FILE", os.environ["QGIS_MCP_CONNECTION_FILE"])
        process.setProcessEnvironment(environment)
        process.setProgram(os.environ.get("QGIS_MCP_TEST_PYTHON", sys.executable))
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

    def test_06_secure_data_acquisition_cache_and_provenance(self):
        payload = b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"name":"Paris"},"geometry":{"type":"Point","coordinates":[2.35,48.86]}}]}'

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/geo+json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        previous_policy = dispatcher.data.policy
        previous_cache = dispatcher.data.cache
        with tempfile.TemporaryDirectory() as directory:
            dispatcher.data.policy = NetworkPolicy(allow_private=True)
            dispatcher.data.cache = DataCache(directory)
            try:
                url = "http://127.0.0.1:{}/fires.geojson".format(server.server_port)
                first = dispatcher.dispatch("data.fetch", {"url": url, "name": "downloaded-data"})
                layer_id = first["layers"][0]["id"]
                self.assertFalse(first["download"]["cache_hit"])
                self.assertEqual(dispatcher.data.provenance(dispatcher._layer(layer_id))["kind"], "download")
                second = dispatcher.dispatch("data.fetch", {"url": url, "add_to_project": False})
                self.assertTrue(second["download"]["cache_hit"])
            finally:
                dispatcher.data.policy = previous_policy
                dispatcher.data.cache = previous_cache
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                for layer in QgsProject.instance().mapLayersByName("downloaded-data"):
                    QgsProject.instance().removeMapLayer(layer.id())

    def test_07_cartography_layout_dry_run_and_atomic_rollback(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        created = dispatcher.dispatch(
            "layer.manage",
            {
                "action": "create_memory",
                "name": "autonomy-map",
                "geometry": "Point",
                "fields": [{"name": "kind", "type": "string"}],
            },
        )
        layer_id = created["id"]
        try:
            dispatcher.vector_edit(
                layer_id,
                "add",
                features=[{"attributes": {"kind": "fire"}, "geometry_wkt": "POINT(2.35 48.86)"}],
            )
            dispatcher.vector_edit(layer_id, "commit")
            simple = dispatcher.dispatch(
                "cartography.style",
                {"layer": layer_id, "mode": "simple", "color": "#ff3b1f"},
            )
            self.assertEqual(simple["layer_id"], layer_id)
            styled = dispatcher.dispatch(
                "cartography.style",
                {"layer": layer_id, "mode": "categorized", "field": "kind", "color_ramp": "fire"},
            )
            self.assertEqual(styled["layer_id"], layer_id)
            labels = dispatcher.dispatch("cartography.labels", {"layer": layer_id, "field": "kind"})
            self.assertTrue(labels["enabled"])
            layout_name = "QGIS MCP integration layout"
            dispatcher.dispatch("layout.execute", {"action": "create", "name": layout_name})
            output = Path.home() / ".qgis-mcp" / "outputs" / "qgis-mcp-integration.png"
            exported = dispatcher.dispatch(
                "layout.execute",
                {"action": "export", "name": layout_name, "path": str(output), "format": "png", "dpi": 96},
            )
            self.assertTrue(Path(exported["path"]).is_file())
            revision = dispatcher.state.revision
            preview = dispatcher.dispatch(
                "layer.manage",
                {"action": "rename_layer", "layer": layer_id, "name": "not-applied", "dry_run": True},
            )
            self.assertTrue(preview["dry_run"])
            self.assertEqual(dispatcher.state.revision, revision)
            self.assertEqual(dispatcher._layer(layer_id).name(), "autonomy-map")
            batch = dispatcher.dispatch(
                "batch.execute",
                {
                    "atomic": True,
                    "calls": [
                        {"method": "layer.manage", "params": {"action": "rename_layer", "layer": layer_id, "name": "temporary-name"}},
                        {"method": "project.action", "params": {"action": "unsupported"}},
                    ],
                },
            )
            self.assertTrue(batch["rolled_back"])
            self.assertEqual(QgsProject.instance().mapLayersByName("autonomy-map")[0].name(), "autonomy-map")
        finally:
            layout = QgsProject.instance().layoutManager().layoutByName("QGIS MCP integration layout")
            if layout:
                QgsProject.instance().layoutManager().removeLayout(layout)
            for layer in QgsProject.instance().mapLayersByName("autonomy-map"):
                QgsProject.instance().removeMapLayer(layer.id())

    def test_08_durable_workflow_and_fire_connector_contract(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        created = dispatcher.dispatch(
            "workflow.execute",
            {
                "action": "create",
                "name": "QGIS LTR durable workflow",
                "atomic": False,
                "steps": [
                    {
                        "method": "layer.manage",
                        "params": {"action": "create_group", "name": "durable-test-group"},
                    }
                ],
            },
        )
        workflow_id = created["workflow_id"]
        try:
            finished = dispatcher.dispatch(
                "workflow.execute", {"action": "run", "workflow_id": workflow_id}
            )
            self.assertEqual(finished["status"], "completed")
            self.assertEqual(finished["current_step"], 1)
            inspected = dispatcher.dispatch(
                "workflow.execute", {"action": "inspect", "workflow_id": workflow_id}
            )
            self.assertEqual(inspected["run_count"], 1)
            self.assertIsNotNone(QgsProject.instance().layerTreeRoot().findGroup("durable-test-group"))
            catalog = dispatcher.dispatch("connector.catalog", {})
            self.assertEqual(catalog["connectors"][0]["provider"], "NASA LANCE FIRMS")
            with self.assertRaises(DispatchError):
                dispatcher.dispatch(
                    "connector.fire_map",
                    {"map_key_env": "QGIS_MCP_TEST_MISSING_FIRMS_KEY", "add_satellite": False},
                )
        finally:
            dispatcher.dispatch(
                "workflow.execute", {"action": "delete", "workflow_id": workflow_id}
            )
            group = QgsProject.instance().layerTreeRoot().findGroup("durable-test-group")
            if group:
                group.parent().removeChildNode(group)

    def test_09_onboarding_health_check_keeps_qgis_responsive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_dir = root / "qgis_agent_mcp"
            shutil.copytree(
                Path(__file__).resolve().parents[2] / "src" / "qgis_mcp",
                plugin_dir / "_server" / "qgis_mcp",
            )
            spec = RuntimeManager(
                plugin_dir=plugin_dir,
                qgis_prefix=QgsApplication.prefixPath(),
                home=root / "home",
            ).ensure()
            result = health_check(
                spec,
                event_pump=lambda: QCoreApplication.processEvents(
                    QEventLoop.ExcludeUserInputEvents, 25
                ),
            )
            self.assertIn("revision", result)
            self.assertEqual(
                result["layer_count"], len(QgsProject.instance().mapLayers())
            )

    def test_10_runtime_events_transactions_and_preflight(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        runtime = dispatcher.dispatch("runtime.control", {"action": "compatibility"})
        self.assertIn(runtime["qgis_major"], {3, 4})
        self.assertTrue(runtime["supported"])
        providers = dispatcher.dispatch("runtime.control", {"action": "providers"})
        self.assertIn("ogr", providers["data_providers"])

        layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "transaction-test", "memory")
        QgsProject.instance().addMapLayer(layer)
        try:
            started = dispatcher.dispatch(
                "runtime.transaction", {"action": "start", "layers": [layer.id()]}
            )
            self.assertTrue(started["layers"][0]["editable"])
            dispatcher.vector_edit(
                layer.id(),
                "add",
                features=[{"attributes": {"name": "temporary"}, "geometry_wkt": "POINT(1 2)"}],
            )
            undo = dispatcher.dispatch(
                "runtime.undo", {"action": "undo", "layer": layer.id()}
            )
            self.assertTrue(undo["can_redo"])
            rolled_back = dispatcher.dispatch(
                "runtime.transaction", {"action": "rollback", "layers": [layer.id()]}
            )
            self.assertFalse(rolled_back["layers"][0]["editable"])

            checked = dispatcher.dispatch(
                "runtime.preflight",
                {
                    "calls": [
                        {"method": "project.inspect", "params": {"section": "project"}},
                        {"method": "vector.edit", "params": {"layer": layer.id(), "action": "start"}},
                    ]
                },
            )
            self.assertTrue(checked["valid"])
            self.assertEqual(checked["mutation_count"], 1)
            events = dispatcher.dispatch("runtime.events", {"after_revision": 0})
            self.assertGreater(events["current_revision"], 0)
            diff = dispatcher.dispatch(
                "runtime.diff", {"from_revision": 0, "to_revision": events["current_revision"]}
            )
            self.assertIn("changed_resources", diff)
        finally:
            if layer.isEditable():
                layer.rollBack()
            QgsProject.instance().removeMapLayer(layer.id())

    def test_11_project_canvas_crs_expression_metadata_and_bookmarks(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        layer = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=name:string", "project-tools", "memory"
        )
        QgsProject.instance().addMapLayer(layer)
        dispatcher.vector_edit(
            layer.id(),
            "add",
            features=[
                {
                    "attributes": {"name": "Paris"},
                    "geometry_wkt": "POINT(2.35 48.86)",
                }
            ],
        )
        dispatcher.vector_edit(layer.id(), "commit")
        original_title = QgsProject.instance().title()
        bookmark_id = None
        theme_name = "mcp-project-tools-theme"
        try:
            properties = dispatcher.dispatch(
                "project.properties",
                {
                    "action": "set",
                    "title": "QGIS MCP project tools",
                    "crs": "EPSG:4326",
                    "variables": {"mcp_test": "yes"},
                },
            )
            self.assertEqual(properties["title"], "QGIS MCP project tools")
            self.assertEqual(properties["crs"]["authid"], "EPSG:4326")

            canvas = dispatcher.dispatch(
                "canvas.control",
                {
                    "action": "set_extent",
                    "extent": [1.0, 47.0, 4.0, 50.0],
                    "crs": "EPSG:4326",
                },
            )
            self.assertEqual(canvas["crs"], "EPSG:4326")
            identified = dispatcher.dispatch(
                "map.identify",
                {
                    "point": [2.35, 48.86],
                    "crs": "EPSG:4326",
                    "layers": [layer.id()],
                    "tolerance": 0.01,
                },
            )
            self.assertEqual(
                identified["results"][0]["features"][0]["attributes"]["name"],
                "Paris",
            )

            measured = dispatcher.dispatch(
                "map.measure",
                {
                    "action": "length",
                    "geometry_wkt": "LINESTRING(2.35 48.86,2.36 48.87)",
                    "crs": "EPSG:4326",
                },
            )
            self.assertGreater(measured["value"], 0)
            transformed = dispatcher.dispatch(
                "crs.control",
                {
                    "action": "transform_points",
                    "source": "EPSG:4326",
                    "target": "EPSG:3857",
                    "points": [[2.35, 48.86]],
                },
            )
            self.assertGreater(transformed["points"][0][0], 200000)
            expression = dispatcher.dispatch(
                "expression.control",
                {"action": "evaluate", "expression": "upper('qgis')"},
            )
            self.assertEqual(expression["value"], "QGIS")

            metadata = dispatcher.dispatch(
                "metadata.manage",
                {
                    "action": "set",
                    "layer": layer.id(),
                    "values": {
                        "title": "Project tools layer",
                        "abstract": "MCP integration test",
                    },
                },
            )
            self.assertEqual(metadata["title"], "Project tools layer")
            source = dispatcher.dispatch(
                "layer.source", {"layer": layer.id(), "action": "inspect"}
            )
            self.assertTrue(source["valid"])

            bookmark = dispatcher.dispatch(
                "bookmark.manage",
                {
                    "action": "add",
                    "name": "Paris",
                    "extent": [2.0, 48.5, 2.7, 49.1],
                    "crs": "EPSG:4326",
                },
            )
            bookmark_id = bookmark["id"]
            self.assertEqual(bookmark["name"], "Paris")
            dispatcher.dispatch(
                "map_theme.manage", {"action": "capture", "name": theme_name}
            )
            themes = dispatcher.dispatch("map_theme.manage", {"action": "list"})
            self.assertIn(theme_name, {item["name"] for item in themes["themes"]})
            connections = dispatcher.dispatch(
                "connection.inspect", {"action": "providers"}
            )
            self.assertIn("ogr", connections["providers"])
        finally:
            if bookmark_id:
                dispatcher.dispatch(
                    "bookmark.manage",
                    {"action": "remove", "bookmark_id": bookmark_id},
                )
            if QgsProject.instance().mapThemeCollection().hasMapTheme(theme_name):
                dispatcher.dispatch(
                    "map_theme.manage", {"action": "remove", "name": theme_name}
                )
            QgsProject.instance().setTitle(original_title)
            QgsProject.instance().removeMapLayer(layer.id())

    def test_12_vector_schema_geometry_joins_relations_and_snapping(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        parent = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=code:string", "parent-tools", "memory"
        )
        child = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=parent_code:string", "child-tools", "memory"
        )
        QgsProject.instance().addMapLayers([parent, child])
        relation_id = "mcp-vector-tools-relation"
        try:
            schema = dispatcher.dispatch(
                "vector.schema",
                {
                    "layer": parent.id(),
                    "action": "add",
                    "name": "score",
                    "field_type": "double",
                    "alias": "Score",
                    "default_expression": "1.5",
                },
            )
            self.assertIn("score", {field["name"] for field in schema["fields"]})
            dispatcher.vector_edit(
                parent.id(),
                "add",
                features=[
                    {
                        "attributes": {"code": "A", "score": 5},
                        "geometry_wkt": "POINT(1 1)",
                    }
                ],
            )
            dispatcher.vector_edit(parent.id(), "commit")
            feature_id = next(parent.getFeatures()).id()
            statistics = dispatcher.dispatch(
                "vector.statistics",
                {"layer": parent.id(), "action": "numeric", "field": "score"},
            )
            self.assertEqual(statistics["max"], 5)
            dispatcher.dispatch(
                "vector.geometry",
                {
                    "layer": parent.id(),
                    "feature_ids": [feature_id],
                    "action": "translate",
                    "dx": 2,
                    "dy": 3,
                },
            )
            moved = parent.getFeature(feature_id).geometry().asPoint()
            self.assertAlmostEqual(moved.x(), 3)
            self.assertAlmostEqual(moved.y(), 4)
            parent.commitChanges()

            joined = dispatcher.dispatch(
                "vector.join",
                {
                    "layer": child.id(),
                    "action": "add",
                    "join_layer": parent.id(),
                    "target_field": "parent_code",
                    "join_field": "code",
                    "prefix": "parent_",
                },
            )
            self.assertEqual(joined["joins"][0]["join_layer_id"], parent.id())
            relations = dispatcher.dispatch(
                "project.relation",
                {
                    "action": "add",
                    "relation_id": relation_id,
                    "name": "Parent child",
                    "referenced_layer": parent.id(),
                    "referencing_layer": child.id(),
                    "field_pairs": {"parent_code": "code"},
                },
            )
            self.assertIn(relation_id, {item["id"] for item in relations["relations"]})
            snapping = dispatcher.dispatch(
                "project.snapping",
                {
                    "action": "set",
                    "enabled": True,
                    "mode": "all_layers",
                    "types": ["vertex", "segment"],
                    "tolerance": 12,
                    "units": "pixels",
                },
            )
            self.assertTrue(snapping["enabled"])
            selected = dispatcher.dispatch(
                "selection.advanced", {"layer": parent.id(), "action": "all"}
            )
            self.assertEqual(selected["selected_count"], 1)
        finally:
            manager = QgsProject.instance().relationManager()
            if relation_id in manager.relations():
                manager.removeRelation(relation_id)
            QgsProject.instance().removeMapLayers([parent.id(), child.id()])

    def test_13_processing_batch_assets_and_history(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        source = QgsVectorLayer("Point?crs=EPSG:4326", "batch-source", "memory")
        QgsProject.instance().addMapLayer(source)
        try:
            providers = dispatcher.dispatch("processing.provider", {"action": "list"})
            self.assertIn("native", {item["id"] for item in providers["providers"]})
            assets = dispatcher.dispatch(
                "processing.assets",
                {"kind": "algorithms", "query": "buffer", "limit": 20},
            )
            self.assertIn("native:buffer", {item["id"] for item in assets["items"]})
            context = dispatcher.dispatch("processing.context", {})
            self.assertIn("temporary_folder", context)
            row = {
                "INPUT": source.id(),
                "DISTANCE": 1,
                "SEGMENTS": 5,
                "DISSOLVE": False,
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
            }
            batch = dispatcher.dispatch(
                "processing.batch",
                {"algorithm": "native:buffer", "rows": [row, {**row, "DISTANCE": 2}]},
            )
            self.assertEqual(batch["started"], 2)
            operation_ids = [item["operation"]["id"] for item in batch["items"]]
            deadline = time.monotonic() + 25
            statuses = {}
            while time.monotonic() < deadline:
                QCoreApplication.processEvents(QEventLoop.AllEvents, 50)
                statuses = {
                    operation_id: dispatcher.operation_control(operation_id)
                    for operation_id in operation_ids
                }
                if all(
                    item["status"] not in {"queued", "running", "cancelling"}
                    for item in statuses.values()
                ):
                    break
                time.sleep(0.02)
            self.assertEqual(
                {item["status"] for item in statuses.values()}, {"succeeded"}
            )
            history = dispatcher.dispatch("processing.history", {"action": "list"})
            self.assertTrue(set(operation_ids) <= {item["id"] for item in history["operations"]})
        finally:
            QgsProject.instance().removeMapLayer(source.id())

    def test_14_advanced_cartography_layout_items_and_atlas(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        layer = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=kind:string", "cartography-tools", "memory"
        )
        QgsProject.instance().addMapLayer(layer)
        dispatcher.vector_edit(
            layer.id(),
            "add",
            features=[
                {
                    "attributes": {"kind": "city"},
                    "geometry_wkt": "POINT(2.35 48.86)",
                }
            ],
        )
        dispatcher.vector_edit(layer.id(), "commit")
        layout_name = "QGIS MCP advanced cartography"
        try:
            renderer = dispatcher.dispatch(
                "cartography.renderer",
                {
                    "layer": layer.id(),
                    "action": "rule_based",
                    "rules": [
                        {
                            "expression": "kind = 'city'",
                            "label": "Cities",
                            "color": "#e53935",
                            "size": 4,
                        },
                        {"else": True, "label": "Other", "color": "#607d8b"},
                    ],
                },
            )
            self.assertIn("rule", renderer["renderer"]["type"].casefold())
            symbols = dispatcher.dispatch(
                "cartography.symbol",
                {"layer": layer.id(), "action": "set", "opacity": 0.8},
            )
            self.assertTrue(symbols["symbols"])
            labels = dispatcher.dispatch(
                "cartography.labeling",
                {
                    "layer": layer.id(),
                    "action": "set",
                    "field": "kind",
                    "font_size": 11,
                    "buffer_size": 1,
                    "placement": "around_point",
                },
            )
            self.assertTrue(labels["enabled"])
            library = dispatcher.dispatch(
                "style.library", {"action": "list", "kind": "symbols", "limit": 10}
            )
            self.assertIn("names", library)

            dispatcher.dispatch(
                "layout.execute", {"action": "create", "name": layout_name}
            )
            map_item = dispatcher.dispatch(
                "layout.item",
                {
                    "layout": layout_name,
                    "action": "add",
                    "item_type": "map",
                    "item_id": "mcp-map",
                    "x": 10,
                    "y": 30,
                    "width": 160,
                    "height": 120,
                    "extent": [1.5, 48.4, 3.2, 49.3],
                    "layers": [layer.id()],
                },
            )
            self.assertEqual(map_item["id"], "mcp-map")
            label_item = dispatcher.dispatch(
                "layout.item",
                {
                    "layout": layout_name,
                    "action": "add",
                    "item_type": "label",
                    "item_id": "mcp-title",
                    "text": "Autonomous map",
                    "x": 10,
                    "y": 10,
                },
            )
            self.assertEqual(label_item["text"], "Autonomous map")
            atlas = dispatcher.dispatch(
                "layout.atlas",
                {
                    "layout": layout_name,
                    "action": "configure",
                    "coverage_layer": layer.id(),
                    "filename_expression": "'page_' || @atlas_featurenumber",
                    "page_name_expression": "kind",
                },
            )
            self.assertTrue(atlas["enabled"])
            validation = dispatcher.dispatch(
                "layout.validate", {"layout": layout_name}
            )
            self.assertGreater(validation["item_count"], 0)
        finally:
            layout = QgsProject.instance().layoutManager().layoutByName(layout_name)
            if layout:
                QgsProject.instance().layoutManager().removeLayout(layout)
            QgsProject.instance().removeMapLayer(layer.id())

    def test_15_specialized_layer_capabilities_temporal_and_elevation(self):
        dispatcher = qgis.utils.plugins["qgis_agent_mcp"].dispatcher
        layer = QgsVectorLayer("Point?crs=EPSG:4326", "specialized-data", "memory")
        QgsProject.instance().addMapLayer(layer)
        try:
            properties = dispatcher.dispatch(
                "layer.properties",
                {
                    "layer": layer.id(),
                    "action": "set",
                    "opacity": 0.7,
                    "scale_based_visibility": True,
                    "minimum_scale": 1000,
                    "maximum_scale": 100000,
                },
            )
            self.assertAlmostEqual(properties["opacity"], 0.7)
            self.assertTrue(properties["scale_based_visibility"])
            capabilities = dispatcher.dispatch(
                "layer.capabilities",
                {"layer": layer.id(), "query": "feature", "limit": 100},
            )
            self.assertTrue(any("feature" in item.casefold() for item in capabilities["methods"]))
            temporal = dispatcher.dispatch(
                "layer.temporal",
                {"layer": layer.id(), "action": "set_active", "enabled": True},
            )
            self.assertTrue(temporal["active"])
            elevation = dispatcher.dispatch(
                "layer.elevation", {"layer": layer.id(), "action": "inspect"}
            )
            self.assertEqual(elevation["layer_id"], layer.id())
        finally:
            QgsProject.instance().removeMapLayer(layer.id())


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
