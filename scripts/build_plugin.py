from __future__ import annotations

import argparse
import tempfile
import zipfile
from pathlib import Path

from package_support import copy_packaged_plugin


def build_plugin(root: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qgis-agent-mcp-") as temporary:
        staging = Path(temporary) / "qgis_agent_mcp"
        copy_packaged_plugin(root, staging)
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(
                        path,
                        Path("qgis_agent_mcp") / path.relative_to(staging),
                    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an installable QGIS Agent MCP plugin ZIP"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / "dist" / "qgis_agent_mcp-0.4.3.zip"
    built = build_plugin(root, output.resolve())
    print("Built QGIS plugin package at {}".format(built))


if __name__ == "__main__":
    main()
