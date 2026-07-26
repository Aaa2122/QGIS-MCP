from __future__ import annotations

import base64

import pytest
from qgis_agent_mcp.store import ArtifactStore


def test_artifact_store_chunks_hashes_and_releases():
    store = ArtifactStore(max_item_bytes=16, max_total_bytes=16, max_read_bytes=3)
    descriptor = store.put_bytes(b"abcdef", "application/test", "sample.bin")
    first = store.read(descriptor["artifact_id"], length=99)
    assert base64.b64decode(first["data"]) == b"abc"
    assert first["eof"] is False
    second = store.read(descriptor["artifact_id"], offset=3, length=3)
    assert base64.b64decode(second["data"]) == b"def"
    assert second["eof"] is True
    assert store.release(descriptor["artifact_id"]) is True
    with pytest.raises(KeyError):
        store.read(descriptor["artifact_id"])


def test_artifact_store_evicts_lru_to_stay_bounded():
    store = ArtifactStore(max_items=2, max_item_bytes=8, max_total_bytes=8)
    first = store.put_bytes(b"1111")
    second = store.put_bytes(b"2222")
    store.read(first["artifact_id"], length=1)
    third = store.put_bytes(b"3333")
    ids = {item["artifact_id"] for item in store.list()}
    assert first["artifact_id"] in ids
    assert second["artifact_id"] not in ids
    assert third["artifact_id"] in ids

