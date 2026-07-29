from __future__ import annotations

from pathlib import Path

from qgis.core import Qgis
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .bridge import LocalBridge
from .connect_dialog import ConnectAiDialog
from .dispatcher import Dispatcher


class QgisAgentMcpPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dispatcher = None
        self.bridge = None
        self.status_action = None
        self.connect_action = None
        self.connect_dialog = None

    def initGui(self):
        try:
            self.dispatcher = Dispatcher(self.iface)
            self.bridge = LocalBridge(self.dispatcher, self.iface.mainWindow())
            self.bridge.start()
            plugin_icon = QIcon(str(Path(__file__).resolve().parent / "icon.png"))
            self.status_action = QAction("QGIS Agent MCP status", self.iface.mainWindow())
            self.status_action.setIcon(plugin_icon)
            self.status_action.triggered.connect(self._show_status)
            self.connect_action = QAction(
                plugin_icon,
                "Connect an AI client",
                self.iface.mainWindow(),
            )
            self.connect_action.setToolTip(
                "Automatically connect Codex, Claude Code, or another MCP client"
            )
            self.connect_action.triggered.connect(self._show_connect_dialog)
            self.iface.addPluginToMenu("&QGIS Agent MCP", self.status_action)
            self.iface.addPluginToMenu("&QGIS Agent MCP", self.connect_action)
            self.iface.addToolBarIcon(self.connect_action)
            self.iface.messageBar().pushMessage(
                "QGIS Agent MCP",
                "Bridge ready. Click “Connect an AI client” for one-click setup.",
                level=Qgis.Success,
                duration=12,
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
        if self.connect_dialog is not None:
            self.connect_dialog.close()
            self.connect_dialog.deleteLater()
            self.connect_dialog = None
        if self.connect_action is not None:
            try:
                self.iface.removePluginMenu("&QGIS Agent MCP", self.connect_action)
                self.iface.removeToolBarIcon(self.connect_action)
            except Exception:
                pass
            self.connect_action.deleteLater()
            self.connect_action = None
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

    def _show_connect_dialog(self):
        if self.connect_dialog is None:
            self.connect_dialog = ConnectAiDialog(
                Path(__file__).resolve().parent,
                self.iface.mainWindow(),
            )
            self.connect_dialog.finished.connect(self._connect_dialog_closed)
        self.connect_dialog.show()
        self.connect_dialog.raise_()
        self.connect_dialog.activateWindow()

    def _connect_dialog_closed(self):
        if self.connect_dialog is not None:
            self.connect_dialog.deleteLater()
            self.connect_dialog = None
