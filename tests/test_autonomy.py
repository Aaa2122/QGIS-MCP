from __future__ import annotations

import json
import zipfile

import pytest
from qgis_agent_mcp.autonomy import (
    DataCache,
    NetworkPolicy,
    OutputPathPolicy,
    PolicyError,
    redact_url,
    safe_extract_zip,
)


def test_network_policy_blocks_credentials_private_hosts_and_fragments():
    policy = NetworkPolicy(allow_private=False)
    assert policy.validate("https://8.8.8.8/data#fragment", resolve=False) == "https://8.8.8.8/data"
    with pytest.raises(PolicyError, match="Credentials"):
        policy.validate("https://user:pass@example.com/data", resolve=False)
    with pytest.raises(PolicyError, match="Private"):
        policy.validate("http://127.0.0.1/data", resolve=False)


def test_network_policy_enforces_host_allowlist():
    policy = NetworkPolicy(allowed_hosts=["example.com"], allow_private=True)
    assert policy.validate("https://data.example.com/a", resolve=False)
    with pytest.raises(PolicyError, match="allow-listed"):
        policy.validate("https://example.net/a", resolve=False)


def test_output_paths_are_restricted(tmp_path):
    policy = OutputPathPolicy([tmp_path / "allowed"])
    target = policy.validate(tmp_path / "allowed" / "map.pdf")
    assert target.parent.is_dir()
    with pytest.raises(PolicyError, match="outside"):
        policy.validate(tmp_path / "blocked" / "map.pdf")


def test_data_cache_records_hash_and_serves_fresh_entries(tmp_path):
    cache = DataCache(tmp_path, max_bytes=1024)
    stored = cache.put("https://example.com/data", b"abc", "data.csv", {"source": "test"})
    assert stored["sha256"]
    cached = cache.lookup("https://example.com/data", 60)
    assert cached["cache_hit"] is True
    assert cached["source"] == "test"
    assert open(cached["path"], "rb").read() == b"abc"


def test_redaction_hides_query_and_path_secrets():
    value = redact_url(
        "https://example.com/api/SECRET/data?token=abc&visible=yes",
        ["SECRET"],
    )
    assert "SECRET" not in value
    assert "token=%2A%2A%2A" in value
    assert "visible=yes" in value


def test_zip_extraction_rejects_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../escape.txt", json.dumps({"bad": True}))
    with pytest.raises(PolicyError, match="unsafe"):
        safe_extract_zip(archive, tmp_path / "output")
