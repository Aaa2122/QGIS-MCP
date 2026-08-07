from __future__ import annotations

from pathlib import Path

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QEventLoop, Qt
from qgis.PyQt.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .onboarding import (
    AntigravityConnector,
    ClaudeCodeConnector,
    CodexConnector,
    CursorConnector,
    OnboardingError,
    OpenCodeConnector,
    RuntimeManager,
    health_check,
    universal_config,
)


class ConnectAiDialog(QDialog):
    def __init__(self, plugin_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect an AI client")
        self.setMinimumSize(600, 390)
        self.resize(650, 410)
        self.spec = None
        self.clients = {
            "opencode": OpenCodeConnector(),
            "codex": CodexConnector(),
            "claude": ClaudeCodeConnector(),
            "cursor": CursorConnector(),
            "antigravity": AntigravityConnector(),
        }
        self._selected_key = "opencode"
        self._status_labels = {}
        self._connect_buttons = {}
        self._disconnect_buttons = {}
        self._clients_collapsed_height = None
        self._build_ui()
        self._prepare_runtime(Path(plugin_dir))
        self._refresh()

    def _build_ui(self):
        self.setStyleSheet(
            """
            QFrame#hero {
                background: palette(alternate-base);
                border: 1px solid palette(mid);
                border-radius: 9px;
            }
            QLabel#title { font-size: 18px; font-weight: 700; }
            QLabel#clientName { font-size: 14px; font-weight: 650; }
            QLabel#muted { color: palette(mid); }
            QLabel#success {
                color: #18794e;
                background: rgba(24, 121, 78, 0.10);
                border: 1px solid rgba(24, 121, 78, 0.35);
                border-radius: 7px;
                padding: 4px 8px;
            }
            QLabel#warning {
                color: #9a6700;
                background: rgba(154, 103, 0, 0.10);
                border: 1px solid rgba(154, 103, 0, 0.35);
                border-radius: 7px;
                padding: 4px 8px;
            }
            QLabel#error {
                color: #c93c37;
                background: rgba(201, 60, 55, 0.10);
                border: 1px solid rgba(201, 60, 55, 0.35);
                border-radius: 7px;
                padding: 4px 8px;
            }
            QPushButton {
                min-height: 25px;
                padding: 1px 8px;
            }
            QPushButton#primary {
                font-weight: 650;
                min-width: 112px;
            }
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        hero = QFrame()
        hero.setObjectName("hero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(13, 9, 13, 9)
        title = QLabel("Connect QGIS to your AI client")
        title.setObjectName("title")
        hero_layout.addWidget(title)
        intro = QLabel(
            "Select a client. QGIS updates its user configuration without exposing "
            "a port or token."
        )
        hero_layout.addWidget(intro)
        self.runtime_label = QLabel("Preparing the secure local launcher…")
        self.runtime_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        hero_layout.addWidget(self.runtime_label)
        root.addWidget(hero)

        clients = QFrame()
        clients.setObjectName("hero")
        client_layout = QVBoxLayout(clients)
        client_layout.setContentsMargins(10, 8, 10, 8)
        visible_grid = QGridLayout()
        visible_grid.setHorizontalSpacing(10)
        visible_grid.setVerticalSpacing(5)
        visible_grid.addWidget(QLabel("<b>AI client</b>"), 0, 0)
        visible_grid.addWidget(QLabel("<b>Status</b>"), 0, 1)
        visible_grid.addWidget(QLabel("<b>Actions</b>"), 0, 2)
        for row, key in enumerate(("opencode", "codex", "claude"), start=1):
            self._add_client_row(visible_grid, row, key)
        visible_grid.setColumnStretch(0, 1)
        visible_grid.setColumnStretch(2, 2)
        client_layout.addLayout(visible_grid)

        self.extra_clients = QFrame()
        extra_grid = QGridLayout(self.extra_clients)
        extra_grid.setContentsMargins(0, 2, 0, 0)
        extra_grid.setHorizontalSpacing(10)
        extra_grid.setVerticalSpacing(5)
        for row, key in enumerate(("cursor", "antigravity")):
            self._add_client_row(extra_grid, row, key)
        extra_grid.setColumnStretch(0, 1)
        extra_grid.setColumnStretch(2, 2)
        self.extra_clients.setVisible(False)
        client_layout.addWidget(self.extra_clients)

        self.more_clients_button = QPushButton("▾  Show 2 more clients")
        self.more_clients_button.setFlat(True)
        self.more_clients_button.setCheckable(True)
        self.more_clients_button.setStyleSheet("text-align: left; padding-left: 2px;")
        self.more_clients_button.toggled.connect(self._toggle_more_clients)
        client_layout.addWidget(self.more_clients_button)
        root.addWidget(clients)

        self.guidance_label = QLabel(
            "Next: connect, then fully restart that AI client. QGIS can stay open."
        )
        self.guidance_label.setWordWrap(True)
        root.addWidget(self.guidance_label)

        utility_layout = QHBoxLayout()
        test_button = QPushButton("Test QGIS bridge")
        test_button.clicked.connect(self._test_connection)
        utility_layout.addWidget(test_button)
        self.copy_button = QPushButton("Copy OpenCode config")
        self.copy_button.clicked.connect(self._copy_universal)
        utility_layout.addWidget(self.copy_button)
        self.details_button = QPushButton("Show details")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self._toggle_details)
        utility_layout.addWidget(self.details_button)
        utility_layout.addStretch(1)
        root.addLayout(utility_layout)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(100)
        self.log.setMaximumBlockCount(200)
        self.log.setPlaceholderText("Setup details and recovery guidance appear here.")
        self.log.setVisible(False)
        root.addWidget(self.log)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_client_row(self, grid, row, key):
        connector = self.clients[key]
        name = QLabel(connector.name)
        name.setObjectName("clientName")
        grid.addWidget(name, row, 0)

        status = QLabel("Checking…")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setMinimumWidth(112)
        grid.addWidget(status, row, 1)

        actions = QHBoxLayout()
        actions.setSpacing(5)
        connect = QPushButton("Connect")
        connect.setObjectName("primary")
        connect.clicked.connect(lambda _checked=False, k=key: self._connect_client(k))
        actions.addWidget(connect)
        help_button = QPushButton("Help")
        help_button.clicked.connect(lambda _checked=False, k=key: self._help_client(k))
        actions.addWidget(help_button)
        disconnect = QPushButton("Disconnect")
        disconnect.clicked.connect(
            lambda _checked=False, k=key: self._disconnect_client(k)
        )
        actions.addWidget(disconnect)
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
            self.spec = manager.ensure(event_pump=self._process_bridge_events)
            self.runtime_label.setObjectName("success")
            self.runtime_label.setText("✓ Secure local launcher ready")
            self.runtime_label.setToolTip(self.spec.launcher_path)
            self._append("Bundled MCP launcher installed and verified.")
        except OnboardingError as exc:
            self.runtime_label.setObjectName("error")
            self.runtime_label.setText("Launcher unavailable — open details")
            self.runtime_label.setToolTip(str(exc))
            self._append("ERROR: {}".format(exc))
        self.runtime_label.style().unpolish(self.runtime_label)
        self.runtime_label.style().polish(self.runtime_label)

    def _refresh(self):
        for key, connector in self.clients.items():
            self._set_client_status(key, connector)

    def _set_client_status(self, key, connector):
        try:
            detected = connector.detected()
            status = connector.status()
        except OnboardingError as exc:
            detected = False
            status = "error"
            self._append("ERROR reading {}: {}".format(connector.name, exc))

        if status == "configured":
            text, style = "✓ Connected", "success"
        elif status == "conflict":
            text, style = "Needs attention", "error"
        elif status == "error":
            text, style = "Config unreadable", "error"
        elif detected or not connector.requires_executable:
            text, style = "Ready to connect", "warning"
        else:
            text, style = "Not detected", "warning"
        status_label = self._status_labels[key]
        status_label.setText(text)
        status_label.setObjectName(style)
        status_label.style().unpolish(status_label)
        status_label.style().polish(status_label)

        location = getattr(connector, "config_path", None) or getattr(
            connector, "executable", None
        )
        tooltip = connector.description
        if location:
            tooltip += "\n{}".format(location)
        status_label.setToolTip(tooltip)
        self._connect_buttons[key].setToolTip(tooltip)

        can_connect = self.spec is not None and (
            not connector.requires_executable or detected
        )
        self._connect_buttons[key].setEnabled(can_connect)
        self._disconnect_buttons[key].setEnabled(status == "configured")

    def _selected_connector(self):
        return self.clients[self._selected_key]

    def _select_client(self, key):
        self._selected_key = key
        self.copy_button.setText(
            "Copy {} config".format(self.clients[key].name)
        )

    def _connect_client(self, key):
        self._select_client(key)
        self._connect(self.clients[key])

    def _help_client(self, key):
        self._select_client(key)
        self._show_client_help(self.clients[key])

    def _disconnect_client(self, key):
        self._select_client(key)
        self._disconnect(self.clients[key])

    def _toggle_more_clients(self, visible):
        if visible:
            self._clients_collapsed_height = self.height()
        self.extra_clients.setVisible(bool(visible))
        self.more_clients_button.setText(
            "▴  Show fewer clients" if visible else "▾  Show 2 more clients"
        )
        self.layout().activate()
        target_height = self.sizeHint().height()
        if not visible and self._clients_collapsed_height is not None:
            target_height = self._clients_collapsed_height
            self._clients_collapsed_height = None
        self.resize(self.width(), target_height)

    def _show_client_help(self, connector):
        message = connector.manual_help()
        if connector is self.clients["claude"]:
            answer = QMessageBox.question(
                self,
                "Claude Code setup help",
                message + "\n\nDo you want to locate Claude Code now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
                if not connector.executable
                else QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._locate_claude()
            return
        QMessageBox.information(
            self,
            "{} setup help".format(connector.name),
            message,
        )

    def _toggle_details(self, visible):
        self.log.setVisible(bool(visible))
        self.details_button.setText("Hide details" if visible else "Show details")
        self.layout().activate()
        self.resize(self.width(), self.sizeHint().height())

    def _locate_claude(self):
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Locate the Claude Code executable",
            str(Path.home()),
            "Executables (*.exe *.cmd *.bat);;All files (*)",
        )
        if not filename:
            return
        try:
            self.clients["claude"].set_executable(filename)
            self._append("Claude Code found at {}.".format(filename))
        except OnboardingError as exc:
            self._show_error("Claude Code was not found", exc)
        self._refresh()

    def _connect(self, connector):
        if self.spec is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            message = connector.install(self.spec)
            self._append(message)
            result = health_check(self.spec, event_pump=self._process_bridge_events)
            healthy = (
                "QGIS bridge verified at revision {revision}; "
                "{layer_count} layer(s)."
            ).format(**result)
            self._append(healthy)
            self.guidance_label.setText(connector.restart_hint)
            QMessageBox.information(
                self,
                "{} is connected".format(connector.name),
                "{}\n\nNext step:\n{}\n\n{}".format(
                    message, connector.restart_hint, healthy
                ),
            )
        except OnboardingError as exc:
            self._show_error("{} could not be connected".format(connector.name), exc)
        except Exception as exc:
            self._show_error(
                "{} could not be connected".format(connector.name),
                OnboardingError(
                    "An unexpected setup error occurred. No existing configuration "
                    "was intentionally removed: {}".format(exc)
                ),
            )
        finally:
            QApplication.restoreOverrideCursor()
            self._refresh()

    def _disconnect(self, connector):
        answer = QMessageBox.question(
            self,
            "Disconnect {}".format(connector.name),
            "Remove only the QGIS MCP entry managed by this plugin from {}?".format(
                connector.name
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._append(connector.remove())
        except OnboardingError as exc:
            self._show_error("{} could not be updated".format(connector.name), exc)
        self._refresh()

    def _copy_universal(self):
        if self.spec is None:
            return
        connector = self._selected_connector()
        if isinstance(connector, OpenCodeConnector):
            configuration = connector.manual_config(self.spec)
        else:
            configuration = universal_config(self.spec)
        QApplication.clipboard().setText(configuration)
        self._append(
            "Manual MCP configuration for {} copied to the clipboard.".format(
                connector.name
            )
        )
        QMessageBox.information(
            self,
            "Configuration copied",
            "Paste the qgis entry into {} user-level MCP settings, then fully "
            "restart that client.".format(connector.name),
        )

    def _test_connection(self):
        if self.spec is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = health_check(self.spec, event_pump=self._process_bridge_events)
            message = (
                "Connection successful. QGIS revision {revision}; "
                "{layer_count} layer(s)."
            ).format(**result)
            self._append(message)
            QMessageBox.information(self, "QGIS bridge is ready", message)
        except OnboardingError as exc:
            self._show_error("QGIS bridge test failed", exc)
        finally:
            QApplication.restoreOverrideCursor()

    def _show_error(self, title, error):
        self._append("ERROR: {}".format(error))
        self.details_button.setChecked(True)
        QMessageBox.critical(
            self,
            title,
            "{}\n\nYour existing configuration was left unchanged whenever a "
            "safe update could not be guaranteed.".format(error),
        )

    def _append(self, message):
        self.log.appendPlainText(str(message))

    @staticmethod
    def _process_bridge_events():
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 25
        )
