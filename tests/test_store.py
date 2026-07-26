from __future__ import annotations

import pytest
from qgis_agent_mcp.store import EventLog, HandleStore


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
