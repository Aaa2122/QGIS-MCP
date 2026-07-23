from __future__ import annotations

import json

import pytest

from qgis_mcp.config import ConnectionInfo, load_connection_info


def test_load_connection_info(tmp_path):
    path = tmp_path / "connection.json"
    path.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 54321,
                "token": "a" * 64,
                "protocol": 1,
                "pid": 42,
            }
        ),
        encoding="utf-8",
    )
    info = load_connection_info(path)
    assert info == ConnectionInfo("127.0.0.1", 54321, "a" * 64, 1, 42, None)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "example.com"])
def test_connection_rejects_non_loopback(host):
    with pytest.raises(ValueError, match="loopback"):
        ConnectionInfo.from_dict({"host": host, "port": 1, "token": "x" * 32})


def test_missing_connection_file_has_actionable_error(tmp_path):
    with pytest.raises(RuntimeError, match="Start QGIS"):
        load_connection_info(tmp_path / "missing.json")

