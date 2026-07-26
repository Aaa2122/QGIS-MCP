from __future__ import annotations

import contextlib
import os
import sys
import traceback
import unittest

import qgis.utils
from qgis.PyQt.QtWidgets import QApplication


def run_all():
    suite = unittest.defaultTestLoader.discover(
        "tests/qgis", pattern="test_*.py", top_level_dir="."
    )
    log_path = os.environ.get("QGIS_MCP_TEST_LOG")
    stream_context = (
        open(log_path, "a", encoding="utf-8")
        if log_path
        else contextlib.nullcontext(sys.__stderr__)
    )
    with stream_context as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        unload_succeeded = True
        try:
            qgis.utils.unloadPlugin("qgis_agent_mcp")
            stream.write("QGIS MCP plugin unloaded cleanly.\n")
        except Exception:
            unload_succeeded = False
            traceback.print_exc(file=stream)
    succeeded = result.wasSuccessful() and unload_succeeded
    QApplication.instance().exit(0 if succeeded else 1)
    if not succeeded:
        sys.__stderr__.write("QGIS integration suite failed\n")
