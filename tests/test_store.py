from __future__ import annotations

import json

import pytest
from qgis_agent_mcp.store import EventLog, HandleStore, PersistentDiagnostics


def test_event_log_is_revisioned_and_pageable():
    log = EventLog(capacity=3)
    log.add("test", "one")
    log.add("test", "two", "warning")
    log.add("test", "three")
    log.add("test", "four")
    result = log.read(after=1, limit=2)
    assert [item["message"] for item in result["events"]] == ["two", "three"]
    assert result["latest"] == 4
    assert result["has_more"] is True


def test_handle_store_pages_and_expires(monkeypatch):
    clock = iter([0.0, 0.0, 0.5, 2.0])
    monkeypatch.setattr("qgis_agent_mcp.store.time.monotonic", lambda: next(clock))
    store = HandleStore(ttl_seconds=1)
    descriptor = store.put([1, 2, 3])
    page = store.read(descriptor["handle"], offset=1, limit=1)
    assert page["items"] == [2]
    assert page["has_more"] is True
    with pytest.raises(KeyError, match="expired"):
        store.read(descriptor["handle"])


def test_handle_store_keeps_pages_bounded_with_nested_handles():
    store = HandleStore(max_read_bytes=1024)
    descriptor = store.put({"small": 1, "large": ["x" * 10_000]})
    first = store.read(descriptor["handle"], limit=10)
    assert first["items"][0] == {"key": "small", "value": 1}
    nested = first["items"][1]["value"]
    assert nested["truncated"] is True
    assert len(json.dumps(first).encode("utf-8")) < 2048

    nested_page = store.read(nested["handle"])
    scalar_handle = nested_page["items"][0]
    scalar_page = store.read(scalar_handle["handle"])
    assert len(scalar_page["value"].encode("utf-8")) <= 1024
    assert scalar_page["has_more"] is True


def test_persistent_diagnostics_recovers_interrupted_call_without_values(tmp_path):
    path = tmp_path / "diagnostics.json"
    first = PersistentDiagnostics(path)
    first.begin("data.fetch", {"url": "https://secret.example", "token": "secret"})

    recovered = PersistentDiagnostics(path)
    snapshot = recovered.snapshot()
    assert [item["method"] for item in snapshot["previous_interruption"]] == [
        "data.fetch"
    ]
    assert snapshot["previous_interruption"][0]["parameter_names"] == ["token", "url"]
    assert "secret.example" not in path.read_text(encoding="utf-8")
    assert snapshot["parameter_values_persisted"] is False


def test_persistent_diagnostics_clears_successful_call(tmp_path):
    path = tmp_path / "diagnostics.json"
    diagnostics = PersistentDiagnostics(path)
    token = diagnostics.begin("project.inspect", {"section": "project"})
    diagnostics.finish(token, "succeeded")

    restarted = PersistentDiagnostics(path)
    assert restarted.previous_interruption == []
    assert restarted.snapshot()["history"][-1]["status"] == "succeeded"


def test_read_only_diagnostics_batch_disk_writes(tmp_path):
    path = tmp_path / "diagnostics.json"
    diagnostics = PersistentDiagnostics(path, flush_interval_seconds=3600)
    for _ in range(20):
        token = diagnostics.begin("project.inspect", {}, durable=False)
        diagnostics.finish(token, "succeeded", durable=False)
    assert diagnostics._write_count == 0
    assert not path.exists()
    diagnostics.flush()
    assert diagnostics._write_count == 1
    assert path.exists()
