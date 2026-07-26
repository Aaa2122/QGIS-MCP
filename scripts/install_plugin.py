from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from package_support import copy_packaged_plugin


def default_plugins_directory(profile: str) -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not defined; pass --plugins-dir explicitly")
    return (
        Path(appdata)
        / "QGIS"
        / "QGIS3"
        / "profiles"
        / profile
        / "python"
        / "plugins"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the QGIS Agent MCP plugin")
    parser.add_argument("--profile", default="default", help="QGIS profile name")
    parser.add_argument("--plugins-dir", type=Path, help="Override QGIS plugins directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    plugins_dir = args.plugins_dir or default_plugins_directory(args.profile)
    target = plugins_dir.expanduser().resolve() / "qgis_agent_mcp"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        resolved_plugins = plugins_dir.resolve()
        resolved_target = target.resolve()
        if resolved_target.parent != resolved_plugins:
            raise RuntimeError("Refusing to replace a plugin outside the selected directory")
        shutil.rmtree(resolved_target)
    copy_packaged_plugin(root, target)
    print("Installed QGIS Agent MCP plugin at {}".format(target))
    print("Enable it in QGIS under Plugins > Manage and Install Plugins.")


if __name__ == "__main__":
    main()
