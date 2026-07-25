from __future__ import annotations

import sys
import unittest

from qgis.PyQt.QtWidgets import QApplication


def run_all():
    suite = unittest.defaultTestLoader.discover(
        "tests/qgis", pattern="test_*.py", top_level_dir="."
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    QApplication.instance().exit(0 if result.wasSuccessful() else 1)
    if not result.wasSuccessful():
        sys.__stderr__.write("QGIS integration suite failed\n")

