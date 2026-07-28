from __future__ import annotations

import shutil
from pathlib import Path

IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


def copy_packaged_plugin(root: Path, target: Path) -> None:
    source = root / "plugin" / "qgis_agent_mcp"
    if not source.is_dir():
        raise RuntimeError("Plugin source directory not found: {}".format(source))
    server_source = root / "src" / "qgis_mcp"
    if not (server_source / "__main__.py").is_file():
        raise RuntimeError("MCP server package not found: {}".format(server_source))
    shutil.copytree(source, target, ignore=IGNORED)
    license_path = root / "LICENSE"
    if not license_path.is_file():
        raise RuntimeError("Plugin license not found: {}".format(license_path))
    shutil.copy2(license_path, target / "LICENSE")
    shutil.copytree(
        server_source,
        target / "_server" / "qgis_mcp",
        ignore=IGNORED,
    )
