from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import qgis.utils
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import QApplication

LOG_PATH = Path(os.environ.get("QGIS_MCP_TEST_LOG", Path.cwd() / "qgis-test.log"))

try:
    LOG_PATH.write_text("QGIS test bootstrap reached\n", encoding="utf-8")
    configured_root = os.environ.get("QGIS_MCP_REPO_ROOT")
    ROOT = Path(configured_root).resolve() if configured_root else Path.cwd().resolve()
    for path in (ROOT, ROOT / "src", ROOT / "plugin"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    if not qgis.utils.loadPlugin("qgis_agent_mcp"):
        raise RuntimeError(
            "Could not load qgis_agent_mcp; plugin paths: {}".format(qgis.utils.plugin_paths)
        )
    if not qgis.utils.startPlugin("qgis_agent_mcp"):
        raise RuntimeError("Could not start qgis_agent_mcp in QGIS")

    from tests.qgis.run_all import run_all

    with LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write("Plugin started; scheduling test suite\n")
    QTimer.singleShot(0, run_all)
except Exception:
    with LOG_PATH.open("a", encoding="utf-8") as stream:
        traceback.print_exc(file=stream)
    QTimer.singleShot(0, lambda: QApplication.instance().exit(1))
