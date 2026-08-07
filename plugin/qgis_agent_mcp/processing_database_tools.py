from __future__ import annotations

import re
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsFeedback,
    QgsProcessing,
    QgsProcessingUtils,
    QgsProviderRegistry,
)

from .serialize import field_schema, json_safe


class ProcessingDatabaseTools:
    def __init__(self, state, operations):
        self.state = state
        self.operations = operations

    def processing_providers(self, action="list", provider=None):
        registry = QgsApplication.processingRegistry()
        if action == "refresh":
            if not provider:
                raise ValueError("provider is required")
            selected = registry.providerById(str(provider))
            if selected is None:
                raise KeyError("Processing provider not found")
            selected.refreshAlgorithms()
            self.state.touch("processing.provider_refreshed", {"provider": provider})
        elif action != "list":
            raise ValueError("Unknown Processing provider action")
        return {
            "providers": [
                {
                    "id": item.id(),
                    "name": item.name(),
                    "active": bool(item.isActive()),
                    "algorithm_count": len(item.algorithms()),
                    "supported_output_raster_extensions": list(
                        item.supportedOutputRasterLayerExtensions()
                    ),
                    "supported_output_vector_extensions": list(
                        item.supportedOutputVectorLayerExtensions()
                    ),
                }
                for item in registry.providers()
            ]
        }

    def processing_batch(
        self,
        algorithm,
        rows,
        retain_outputs=True,
        add_to_project=False,
        stop_on_error=False,
        allow_main_thread=False,
    ):
        if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
            raise ValueError("rows must contain between 1 and 50 parameter objects")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("Every batch row must be an object")
        registry = QgsApplication.processingRegistry()
        if registry.algorithmById(str(algorithm)) is None:
            raise KeyError("Processing algorithm not found")
        operations = []
        for index, parameters in enumerate(rows):
            try:
                started = self.operations.start_processing(
                    str(algorithm),
                    dict(parameters),
                    bool(retain_outputs),
                    bool(add_to_project),
                    bool(allow_main_thread),
                )
                operations.append({"row": index, "operation": started})
            except Exception as exc:
                operations.append(
                    {
                        "row": index,
                        "error": {"message": str(exc), "exception": type(exc).__name__},
                    }
                )
                if stop_on_error:
                    break
        self.state.touch(
            "processing.batch_started",
            {"algorithm": algorithm, "count": len(operations)},
        )
        return {
            "algorithm": str(algorithm),
            "requested": len(rows),
            "started": sum("operation" in item for item in operations),
            "items": operations,
        }

    def processing_history(self, action="list", operation_id=None):
        operations = self.operations.list_public()
        if action == "list":
            return {"operations": operations}
        if action == "replay":
            source = next(
                (item for item in operations if item.get("id") == str(operation_id)),
                None,
            )
            if source is None:
                raise KeyError("Processing operation not found")
            started = self.operations.start_processing(
                source["algorithm"],
                dict(source.get("parameters") or {}),
                bool(source.get("retain_outputs", True)),
                bool(source.get("add_to_project", False)),
                bool(source.get("allow_main_thread", False)),
            )
            self.state.touch(
                "processing.operation_replayed",
                {"source_id": operation_id, "id": started["id"]},
            )
            return {"source_operation_id": str(operation_id), "operation": started}
        raise ValueError("Unknown Processing history action")

    def processing_assets(self, kind="models", query="", limit=100):
        kind = str(kind)
        if kind not in {"models", "scripts", "algorithms"}:
            raise ValueError("kind must be models, scripts, or algorithms")
        query = str(query or "").casefold()
        items = []
        for provider in QgsApplication.processingRegistry().providers():
            for algorithm in provider.algorithms():
                algorithm_id = algorithm.id()
                provider_id = provider.id()
                is_model = provider_id == "model" or algorithm_id.startswith("model:")
                is_script = provider_id == "script" or algorithm_id.startswith("script:")
                if kind == "models" and not is_model:
                    continue
                if kind == "scripts" and not is_script:
                    continue
                haystack = "{} {} {}".format(
                    algorithm_id, algorithm.displayName(), algorithm.group()
                ).casefold()
                if query and query not in haystack:
                    continue
                items.append(
                    {
                        "id": algorithm_id,
                        "name": algorithm.displayName(),
                        "group": algorithm.group(),
                        "provider": provider_id,
                        "flags": int(algorithm.flags()),
                    }
                )
        limit = max(1, min(int(limit), 1000))
        return {"kind": kind, "items": items[:limit], "count": len(items), "has_more": len(items) > limit}

    def processing_context(self):
        return {
            "temporary_output_token": str(QgsProcessing.TEMPORARY_OUTPUT),
            "temporary_folder": str(Path(QgsProcessingUtils.tempFolder())),
            "providers": [item.id() for item in QgsApplication.processingRegistry().providers()],
            "operation_count": len(self.operations.list_public()),
        }

    def database(
        self,
        provider,
        connection,
        action="schemas",
        schema="",
        table=None,
        sql=None,
        limit=1000,
        allow_mutation=False,
        new_name=None,
        allow_blocking=False,
    ):
        if action in {"query", "vacuum"} and not allow_blocking:
            raise ValueError(
                "This database action can block QGIS while the provider waits. "
                "Retry with allow_blocking=true only when the connection is known "
                "to be responsive."
            )
        _, database = _database_connection(provider, connection)
        if action == "schemas":
            return {"provider": provider, "connection": connection, "schemas": list(database.schemas())}
        if action == "tables":
            return {
                "provider": provider,
                "connection": connection,
                "schema": schema,
                "tables": [_table_summary(item) for item in database.tables(str(schema or ""))],
            }
        if action == "fields":
            if not table:
                raise ValueError("table is required")
            fields = database.fields(str(schema or ""), str(table))
            return {"schema": schema, "table": table, "fields": [field_schema(item) for item in fields]}
        if action == "query":
            if not sql:
                raise ValueError("sql is required")
            if not allow_mutation and not _read_only_sql(sql):
                raise ValueError("Mutating SQL requires allow_mutation=true")
            feedback = QgsFeedback()
            result = database.execSql(str(sql), feedback)
            rows = []
            limit = max(1, min(int(limit), 10000))
            while result.hasNextRow() and len(rows) < limit + 1:
                rows.append(json_safe(result.nextRow()))
            return {
                "columns": list(result.columns()),
                "rows": rows[:limit],
                "has_more": len(rows) > limit or result.hasNextRow(),
                "fetched": min(len(rows), limit),
                "execution_time_ms": result.queryExecutionTime(),
                "mutation_allowed": bool(allow_mutation),
                "blocking_opt_in": bool(allow_blocking),
            }
        if action == "create_schema":
            if not allow_mutation or not schema:
                raise ValueError("schema and allow_mutation=true are required")
            database.createSchema(str(schema))
        elif action == "drop_schema":
            if not allow_mutation or not schema:
                raise ValueError("schema and allow_mutation=true are required")
            database.dropSchema(str(schema), False)
        elif action == "rename_schema":
            if not allow_mutation or not schema or not new_name:
                raise ValueError("schema, new_name, and allow_mutation=true are required")
            database.renameSchema(str(schema), str(new_name))
        elif action == "drop_table":
            if not allow_mutation or not table:
                raise ValueError("table and allow_mutation=true are required")
            database.dropVectorTable(str(schema or ""), str(table))
        elif action == "rename_table":
            if not allow_mutation or not table or not new_name:
                raise ValueError("table, new_name, and allow_mutation=true are required")
            database.renameVectorTable(str(schema or ""), str(table), str(new_name))
        elif action == "vacuum":
            if not allow_mutation:
                raise ValueError("allow_mutation=true is required")
            database.vacuum(str(schema or ""), str(table or ""))
        else:
            raise ValueError("Unknown database action")
        self.state.touch(
            "database.{}".format(action),
            {"provider": provider, "connection": connection, "schema": schema, "table": table},
        )
        return {"action": action, "ok": True, "provider": provider, "connection": connection}

    def connection_manage(
        self,
        provider,
        action,
        name=None,
        uri=None,
        configuration=None,
    ):
        metadata = QgsProviderRegistry.instance().providerMetadata(str(provider))
        if metadata is None:
            raise KeyError("Provider not found")
        if action == "create":
            if not name or not uri:
                raise ValueError("name and uri are required")
            created = metadata.createConnection(str(uri), dict(configuration or {}))
            if created is None:
                raise RuntimeError("Provider could not create the connection")
            metadata.saveConnection(created, str(name))
        elif action == "delete":
            if not name:
                raise ValueError("name is required")
            metadata.deleteConnection(str(name))
        elif action == "test":
            if not name:
                raise ValueError("name is required")
            connection = metadata.createConnection(str(name))
            if connection is None:
                raise KeyError("Connection not found")
            details = {
                "provider": str(provider),
                "name": str(name),
                "type": type(connection).__name__,
                "capabilities": _enum_value(connection.capabilities()),
            }
            if hasattr(connection, "schemas"):
                details["schema_count"] = len(connection.schemas())
            return {"ok": True, "connection": details}
        else:
            raise ValueError("Unknown connection management action")
        self.state.touch(
            "connection.{}".format(action),
            {"provider": provider, "name": name},
        )
        return {"action": action, "provider": str(provider), "name": str(name), "ok": True}


def _database_connection(provider, name):
    metadata = QgsProviderRegistry.instance().providerMetadata(str(provider))
    if metadata is None:
        raise KeyError("Provider not found")
    connections = metadata.connections(False)
    connection = connections.get(str(name))
    if connection is None:
        raise KeyError("Stored provider connection not found")
    if not hasattr(connection, "tables") or not hasattr(connection, "execSql"):
        raise ValueError("Connection is not a database connection")
    return metadata, connection


def _table_summary(table):
    return {
        "schema": table.schema(),
        "name": table.tableName(),
        "geometry_column": table.geometryColumn(),
        "geometry_column_count": table.geometryColumnCount(),
        "primary_key_columns": list(table.primaryKeyColumns()),
        "flags": _enum_value(table.flags()),
        "comment": table.comment(),
        "max_coordinate_dimensions": table.maxCoordinateDimensions(),
        "crs": [crs.authid() for crs in table.crsList()],
    }


def _read_only_sql(sql):
    cleaned = re.sub(r"/\*.*?\*/", " ", str(sql), flags=re.DOTALL)
    cleaned = re.sub(r"--[^\n]*", " ", cleaned).strip()
    first = cleaned.split(None, 1)[0].casefold() if cleaned else ""
    return first in {"select", "with", "explain", "pragma", "show", "describe"}


def _enum_value(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)
