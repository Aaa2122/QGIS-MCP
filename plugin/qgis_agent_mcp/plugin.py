from __future__ import annotations

from qgis.PyQt.QtWidgets import QAction
from qgis.core import Qgis

from .bridge import LocalBridge
from .dispatcher import Dispatcher


class QgisAgentMcpPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dispatcher = None
        self.bridge = None
        self.status_action = None

    def initGui(self):
        try:
            self.dispatcher = Dispatcher(self.iface)
            self.bridge = LocalBridge(self.dispatcher, self.iface.mainWindow())
            self.bridge.start()
            self.status_action = QAction("QGIS Agent MCP status", self.iface.mainWindow())
            self.status_action.triggered.connect(self._show_status)
            self.iface.addPluginToMenu("&QGIS Agent MCP", self.status_action)
            self.iface.messageBar().pushMessage(
                "QGIS Agent MCP",
                "Local bridge listening on port {}".format(self.bridge.port),
                level=Qgis.Success,
                duration=8,
            )
        except Exception as exc:
            self.iface.messageBar().pushMessage(
                "QGIS Agent MCP",
                "Bridge failed to start: {}".format(exc),
                level=Qgis.Critical,
                duration=0,
            )
            self.unload()
            raise

    def unload(self):
        if self.status_action is not None:
            try:
                self.iface.removePluginMenu("&QGIS Agent MCP", self.status_action)
            except Exception:
                pass
            self.status_action.deleteLater()
            self.status_action = None
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge.deleteLater()
            self.bridge = None
        if self.dispatcher is not None:
            self.dispatcher.close()
            self.dispatcher = None

    def _show_status(self):
        if self.bridge is None:
            message = "Bridge is not running"
            level = Qgis.Warning
        else:
            message = (
                "Listening on 127.0.0.1:{}; {} authenticated client(s); "
                "Python execution {}"
            ).format(
                self.bridge.port,
                sum(
                    1
                    for state in self.bridge.clients.values()
                    if state["authenticated"]
                ),
                "enabled" if self.dispatcher.python_enabled else "disabled",
            )
            level = Qgis.Info
        self.iface.messageBar().pushMessage(
            "QGIS Agent MCP", message, level=level, duration=10
        )

