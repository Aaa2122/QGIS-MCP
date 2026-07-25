from __future__ import annotations

from pathlib import Path

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .onboarding import (
    ClaudeCodeConnector,
    CodexConnector,
    OnboardingError,
    RuntimeManager,
    health_check,
    universal_config,
)


class ConnectAiDialog(QDialog):
    def __init__(self, plugin_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect QGIS Agent MCP")
        self.setMinimumWidth(680)
        self.spec = None
        self.codex = CodexConnector()
        self.claude = ClaudeCodeConnector()
        self._status_labels = {}
        self._connect_buttons = {}
        self._disconnect_buttons = {}
        self._build_ui()
        self._prepare_runtime(Path(plugin_dir))
        self._refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            "Connect an AI client without copying a port, token, or configuration "
            "file. QGIS keeps the local session credentials synchronized."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.runtime_label = QLabel("Preparing the bundled MCP runtime…")
        self.runtime_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.runtime_label)

        clients = QGroupBox("Detected AI clients")
        grid = QGridLayout(clients)
        grid.addWidget(QLabel("<b>Client</b>"), 0, 0)
        grid.addWidget(QLabel("<b>Status</b>"), 0, 1)
        grid.addWidget(QLabel("<b>Actions</b>"), 0, 2)
        self._add_client_row(grid, 1, "codex", "Codex")
        self._add_client_row(grid, 2, "claude", "Claude Code")
        root.addWidget(clients)

        universal = QGroupBox("Other MCP clients")
        universal_layout = QHBoxLayout(universal)
        universal_layout.addWidget(
            QLabel("Copy a standard stdio MCP configuration for another client.")
        )
        universal_layout.addStretch(1)
        copy_button = QPushButton("Copy universal configuration")
        copy_button.clicked.connect(self._copy_universal)
        universal_layout.addWidget(copy_button)
        root.addWidget(universal)

        test_layout = QHBoxLayout()
        test_button = QPushButton("Test QGIS connection")
        test_button.clicked.connect(self._test_connection)
        test_layout.addWidget(test_button)
        test_layout.addStretch(1)
        root.addLayout(test_layout)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setPlaceholderText("Connection and repair details appear here.")
        root.addWidget(self.log, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_client_row(self, grid, row, key, label):
        grid.addWidget(QLabel(label), row, 0)
        status = QLabel()
        grid.addWidget(status, row, 1)
        actions = QHBoxLayout()
        connect = QPushButton("Connect / Repair")
        disconnect = QPushButton("Disconnect")
        if key == "codex":
            connect.clicked.connect(lambda: self._connect(self.codex))
            disconnect.clicked.connect(lambda: self._disconnect(self.codex))
        else:
            connect.clicked.connect(lambda: self._connect(self.claude))
            disconnect.clicked.connect(lambda: self._disconnect(self.claude))
        actions.addWidget(connect)
        actions.addWidget(disconnect)
        actions.addStretch(1)
        grid.addLayout(actions, row, 2)
        self._status_labels[key] = status
        self._connect_buttons[key] = connect
        self._disconnect_buttons[key] = disconnect

    def _prepare_runtime(self, plugin_dir):
        try:
            manager = RuntimeManager(
                plugin_dir=plugin_dir,
                qgis_prefix=QgsApplication.prefixPath(),
            )
            self.spec = manager.ensure()
            self.runtime_label.setText(
                "✓ Bundled launcher ready: {}".format(self.spec.launcher_path)
            )
            self._append("Bundled MCP launcher installed and verified.")
        except OnboardingError as exc:
            self.runtime_label.setText("✗ Runtime unavailable: {}".format(exc))
            self._append(str(exc))

    def _refresh(self):
        self._set_client_status("codex", self.codex)
        self._set_client_status("claude", self.claude)
        enabled = self.spec is not None
        for button in self._connect_buttons.values():
            button.setEnabled(enabled)

    def _set_client_status(self, key, connector):
        detected = connector.detected()
        status = connector.status() if detected else "not_detected"
        if status == "configured":
            text = "✓ Configured"
        elif detected:
            text = "Detected — not configured"
        else:
            text = "Not detected"
        self._status_labels[key].setText(text)
        self._disconnect_buttons[key].setEnabled(status == "configured")

    def _connect(self, connector):
        if self.spec is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            message = connector.install(self.spec)
            self._append(message)
            result = health_check(self.spec)
            healthy = (
                "QGIS session verified at revision {revision}; "
                "{layer_count} layer(s)."
            ).format(**result)
            self._append(healthy)
            QMessageBox.information(
                self,
                "QGIS Agent MCP",
                "{}\n\n{}".format(message, healthy),
            )
        except OnboardingError as exc:
            self._append("ERROR: {}".format(exc))
            QMessageBox.critical(self, "QGIS Agent MCP", str(exc))
        finally:
            QApplication.restoreOverrideCursor()
            self._refresh()

    def _disconnect(self, connector):
        answer = QMessageBox.question(
            self,
            "Disconnect QGIS Agent MCP",
            "Remove the QGIS MCP entry from {}?".format(connector.name),
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self._append(connector.remove())
        except OnboardingError as exc:
            self._append("ERROR: {}".format(exc))
            QMessageBox.critical(self, "QGIS Agent MCP", str(exc))
        self._refresh()

    def _copy_universal(self):
        if self.spec is None:
            return
        QApplication.clipboard().setText(universal_config(self.spec))
        self._append("Universal MCP configuration copied to the clipboard.")
        QMessageBox.information(
            self,
            "QGIS Agent MCP",
            "The universal MCP configuration was copied to the clipboard.",
        )

    def _test_connection(self):
        if self.spec is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = health_check(self.spec)
            message = (
                "Connection successful. QGIS revision {revision}; "
                "{layer_count} layer(s)."
            ).format(**result)
            self._append(message)
            QMessageBox.information(self, "QGIS Agent MCP", message)
        except OnboardingError as exc:
            self._append("ERROR: {}".format(exc))
            QMessageBox.critical(self, "QGIS Agent MCP", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _append(self, message):
        self.log.appendPlainText(str(message))
