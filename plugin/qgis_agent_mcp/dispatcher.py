from __future__ import annotations

import json
import os
import time
import traceback
from urllib.parse import unquote, urlparse

from qgis.core import (
    Qgis,
    QgsExpression,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsLayoutExporter,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QBuffer, QCoreApplication, QEventLoop, QIODevice, Qt
from qgis.PyQt.QtWidgets import (
    QAbstractButton,
    QAction,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QMainWindow,
    QSpinBox,
    QWidget,
)

from .advanced_cartography import AdvancedCartographyTools
from .authoring_tools import AuthoringTools
from .capabilities import CapabilityIndex, ObjectRegistry
from .cartography import CartographyManager, LayoutManager, ProjectLayerManager
from .connectors import FireMapManager
from .data_sources import DataAcquisitionManager
from .ecosystem_tools import EcosystemTools
from .operations import OperationManager
from .processing_database_tools import ProcessingDatabaseTools
from .project_tools import ProjectTools
from .qa_tools import QaTools
from .reliability import IdempotencyConflict, MutationGuard
from .revisions import (
    CAPABILITIES_URI,
    LAYER_TREE_URI,
    LOGS_URI,
    PROJECT_URI,
    SESSION_URI,
    layer_uri,
    operation_uri,
)
from .runtime_tools import RuntimeTools
from .safety import CheckpointManager, ProjectVerifier
from .serialize import (
    feature_summary,
    field_schema,
    json_safe,
    layer_summary,
    renderer_summary,
)
from .specialized_data_tools import SpecializedDataTools
from .state import StateTracker
from .store import ArtifactStore, EventLog, HandleStore
from .vector_raster_tools import VectorRasterTools
from .workflows import WorkflowManager

MAX_INLINE_RESULT_BYTES = 1024 * 1024
MUTATION_METHODS = {
    "project.action",
    "selection.set",
    "vector.edit",
    "capabilities.invoke",
    "processing.start",
    "operation.control",
    "ui.invoke",
    "batch.execute",
    "artifact.release",
    "data.fetch",
    "data.service",
    "data.refresh",
    "layer.manage",
    "cartography.style",
    "cartography.labels",
    "layout.execute",
    "checkpoint.execute",
    "workflow.execute",
    "visual.review",
    "connector.fire_map",
    "runtime.tasks",
    "runtime.render",
    "runtime.transaction",
    "runtime.undo",
    "project.manage",
    "project.properties",
    "project.repair",
    "layer.source",
    "canvas.control",
    "bookmark.manage",
    "map_theme.manage",
    "crs.control",
    "metadata.manage",
    "vector.schema",
    "vector.geometry",
    "vector.index",
    "vector.join",
    "project.relation",
    "project.snapping",
    "selection.advanced",
    "processing.provider",
    "processing.batch",
    "processing.history",
    "database.control",
    "connection.manage",
    "cartography.renderer",
    "cartography.symbol",
    "style.library",
    "cartography.labeling",
    "layout.item",
    "layout.atlas",
    "layer.properties",
    "raster.style",
    "mesh.control",
    "point_cloud.control",
    "vector_tile.control",
    "tiled_scene.control",
    "layer.temporal",
    "layer.elevation",
    "ecosystem.plugins",
    "ecosystem.settings",
    "ecosystem.shortcuts",
    "ecosystem.gps",
    "ecosystem.3d",
    "ecosystem.server",
    "ecosystem.offline",
    "authoring.forms",
    "authoring.diagrams",
    "authoring.annotations",
    "authoring.geometry_quality",
    "authoring.vector_export",
}


class DispatchError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class Dispatcher:
    def __init__(self, iface):
        self.iface = iface
        self.log = EventLog()
        self.handles = HandleStore()
        self.artifacts = ArtifactStore()
        self.mutations = MutationGuard(
            max_entries=128,
            path=os.environ.get(
                "QGIS_MCP_IDEMPOTENCY_FILE",
                os.path.join(os.path.expanduser("~"), ".qgis-mcp", "idempotency.json"),
            ),
        )
        self.state = StateTracker(iface, self.log)
        self.data = DataAcquisitionManager(self.state, self.log)
        self.layer_manager = ProjectLayerManager(iface, self.state)
        self.cartography = CartographyManager(self.state)
        self.layouts = LayoutManager(self.state, iface=iface)
        self.checkpoints = CheckpointManager(self.state)
        self.verifier = ProjectVerifier()
        self.fire_maps = FireMapManager(
            iface,
            self.data,
            self.layer_manager,
            self.cartography,
            self.layouts,
            self.verifier,
            self.state,
        )
        self.objects = ObjectRegistry(iface)
        self.capabilities = CapabilityIndex(iface, self.objects)
        self.operations = OperationManager(self.log, self.state, self.artifacts)
        self._methods = {
            "session.snapshot": self.session_snapshot,
            "project.inspect": self.project_inspect,
            "project.action": self.project_action,
            "layer.inspect": self.layer_inspect,
            "feature.query": self.feature_query,
            "selection.set": self.selection_set,
            "vector.edit": self.vector_edit,
            "capabilities.search": self.capabilities_search,
            "capabilities.describe": self.capabilities_describe,
            "capabilities.invoke": self.capabilities_invoke,
            "processing.start": self.processing_start,
            "operation.control": self.operation_control,
            "ui.search": self.ui_search,
            "ui.invoke": self.ui_invoke,
            "ui.screenshot": self.ui_screenshot,
            "logs.read": self.logs_read,
            "handle.read": self.handle_read,
            "artifact.read": self.artifact_read,
            "artifact.list": self.artifact_list,
            "artifact.release": self.artifact_release,
            "data.fetch": self.data_fetch,
            "data.service": self.data_service,
            "data.refresh": self.data_refresh,
            "data.catalog": self.data_catalog,
            "data.provenance": self.data_provenance,
            "layer.manage": self.layer_manage,
            "cartography.style": self.cartography_style,
            "cartography.labels": self.cartography_labels,
            "layout.execute": self.layout_execute,
            "checkpoint.execute": self.checkpoint_execute,
            "project.verify": self.project_verify,
            "workflow.execute": self.workflow_execute,
            "visual.review": self.visual_review,
            "connector.fire_map": self.fire_map,
            "connector.catalog": self.connector_catalog,
            "batch.execute": self.batch_execute,
            "runtime.control": self.runtime_control,
            "runtime.tasks": self.runtime_tasks,
            "runtime.events": self.runtime_events,
            "runtime.render": self.runtime_render,
            "runtime.transaction": self.runtime_transaction,
            "runtime.undo": self.runtime_undo,
            "runtime.preflight": self.runtime_preflight,
            "runtime.diff": self.runtime_diff,
            "runtime.diagnostics": self.runtime_diagnostics,
            "runtime.permissions": self.runtime_permissions,
            "runtime.auth": self.runtime_auth,
            "project.manage": self.project_manage,
            "project.properties": self.project_properties,
            "project.repair": self.project_repair,
            "layer.source": self.layer_source,
            "canvas.control": self.canvas_control,
            "map.identify": self.map_identify,
            "map.measure": self.map_measure,
            "bookmark.manage": self.bookmark_manage,
            "map_theme.manage": self.map_theme_manage,
            "crs.control": self.crs_control,
            "expression.control": self.expression_control,
            "metadata.manage": self.metadata_manage,
            "connection.inspect": self.connection_inspect,
            "vector.schema": self.vector_schema,
            "vector.statistics": self.vector_statistics,
            "vector.geometry": self.geometry_edit,
            "vector.index": self.vector_indexes,
            "vector.join": self.vector_joins,
            "project.relation": self.project_relations,
            "project.snapping": self.project_snapping,
            "selection.advanced": self.advanced_selection,
            "raster.inspect": self.raster_inspect,
            "processing.provider": self.processing_provider,
            "processing.batch": self.processing_batch,
            "processing.history": self.processing_history,
            "processing.assets": self.processing_assets,
            "processing.context": self.processing_context,
            "database.control": self.database_control,
            "connection.manage": self.connection_manage,
            "cartography.renderer": self.advanced_renderer,
            "cartography.symbol": self.advanced_symbol,
            "style.library": self.style_library,
            "cartography.labeling": self.advanced_labeling,
            "layout.item": self.layout_item,
            "layout.atlas": self.layout_atlas,
            "layout.validate": self.layout_validate,
            "layer.properties": self.specialized_layer_properties,
            "layer.capabilities": self.specialized_layer_capabilities,
            "raster.style": self.specialized_raster_style,
            "mesh.control": self.specialized_mesh,
            "point_cloud.control": self.specialized_point_cloud,
            "vector_tile.control": self.specialized_vector_tiles,
            "tiled_scene.control": self.specialized_tiled_scene,
            "layer.temporal": self.specialized_temporal,
            "layer.elevation": self.specialized_elevation,
            "ecosystem.plugins": self.ecosystem_plugins,
            "ecosystem.settings": self.ecosystem_settings,
            "ecosystem.shortcuts": self.ecosystem_shortcuts,
            "ecosystem.gps": self.ecosystem_gps,
            "ecosystem.3d": self.ecosystem_3d,
            "ecosystem.server": self.ecosystem_server,
            "ecosystem.offline": self.ecosystem_offline,
            "authoring.forms": self.authoring_forms,
            "authoring.diagrams": self.authoring_diagrams,
            "authoring.annotations": self.authoring_annotations,
            "authoring.geometry_quality": self.authoring_geometry_quality,
            "authoring.vector_export": self.authoring_vector_export,
            "qa.compatibility": self.qa_compatibility,
            "qa.project_audit": self.qa_project_audit,
            "qa.benchmark": self.qa_benchmark,
            "qa.self_test": self.qa_self_test,
            "resources.list": self.resources_list,
            "resources.read": self.resources_read,
        }
        self.runtime_tools = RuntimeTools(
            iface,
            self.state,
            self.log,
            self.operations,
            self._layer,
            lambda: self._methods,
            MUTATION_METHODS,
            self.data,
            self.layouts,
        )
        self.project_tools = ProjectTools(iface, self.state, self._layer)
        self.vector_raster_tools = VectorRasterTools(self.state, self._layer)
        self.processing_database_tools = ProcessingDatabaseTools(
            self.state, self.operations
        )
        self.advanced_cartography = AdvancedCartographyTools(
            self.state, self._layer
        )
        self.specialized_data_tools = SpecializedDataTools(self.state, self._layer)
        self.ecosystem_tools = EcosystemTools(iface, self.state)
        self.authoring_tools = AuthoringTools(self.state, self._layer)
        self.qa_tools = QaTools(iface, self.state, self.verifier, lambda: self._methods)
        self.workflows = WorkflowManager(
            self.dispatch,
            self.checkpoints,
            self.state,
            self.log,
            mutation_methods=MUTATION_METHODS,
        )
        QgsApplicationMessageLog.connect(self.log)

    def close(self):
        self.workflows.close()
        self.state.close()
        QgsApplicationMessageLog.disconnect(self.log)

    def dispatch(self, method, params):
        handler = self._methods.get(method)
        if handler is None:
            raise DispatchError(-32601, "Unknown QGIS bridge method: {}".format(method))
        if not isinstance(params, dict):
            raise DispatchError(-32602, "Parameters must be an object")
        params = dict(params)
        idempotency_key = params.pop("idempotency_key", None)
        if_revision = params.pop("if_revision", None)
        resource_preconditions = params.pop("if_resource_revisions", None)
        dry_run = bool(params.pop("dry_run", False))
        if method in MUTATION_METHODS:
            if dry_run:
                self._check_preconditions(if_revision, resource_preconditions)
                return {
                    "dry_run": True,
                    "method": method,
                    "parameter_names": sorted(params),
                    "preconditions_valid": True,
                    "current_revision": self.state.revision,
                }
            if idempotency_key:
                try:
                    found, cached = self.mutations.lookup(
                        str(idempotency_key), method, params
                    )
                except IdempotencyConflict as exc:
                    raise DispatchError(-32041, str(exc)) from exc
                if found:
                    return cached
            self._check_preconditions(if_revision, resource_preconditions)
        try:
            result = handler(**params)
            if method not in {"handle.read", "ui.screenshot"}:
                encoded_size = len(
                    json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
                )
                if encoded_size > MAX_INLINE_RESULT_BYTES:
                    descriptor = self.handles.put(
                        result,
                        kind="large_result",
                        metadata={"method": method, "estimated_bytes": encoded_size},
                    )
                    result = {
                        **descriptor,
                        "truncated": True,
                        "message": (
                            "Result retained inside QGIS because it exceeds the inline "
                            "payload limit; page it with qgis_handle_read."
                        ),
                    }
            if method in MUTATION_METHODS and idempotency_key:
                self.mutations.remember(str(idempotency_key), method, params, result)
            return result
        except DispatchError:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise DispatchError(
                -32602,
                str(exc).strip("'"),
                {"method": method, "exception": type(exc).__name__},
            ) from exc
        except Exception as exc:
            stack = traceback.format_exc()
            self.log.add("error", str(exc), "error", {"method": method, "traceback": stack})
            raise DispatchError(
                -32010,
                "QGIS operation failed: {}".format(exc),
                {"method": method, "exception": type(exc).__name__, "traceback": stack},
            ) from exc

    def _check_preconditions(self, revision, resources):
        if revision is not None and int(revision) != self.state.revision:
            raise DispatchError(
                -32040,
                "Global revision precondition failed",
                {"expected": int(revision), "current": self.state.revision},
            )
        if resources is None:
            return
        if not isinstance(resources, dict):
            raise DispatchError(-32602, "if_resource_revisions must be an object")
        conflicts = {
            uri: {"expected": int(expected), "current": self.state.resource_revision(uri)}
            for uri, expected in resources.items()
            if int(expected) != self.state.resource_revision(uri)
        }
        if conflicts:
            raise DispatchError(
                -32040, "Resource revision precondition failed", {"conflicts": conflicts}
            )

    def session_snapshot(self, detail="standard", since_revision=None):
        if detail not in {"summary", "standard", "full"}:
            raise ValueError("detail must be summary, standard, or full")
        result = self.state.snapshot(detail, since_revision)
        if detail != "summary":
            result["capabilities"] = self.capabilities.summary()
            result["operations"] = self.operations.list_public()
            result["python_execution_enabled"] = False
            result["open_windows"] = [
                self.objects.summarize(runtime_id, obj)
                for runtime_id, obj in self.objects.refresh().items()
                if isinstance(obj, QMainWindow) and obj.isVisible()
            ]
        return result

    def project_inspect(self, section="project"):
        project = QgsProject.instance()
        if section == "project":
            return {
                **self.state.snapshot("summary")["project"],
                "home_path": project.homePath(),
                "absolute_path": project.absolutePath(),
                "ellipsoid": project.ellipsoid(),
                "distance_units": int(project.distanceUnits()),
                "area_units": int(project.areaUnits()),
                "metadata": json_safe(project.metadata()),
                "custom_variables": json_safe(project.customVariables()),
            }
        if section == "layer_tree":
            return _tree_node(project.layerTreeRoot())
        if section == "variables":
            return json_safe(project.customVariables())
        if section == "relations":
            return [
                {
                    "id": relation.id(),
                    "name": relation.name(),
                    "valid": relation.isValid(),
                    "referenced_layer": relation.referencedLayerId(),
                    "referencing_layer": relation.referencingLayerId(),
                    "field_pairs": json_safe(relation.fieldPairs()),
                }
                for relation in project.relationManager().relations().values()
            ]
        if section == "layouts":
            return [
                {
                    "name": layout.name(),
                    "type": type(layout).__name__,
                    "item_count": len(layout.items()),
                }
                for layout in project.layoutManager().layouts()
            ]
        raise ValueError("Unknown project section")

    def project_action(
        self,
        action,
        layer=None,
        source=None,
        name=None,
        provider=None,
        path=None,
    ):
        project = QgsProject.instance()
        result = None
        if action == "save":
            success = project.write(path) if path else project.write()
            if not success:
                raise RuntimeError("QGIS could not save the project")
            result = {"file": project.fileName(), "saved": True}
        elif action in {"add_vector", "add_raster"}:
            if not source:
                raise ValueError("source is required")
            layer_name = name or os.path.basename(source) or "Layer"
            if action == "add_vector":
                created = QgsVectorLayer(source, layer_name, provider or "ogr")
            else:
                created = QgsRasterLayer(source, layer_name, provider or "gdal")
            if not created.isValid():
                raise ValueError("QGIS could not create a valid layer from the source")
            project.addMapLayer(created)
            result = layer_summary(created)
        elif action == "remove_layer":
            target = self._layer(layer)
            result = layer_summary(target)
            project.removeMapLayer(target.id())
        elif action == "set_active_layer":
            target = self._layer(layer)
            self.iface.setActiveLayer(target)
            result = layer_summary(target)
        elif action == "zoom_layer":
            target = self._layer(layer)
            self.iface.mapCanvas().setExtent(target.extent())
            self.iface.mapCanvas().refresh()
            result = layer_summary(target)
        elif action == "refresh":
            if layer:
                target = self._layer(layer)
                target.reload()
                target.triggerRepaint()
                result = layer_summary(target)
            else:
                self.iface.mapCanvas().refresh()
                result = {"canvas_refreshed": True}
        elif action == "load_style":
            target = self._layer(layer)
            if not path:
                raise ValueError("path is required")
            message, success = target.loadNamedStyle(path)
            if not success:
                raise ValueError("Could not load style: {}".format(message))
            target.triggerRepaint()
            result = {"layer_id": target.id(), "style_path": path, "message": message}
        else:
            raise ValueError("Unknown project action")
        self.state.touch("project.action", {"action": action, "layer": layer})
        return result

    def layer_inspect(self, layer, include=None, sample_limit=5):
        target = self._layer(layer)
        wanted = set(include or ("metadata", "schema", "style", "selection"))
        result = {"summary": layer_summary(target)}
        if "metadata" in wanted:
            result["metadata"] = {
                "abstract": target.abstract(),
                "title": target.title(),
                "attribution": target.attribution(),
                "keywords": json_safe(target.keywordList()),
                "metadata": json_safe(target.metadata()),
            }
        if "schema" in wanted and isinstance(target, QgsVectorLayer):
            result["schema"] = [field_schema(field) for field in target.fields()]
            result["primary_key_fields"] = target.primaryKeyAttributes()
        if "style" in wanted:
            result["style"] = renderer_summary(target)
        if "selection" in wanted and isinstance(target, QgsVectorLayer):
            ids = list(target.selectedFeatureIds())
            result["selection"] = {
                "count": len(ids),
                "feature_ids": ids[:1000],
                "truncated": len(ids) > 1000,
            }
        if "sample" in wanted and isinstance(target, QgsVectorLayer) and sample_limit:
            result["sample"] = self.feature_query(
                target.id(), limit=min(int(sample_limit), 100)
            )
        return result

    def feature_query(
        self,
        layer,
        expression=None,
        fields=None,
        selected_only=False,
        include_geometry=False,
        limit=100,
        offset=0,
    ):
        target = self._vector_layer(layer)
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        field_names = list(fields) if fields else [field.name() for field in target.fields()]
        unknown = [name for name in field_names if target.fields().indexOf(name) < 0]
        if unknown:
            raise ValueError("Unknown fields: {}".format(", ".join(unknown)))
        request = QgsFeatureRequest()
        if expression:
            parsed = QgsExpression(expression)
            if parsed.hasParserError():
                raise ValueError("Invalid QGIS expression: {}".format(parsed.parserErrorString()))
            request.setFilterExpression(expression)
        if selected_only:
            request.setFilterFids(target.selectedFeatureIds())
        request.setSubsetOfAttributes(field_names, target.fields())
        if not include_geometry:
            request.setFlags(QgsFeatureRequest.Flag.NoGeometry)
        if hasattr(request, "setOffset"):
            request.setLimit(limit + 1)
            request.setOffset(offset)
            iterator = target.getFeatures(request)
        else:
            request.setLimit(offset + limit + 1)
            iterator = target.getFeatures(request)
            for _ in range(offset):
                next(iterator, None)
        selected_fields = [target.fields().field(name) for name in field_names]
        items = []
        for feature in iterator:
            items.append(feature_summary(feature, selected_fields, include_geometry))
            if len(items) > limit:
                break
        has_more = len(items) > limit
        return {
            "layer_id": target.id(),
            "offset": offset,
            "limit": limit,
            "items": items[:limit],
            "has_more": has_more,
            "feature_count": target.featureCount(),
        }

    def selection_set(
        self, layer, mode="replace", feature_ids=None, expression=None
    ):
        target = self._vector_layer(layer)
        if mode == "clear":
            target.removeSelection()
        else:
            behavior = {
                "replace": QgsVectorLayer.SelectBehavior.SetSelection,
                "add": QgsVectorLayer.SelectBehavior.AddToSelection,
                "remove": QgsVectorLayer.SelectBehavior.RemoveFromSelection,
            }.get(mode)
            if behavior is None:
                raise ValueError("Unknown selection mode")
            if expression is not None:
                parsed = QgsExpression(expression)
                if parsed.hasParserError():
                    raise ValueError(
                        "Invalid QGIS expression: {}".format(parsed.parserErrorString())
                    )
                target.selectByExpression(expression, behavior)
            elif feature_ids is not None:
                target.selectByIds([int(item) for item in feature_ids], behavior)
            else:
                raise ValueError("feature_ids or expression is required")
        self.state.touch("selection.set", {"layer_id": target.id(), "mode": mode})
        return {
            "layer_id": target.id(),
            "selected_count": target.selectedFeatureCount(),
            "feature_ids": list(target.selectedFeatureIds())[:1000],
        }

    def vector_edit(
        self,
        layer,
        action,
        features=None,
        feature_ids=None,
        auto_start=True,
    ):
        target = self._vector_layer(layer)
        if action == "start":
            if not target.isEditable() and not target.startEditing():
                raise RuntimeError("QGIS could not start an edit session")
        elif action in {"add", "update", "delete"}:
            if not target.isEditable():
                if not auto_start or not target.startEditing():
                    raise RuntimeError("Layer is not editable")
            target.beginEditCommand("QGIS MCP {}".format(action))
            try:
                if action == "add":
                    added = []
                    for item in (features or [])[:1000]:
                        feature = QgsFeature(target.fields())
                        for field_name, value in (item.get("attributes") or {}).items():
                            if target.fields().indexOf(field_name) < 0:
                                raise ValueError("Unknown field: {}".format(field_name))
                            feature[field_name] = value
                        if item.get("geometry_wkt"):
                            geometry = QgsGeometry.fromWkt(item["geometry_wkt"])
                            if geometry.isNull():
                                raise ValueError("Invalid geometry WKT")
                            feature.setGeometry(geometry)
                        if not target.addFeature(feature):
                            raise RuntimeError("Provider rejected a new feature")
                        added.append(feature.id())
                    result = {"added_feature_ids": added}
                elif action == "update":
                    updated = []
                    for item in (features or [])[:1000]:
                        if "id" not in item:
                            raise ValueError("Each updated feature requires an id")
                        feature_id = int(item["id"])
                        changes = {}
                        for field_name, value in (item.get("attributes") or {}).items():
                            field_index = target.fields().indexOf(field_name)
                            if field_index < 0:
                                raise ValueError("Unknown field: {}".format(field_name))
                            changes[field_index] = value
                        if changes and not target.changeAttributeValues(feature_id, changes):
                            raise RuntimeError(
                                "Could not update feature {}".format(feature_id)
                            )
                        if item.get("geometry_wkt"):
                            geometry = QgsGeometry.fromWkt(item["geometry_wkt"])
                            if geometry.isNull() or not target.changeGeometry(
                                feature_id, geometry
                            ):
                                raise RuntimeError(
                                    "Could not update geometry for feature {}".format(
                                        feature_id
                                    )
                                )
                        updated.append(feature_id)
                    result = {"updated_feature_ids": updated}
                else:
                    ids = [int(item) for item in (feature_ids or [])[:1000]]
                    if not target.deleteFeatures(ids):
                        raise RuntimeError("Could not delete requested features")
                    result = {"deleted_feature_ids": ids}
                target.endEditCommand()
            except Exception:
                target.destroyEditCommand()
                raise
        elif action == "commit":
            if not target.isEditable():
                raise ValueError("Layer has no active edit session")
            if not target.commitChanges():
                errors = target.commitErrors()
                raise RuntimeError("Commit failed: {}".format("; ".join(errors)))
            result = {"committed": True}
        elif action == "rollback":
            if not target.isEditable():
                raise ValueError("Layer has no active edit session")
            if not target.rollBack():
                raise RuntimeError("Rollback failed")
            result = {"rolled_back": True}
        else:
            raise ValueError("Unknown vector edit action")
        self.state.touch(
            "vector.edit",
            {"layer_id": target.id(), "action": action},
        )
        return {
            "layer_id": target.id(),
            "editable": target.isEditable(),
            "modified": target.isModified(),
            **(result or {}),
        }

    def capabilities_search(self, query="", kinds=None, limit=30):
        return self.capabilities.search(query, kinds, int(limit))

    def capabilities_describe(self, kind, id):
        return self.capabilities.describe(kind, id)

    def capabilities_invoke(
        self, kind, target, member, args=None, kwargs=None
    ):
        result = self.capabilities.invoke(kind, target, member, args, kwargs)
        self.state.touch(
            "capability.invoked",
            {"kind": kind, "target": target, "member": member},
        )
        return {"result": json_safe(result)}

    def processing_start(
        self, algorithm, parameters, retain_outputs=True, add_to_project=False
    ):
        return self.operations.start_processing(
            algorithm, parameters, retain_outputs, add_to_project
        )

    def operation_control(self, operation_id, action="status"):
        return self.operations.control(operation_id, action)

    def ui_search(self, query="", types=None, visible_only=True, limit=50):
        needle = query.casefold().strip()
        wanted = {item.casefold() for item in types} if types else None
        values = []
        for runtime_id, obj in self.objects.refresh().items():
            item = self.objects.summarize(runtime_id, obj)
            haystack = " ".join(str(value) for value in item.values()).casefold()
            if needle and needle not in haystack:
                continue
            if wanted and item["class"].casefold() not in wanted and item["kind"] not in wanted:
                continue
            if visible_only and item.get("visible") is False and item["kind"] != "action":
                continue
            values.append(item)
        return {"results": values[: int(limit)], "truncated": len(values) > int(limit)}

    def ui_invoke(self, target, action, value=None):
        obj = self.objects.get(target)
        if action == "trigger" and isinstance(obj, QAction):
            obj.trigger()
        elif action == "click" and isinstance(obj, QAbstractButton):
            obj.click()
        elif action == "set_text" and isinstance(obj, (QLineEdit, QComboBox)):
            obj.setText(str(value)) if isinstance(obj, QLineEdit) else obj.setCurrentText(str(value))
        elif action == "set_value" and isinstance(obj, (QSpinBox, QDoubleSpinBox)):
            obj.setValue(value)
        elif action == "set_checked" and isinstance(obj, (QAction, QAbstractButton)):
            obj.setChecked(bool(value))
        elif action == "show" and isinstance(obj, QWidget):
            obj.show()
            obj.raise_()
            obj.activateWindow()
        elif action == "close" and isinstance(obj, QWidget):
            obj.close()
        else:
            raise ValueError(
                "Action {} is not supported by {}".format(action, type(obj).__name__)
            )
        self.state.touch("ui.invoked", {"target": target, "action": action})
        return self.objects.summarize(target, obj)

    def ui_screenshot(self, target="canvas", max_width=1600, as_artifact=False):
        if target == "canvas":
            widget = self.iface.mapCanvas()
        elif target in {"main", "window"}:
            widget = self.iface.mainWindow()
        else:
            widget = self.objects.get(target)
            if not isinstance(widget, QWidget):
                raise ValueError("Screenshot target must be a QWidget")
        pixmap = widget.grab()
        if pixmap.width() > int(max_width):
            pixmap = pixmap.scaledToWidth(
                int(max_width), Qt.TransformationMode.SmoothTransformation
            )
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            raise RuntimeError("Could not encode screenshot")
        import base64

        raw = bytes(buffer.data())
        result = {
            "mime_type": "image/png",
            "width": pixmap.width(),
            "height": pixmap.height(),
            "target": target,
            "revision": self.state.revision,
        }
        if as_artifact:
            result["artifact"] = self.artifacts.put_bytes(
                raw,
                "image/png",
                "qgis-{}.png".format(target),
                {"target": target, "revision": self.state.revision},
            )
        else:
            result["data"] = base64.b64encode(raw).decode("ascii")
        return result

    def logs_read(self, after=0, level=None, limit=100):
        return self.log.read(int(after), level, int(limit))

    def handle_read(self, handle, offset=0, limit=100):
        return self.handles.read(handle, int(offset), int(limit))

    def artifact_read(self, artifact_id, offset=0, length=None):
        return self.artifacts.read(artifact_id, int(offset), length)

    def artifact_list(self):
        return {"artifacts": self.artifacts.list()}

    def artifact_release(self, artifact_id):
        return {"artifact_id": artifact_id, "released": self.artifacts.release(artifact_id)}

    def data_fetch(
        self,
        url,
        name=None,
        authcfg=None,
        cache_mode="reuse",
        max_age_seconds=3600,
        max_bytes=64 * 1024 * 1024,
        expected_sha256=None,
        add_to_project=True,
        provider=None,
        x_field=None,
        y_field=None,
        delimiter=",",
        crs="EPSG:4326",
    ):
        return self.data.fetch(
            url=url,
            name=name,
            authcfg=authcfg,
            cache_mode=cache_mode,
            max_age_seconds=max_age_seconds,
            max_bytes=max_bytes,
            expected_sha256=expected_sha256,
            add_to_project=add_to_project,
            provider=provider,
            x_field=x_field,
            y_field=y_field,
            delimiter=delimiter,
            crs=crs,
        )

    def data_service(
        self,
        kind,
        url,
        name,
        authcfg=None,
        layer=None,
        crs=None,
        format="image/png",
        zmin=0,
        zmax=20,
    ):
        return self.data.add_service(
            kind=kind,
            url=url,
            name=name,
            authcfg=authcfg,
            layer=layer,
            crs=crs,
            format=format,
            zmin=zmin,
            zmax=zmax,
        )

    def data_refresh(self, layer):
        return self.data.refresh(self._layer(layer))

    def data_catalog(self):
        return self.data.catalog()

    def data_provenance(self, layer):
        target = self._layer(layer)
        return {"layer_id": target.id(), "provenance": self.data.provenance(target)}

    def layer_manage(self, **params):
        return self.layer_manager.execute(**params)

    def cartography_style(self, layer, **params):
        return self.cartography.style(self._layer(layer), **params)

    def cartography_labels(self, layer, **params):
        return self.cartography.labels(self._layer(layer), **params)

    def layout_execute(self, **params):
        return self.layouts.execute(**params)

    def checkpoint_execute(self, **params):
        return self.checkpoints.execute(**params)

    def project_verify(self, **params):
        return self.verifier.verify(**params)

    def workflow_execute(self, **params):
        return self.workflows.execute(**params)

    def visual_review(
        self,
        action="capture",
        target="canvas",
        layout=None,
        page=0,
        max_width=1600,
        wait_ms=1500,
        geometry_sample=100,
        require_layout=False,
        require_saved=False,
        findings=None,
        passed=None,
        correction_calls=None,
        atomic=True,
    ):
        if action == "record":
            normalized = self._visual_findings(findings)
            if passed is None:
                passed = not any(item["severity"] == "error" for item in normalized)
            result = {
                "passed": bool(passed),
                "findings": normalized,
                "revision": self.state.revision,
                "recorded_at": time.time(),
            }
            self.state.touch("visual_review.recorded", result)
            return result
        corrections = None
        if action == "apply":
            if not isinstance(correction_calls, list) or not correction_calls:
                raise ValueError("correction_calls are required for action=apply")
            if len(correction_calls) > 25:
                raise ValueError("At most 25 correction calls are allowed")
            forbidden = {"batch.execute", "visual.review", "workflow.execute"}
            requested_methods = {str(call.get("method")) for call in correction_calls}
            if requested_methods & forbidden:
                raise ValueError("Nested orchestration corrections are not allowed")
            preflight = self.runtime_tools.preflight(correction_calls)
            if not preflight["valid"]:
                raise ValueError("Visual correction preflight failed: {}".format(preflight["errors"]))
            corrections = {
                "preflight": preflight,
                "batch": self.batch_execute(
                    correction_calls,
                    continue_on_error=False,
                    atomic=bool(atomic),
                ),
                "input_findings": self._visual_findings(findings),
            }
            if corrections["batch"]["rolled_back"]:
                raise RuntimeError("Visual corrections failed and were rolled back")
        elif action != "capture":
            raise ValueError("Unknown visual review action")

        canvas = self.iface.mapCanvas()
        canvas.refresh()
        deadline = time.monotonic() + max(0, min(int(wait_ms), 5000)) / 1000.0
        while canvas.isDrawing() and time.monotonic() < deadline:
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25
            )
            time.sleep(0.01)
        capture = (
            self._layout_screenshot(layout, page=page, max_width=max_width)
            if layout
            else self.ui_screenshot(target=target, max_width=max_width)
        )
        audit = self.qa_tools.project_audit(
            geometry_sample=geometry_sample,
            require_layout=require_layout,
            require_saved=require_saved,
            include_server=False,
            include_metadata=True,
        )
        layout_check = self.advanced_cartography.layout_validate(layout) if layout else None
        automated = list(audit["issues"])
        if not canvas.layers():
            automated.append(
                {
                    "severity": "error",
                    "code": "canvas.no_layers",
                    "message": "The map canvas has no visible layers.",
                }
            )
        if not canvas.renderFlag():
            automated.append(
                {
                    "severity": "error",
                    "code": "canvas.render_disabled",
                    "message": "Canvas rendering is disabled.",
                }
            )
        if canvas.isDrawing():
            automated.append(
                {
                    "severity": "warning",
                    "code": "canvas.render_incomplete",
                    "message": "The canvas was still drawing when the image was captured.",
                }
            )
        if layout_check:
            automated.extend(
                {
                    "severity": item["severity"],
                    "code": "layout.{}".format(item["type"]),
                    "message": json.dumps(item, ensure_ascii=False, default=str),
                }
                for item in layout_check["issues"]
            )
        capture.update(
            {
                "action": action,
                "automated_review": {
                    "passed": not any(item["severity"] == "error" for item in automated),
                    "errors": sum(item["severity"] == "error" for item in automated),
                    "warnings": sum(item["severity"] == "warning" for item in automated),
                    "findings": automated,
                },
                "layout_review": layout_check,
                "corrections": corrections,
                "visual_checklist": [
                    "Confirm visual hierarchy, contrast and color accessibility.",
                    "Check label collisions, clipping and readability at the intended scale.",
                    "Check legend, scale, title, sources, dates and attribution when applicable.",
                    "Check geographic framing, empty space and whether important features are hidden.",
                ],
                "next_action": (
                    "Inspect the image content. Record a passing verdict, or call action=apply "
                    "with bounded correction_calls and review the returned image again."
                ),
            }
        )
        return capture

    @staticmethod
    def _visual_findings(findings):
        if findings is None:
            return []
        if not isinstance(findings, list) or len(findings) > 50:
            raise ValueError("findings must be an array of at most 50 items")
        normalized = []
        for item in findings:
            if not isinstance(item, dict) or not item.get("message"):
                raise ValueError("Every visual finding requires a message")
            severity = str(item.get("severity", "warning"))
            if severity not in {"info", "warning", "error"}:
                raise ValueError("Visual finding severity must be info, warning, or error")
            normalized.append(
                {
                    "severity": severity,
                    "code": str(item.get("code", "visual.model_review")),
                    "message": str(item["message"]),
                }
            )
        return normalized

    def _layout_screenshot(self, layout, page=0, max_width=1600):
        target = QgsProject.instance().layoutManager().layoutByName(str(layout))
        if target is None:
            raise KeyError("Print layout not found")
        image = QgsLayoutExporter(target).renderPageToImage(int(page))
        if image.isNull():
            raise RuntimeError("Could not render the requested layout page")
        if image.width() > int(max_width):
            image = image.scaledToWidth(
                int(max_width), Qt.TransformationMode.SmoothTransformation
            )
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "PNG"):
            raise RuntimeError("Could not encode the layout review image")
        import base64

        return {
            "data": base64.b64encode(bytes(buffer.data())).decode("ascii"),
            "mime_type": "image/png",
            "width": image.width(),
            "height": image.height(),
            "target": "layout:{}".format(layout),
            "page": int(page),
            "revision": self.state.revision,
        }

    def fire_map(self, **params):
        return self.fire_maps.build(**params)

    def connector_catalog(self):
        return {"connectors": [self.fire_maps.catalog()]}

    def batch_execute(self, calls, continue_on_error=False, atomic=False):
        if not isinstance(calls, list) or not 1 <= len(calls) <= 100:
            raise ValueError("calls must contain between 1 and 100 items")
        if any(not isinstance(call, dict) for call in calls):
            raise ValueError("Every batch call must be an object")
        checkpoint = None
        if atomic:
            checkpoint = self.checkpoints.create("Atomic batch", internal=True)
        results = []
        rolled_back = False
        try:
            for index, call in enumerate(calls[:100]):
                method = call.get("method")
                if method == "batch.execute":
                    raise ValueError("Nested batches are not supported")
                try:
                    results.append(
                        {
                            "index": index,
                            "result": self.dispatch(method, call.get("params") or {}),
                        }
                    )
                except DispatchError as exc:
                    results.append(
                        {
                            "index": index,
                            "error": {
                                "code": exc.code,
                                "message": exc.message,
                                "data": exc.data,
                            },
                        }
                    )
                    if atomic:
                        self.checkpoints.restore(checkpoint["checkpoint_id"])
                        rolled_back = True
                    if atomic or not continue_on_error:
                        break
        finally:
            if checkpoint:
                self.checkpoints.delete(checkpoint["checkpoint_id"])
        return {
            "results": results,
            "completed": len(results),
            "requested": len(calls),
            "atomic": bool(atomic),
            "rolled_back": rolled_back,
        }

    def runtime_control(self, **params):
        return self.runtime_tools.runtime(**params)

    def runtime_tasks(self, **params):
        return self.runtime_tools.tasks(**params)

    def runtime_events(self, **params):
        return self.runtime_tools.events(**params)

    def runtime_render(self, **params):
        return self.runtime_tools.render(**params)

    def runtime_transaction(self, **params):
        return self.runtime_tools.transaction(**params)

    def runtime_undo(self, **params):
        return self.runtime_tools.undo(**params)

    def runtime_preflight(self, **params):
        return self.runtime_tools.preflight(**params)

    def runtime_diff(self, **params):
        return self.runtime_tools.diff(**params)

    def runtime_diagnostics(self, **params):
        return self.runtime_tools.diagnostics(**params)

    def runtime_permissions(self):
        return self.runtime_tools.permissions()

    def runtime_auth(self, **params):
        return self.runtime_tools.auth(**params)

    def project_manage(self, **params):
        return self.project_tools.project(**params)

    def project_properties(self, **params):
        return self.project_tools.project_properties(**params)

    def project_repair(self, **params):
        return self.project_tools.repair(**params)

    def layer_source(self, **params):
        return self.project_tools.source(**params)

    def canvas_control(self, **params):
        return self.project_tools.canvas(**params)

    def map_identify(self, **params):
        return self.project_tools.identify(**params)

    def map_measure(self, **params):
        return self.project_tools.measure(**params)

    def bookmark_manage(self, **params):
        return self.project_tools.bookmarks(**params)

    def map_theme_manage(self, **params):
        return self.project_tools.themes(**params)

    def crs_control(self, **params):
        return self.project_tools.crs(**params)

    def expression_control(self, **params):
        return self.project_tools.expression(**params)

    def metadata_manage(self, **params):
        return self.project_tools.metadata(**params)

    def connection_inspect(self, **params):
        return self.project_tools.connections(**params)

    def vector_schema(self, **params):
        return self.vector_raster_tools.vector_schema(**params)

    def vector_statistics(self, **params):
        return self.vector_raster_tools.vector_statistics(**params)

    def geometry_edit(self, **params):
        return self.vector_raster_tools.geometry_edit(**params)

    def vector_indexes(self, **params):
        return self.vector_raster_tools.indexes(**params)

    def vector_joins(self, **params):
        return self.vector_raster_tools.joins(**params)

    def project_relations(self, **params):
        return self.vector_raster_tools.relations(**params)

    def project_snapping(self, **params):
        return self.vector_raster_tools.snapping(**params)

    def advanced_selection(self, **params):
        return self.vector_raster_tools.select(**params)

    def raster_inspect(self, **params):
        return self.vector_raster_tools.raster(**params)

    def processing_provider(self, **params):
        return self.processing_database_tools.processing_providers(**params)

    def processing_batch(self, **params):
        return self.processing_database_tools.processing_batch(**params)

    def processing_history(self, **params):
        return self.processing_database_tools.processing_history(**params)

    def processing_assets(self, **params):
        return self.processing_database_tools.processing_assets(**params)

    def processing_context(self):
        return self.processing_database_tools.processing_context()

    def database_control(self, **params):
        return self.processing_database_tools.database(**params)

    def connection_manage(self, **params):
        return self.processing_database_tools.connection_manage(**params)

    def advanced_renderer(self, **params):
        return self.advanced_cartography.renderer(**params)

    def advanced_symbol(self, **params):
        return self.advanced_cartography.symbol(**params)

    def style_library(self, **params):
        return self.advanced_cartography.style_library(**params)

    def advanced_labeling(self, **params):
        return self.advanced_cartography.labeling(**params)

    def layout_item(self, **params):
        return self.advanced_cartography.layout_items(**params)

    def layout_atlas(self, **params):
        return self.advanced_cartography.atlas(**params)

    def layout_validate(self, **params):
        return self.advanced_cartography.layout_validate(**params)

    def specialized_layer_properties(self, **params):
        return self.specialized_data_tools.layer_properties(**params)

    def specialized_layer_capabilities(self, **params):
        return self.specialized_data_tools.capabilities(**params)

    def specialized_raster_style(self, **params):
        return self.specialized_data_tools.raster_style(**params)

    def specialized_mesh(self, **params):
        return self.specialized_data_tools.mesh(**params)

    def specialized_point_cloud(self, **params):
        return self.specialized_data_tools.point_cloud(**params)

    def specialized_vector_tiles(self, **params):
        return self.specialized_data_tools.vector_tiles(**params)

    def specialized_tiled_scene(self, **params):
        return self.specialized_data_tools.tiled_scene(**params)

    def specialized_temporal(self, **params):
        return self.specialized_data_tools.temporal(**params)

    def specialized_elevation(self, **params):
        return self.specialized_data_tools.elevation(**params)

    def ecosystem_plugins(self, **params):
        return self.ecosystem_tools.plugins(**params)

    def ecosystem_settings(self, **params):
        return self.ecosystem_tools.settings(**params)

    def ecosystem_shortcuts(self, **params):
        return self.ecosystem_tools.shortcuts(**params)

    def ecosystem_gps(self, **params):
        return self.ecosystem_tools.gps(**params)

    def ecosystem_3d(self, **params):
        return self.ecosystem_tools.views_3d(**params)

    def ecosystem_server(self, **params):
        return self.ecosystem_tools.server(**params)

    def ecosystem_offline(self, **params):
        return self.ecosystem_tools.offline(**params)

    def authoring_forms(self, **params):
        return self.authoring_tools.forms(**params)

    def authoring_diagrams(self, **params):
        return self.authoring_tools.diagrams(**params)

    def authoring_annotations(self, **params):
        return self.authoring_tools.annotations(**params)

    def authoring_geometry_quality(self, **params):
        return self.authoring_tools.geometry_quality(**params)

    def authoring_vector_export(self, **params):
        return self.authoring_tools.vector_export(**params)

    def qa_compatibility(self, **params):
        return self.qa_tools.compatibility(**params)

    def qa_project_audit(self, **params):
        return self.qa_tools.project_audit(**params)

    def qa_benchmark(self, **params):
        return self.qa_tools.benchmark(**params)

    def qa_self_test(self):
        return self.qa_tools.self_test()

    def resources_list(self):
        resources = [
            _resource(SESSION_URI, "Current QGIS session", self.state),
            _resource(PROJECT_URI, "Current QGIS project", self.state),
            _resource(LAYER_TREE_URI, "Current project layer tree", self.state),
            _resource(CAPABILITIES_URI, "QGIS capability index", self.state),
            _resource(LOGS_URI, "Recent QGIS MCP events", self.state),
        ]
        for layer in QgsProject.instance().mapLayers().values():
            resources.extend(
                [
                    {
                        "uri": layer_uri(layer.id()),
                        "name": "Layer: {}".format(layer.name()),
                        "mimeType": "application/json",
                        "revision": self.state.resource_revision(layer_uri(layer.id())),
                    },
                    {
                        "uri": layer_uri(layer.id(), "schema"),
                        "name": "Layer schema: {}".format(layer.name()),
                        "mimeType": "application/json",
                        "revision": self.state.resource_revision(layer_uri(layer.id(), "schema")),
                    },
                    {
                        "uri": layer_uri(layer.id(), "selection"),
                        "name": "Layer selection: {}".format(layer.name()),
                        "mimeType": "application/json",
                        "revision": self.state.resource_revision(layer_uri(layer.id(), "selection")),
                    },
                ]
            )
        for operation in self.operations.list_public():
            resources.append(
                {
                    "uri": operation_uri(operation["id"]),
                    "name": "Operation: {}".format(operation["id"]),
                    "mimeType": "application/json",
                    "revision": self.state.resource_revision(operation_uri(operation["id"])),
                }
            )
        known_layer_ids = set(QgsProject.instance().mapLayers())
        for layer in self.operations.retained_layers():
            if layer.id() in known_layer_ids:
                continue
            resources.extend(
                [
                    {
                        "uri": layer_uri(layer.id()),
                        "name": "Retained output layer: {}".format(layer.name()),
                        "mimeType": "application/json",
                        "revision": self.state.resource_revision(layer_uri(layer.id())),
                    },
                    {
                        "uri": layer_uri(layer.id(), "schema"),
                        "name": "Retained output schema: {}".format(layer.name()),
                        "mimeType": "application/json",
                        "revision": self.state.resource_revision(layer_uri(layer.id(), "schema")),
                    },
                ]
            )
        return resources

    def resources_read(self, uri):
        parsed = urlparse(uri)
        if parsed.scheme != "qgis":
            raise ValueError("Unsupported resource URI")
        path = [unquote(item) for item in parsed.path.strip("/").split("/") if item]
        root = parsed.netloc
        value = None
        if root == "session":
            value = self.session_snapshot()
        if root == "project":
            value = self.project_inspect("layer_tree" if path == ["layer-tree"] else "project")
        if root == "capabilities":
            value = self.capabilities.summary()
        if root == "logs":
            value = self.logs_read()
        if root == "layers" and path:
            include = [path[1]] if len(path) > 1 and path[1] in {"schema", "selection"} else None
            value = self.layer_inspect(path[0], include=include)
        if root == "operations" and path:
            value = self.operation_control(path[0])
        if value is None:
            raise ValueError("Unknown QGIS resource URI")
        return {"uri": uri, "revision": self.state.resource_revision(uri), "value": value}

    def _layer(self, identifier):
        project = QgsProject.instance()
        layer = project.mapLayer(str(identifier))
        if layer is None:
            layer = self.operations.map_layer(str(identifier))
        if layer is not None:
            return layer
        matches = project.mapLayersByName(str(identifier))
        if not matches:
            raise KeyError("Layer not found: {}".format(identifier))
        if len(matches) > 1:
            raise ValueError("Layer name is ambiguous; use the layer ID")
        return matches[0]

    def _vector_layer(self, identifier):
        layer = self._layer(identifier)
        if not isinstance(layer, QgsVectorLayer):
            raise ValueError("Layer is not a vector layer")
        return layer


def _tree_node(node):
    value = {
        "name": node.name(),
        "visible": node.isVisible(),
        "expanded": node.isExpanded(),
    }
    if hasattr(node, "layerId"):
        value["kind"] = "layer"
        value["layer_id"] = node.layerId()
    else:
        value["kind"] = "group"
        value["children"] = [_tree_node(child) for child in node.children()]
    return value


def _resource(uri, name, state):
    return {
        "uri": uri,
        "name": name,
        "mimeType": "application/json",
        "revision": state.resource_revision(uri),
    }


class QgsApplicationMessageLog:
    _signal = None
    _callback = None

    @classmethod
    def connect(cls, log):
        from qgis.core import QgsApplication

        cls._signal = QgsApplication.messageLog().messageReceived

        def callback(message, tag, level):
            level_name = {
                Qgis.MessageLevel.Info: "info",
                Qgis.MessageLevel.Warning: "warning",
                Qgis.MessageLevel.Critical: "error",
                Qgis.MessageLevel.Success: "info",
            }.get(level, "info")
            log.add("qgis.message", message, level_name, {"tag": tag})

        cls._callback = callback
        cls._signal.connect(callback)

    @classmethod
    def disconnect(cls, log):
        if cls._signal is not None and cls._callback is not None:
            try:
                cls._signal.disconnect(cls._callback)
            except (RuntimeError, TypeError) as exc:
                log.add("qgis.message", "Message log disconnect failed", "warning", {"cause": str(exc)})
        cls._signal = None
        cls._callback = None
