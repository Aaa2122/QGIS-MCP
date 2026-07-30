from __future__ import annotations

from pathlib import Path

from build_plugin import build_plugin
from check_qgis_plugin_security import (
    QGIS_BLOCKING_BANDIT_RULES,
    validate_package_structure,
)

ROOT = Path(__file__).resolve().parents[1]


def test_security_gate_tracks_non_skippable_python_execution_rules():
    assert {"B102", "B307"} <= QGIS_BLOCKING_BANDIT_RULES


def test_built_plugin_passes_publication_structure_checks(tmp_path):
    package = build_plugin(ROOT, tmp_path / "qgis_agent_mcp.zip")
    root, issues = validate_package_structure(package)
    assert root == "qgis_agent_mcp"
    assert issues == []
