from __future__ import annotations

import pytest
from qgis_agent_mcp.reliability import IdempotencyConflict, MutationGuard
from qgis_agent_mcp.revisions import (
    PROJECT_URI,
    SESSION_URI,
    ResourceRevisionIndex,
    layer_uri,
    operation_uri,
)


def test_resource_revisions_only_advance_for_affected_resources():
    index = ResourceRevisionIndex()
    layer = layer_uri("roads/id")
    affected = index.affected("selection.set", {"layer_id": "roads/id"})
    index.bump(affected, 7)
    assert index.revision(SESSION_URI) == 7
    assert index.revision(layer) == 7
    assert index.revision(layer_uri("roads/id", "selection")) == 7
    assert index.revision(PROJECT_URI) == 0
    assert operation_uri("op 1") == "qgis://operations/op%201"


def test_mutation_guard_replays_same_call_and_rejects_key_reuse():
    guard = MutationGuard(max_entries=2)
    guard.remember("request-1", "vector.edit", {"feature_ids": [1]}, {"ok": True})
    found, result = guard.lookup(
        "request-1", "vector.edit", {"feature_ids": [1]}
    )
    assert found is True
    assert result == {"ok": True}
    with pytest.raises(IdempotencyConflict):
        guard.lookup("request-1", "vector.edit", {"feature_ids": [2]})


def test_mutation_guard_survives_process_restart(tmp_path):
    path = tmp_path / "idempotency.json"
    first = MutationGuard(path=path)
    first.remember(
        "workflow:one:run:0",
        "layer.manage",
        {"action": "create_group", "name": "fires"},
        {"created": True},
    )

    restarted = MutationGuard(path=path)
    found, result = restarted.lookup(
        "workflow:one:run:0",
        "layer.manage",
        {"action": "create_group", "name": "fires"},
    )
    assert found is True
    assert result == {"created": True}


def test_mutation_guard_bounds_large_persisted_results(tmp_path):
    path = tmp_path / "idempotency.json"
    guard = MutationGuard(path=path, max_result_bytes=16)
    guard.remember("large", "project.action", {"action": "save"}, {"data": "x" * 100})

    restarted = MutationGuard(path=path, max_result_bytes=16)
    found, result = restarted.lookup("large", "project.action", {"action": "save"})
    assert found is True
    assert result["idempotency_replayed"] is True
    assert result["result_unavailable_after_restart"] is True
