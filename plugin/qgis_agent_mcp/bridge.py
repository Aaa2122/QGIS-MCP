from __future__ import annotations

import json
import os
import secrets
import tempfile
import traceback
from pathlib import Path

from qgis.core import Qgis
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer

from .dispatcher import DispatchError

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
PROTOCOL_VERSION = 1


class LocalBridge(QObject):
    def __init__(self, dispatcher, parent=None):
        super().__init__(parent)
        self.dispatcher = dispatcher
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._accept)
        self.clients = {}
        self.token = secrets.token_urlsafe(48)
        self.connection_path = Path(
            os.environ.get(
                "QGIS_MCP_CONNECTION_FILE",
                str(Path.home() / ".qgis-mcp" / "connection.json"),
            )
        ).expanduser()
        dispatcher.state.changed.connect(self._state_changed)

    @property
    def port(self):
        return int(self.server.serverPort()) if self.server.isListening() else None

    def start(self):
        requested_port = int(os.environ.get("QGIS_MCP_PORT", "0"))
        if requested_port < 0 or requested_port > 65535:
            raise ValueError("QGIS_MCP_PORT must be between 0 and 65535")
        if not self.server.listen(QHostAddress.LocalHost, requested_port):
            raise RuntimeError("Could not start QGIS MCP bridge: " + self.server.errorString())
        self._write_connection_file()
        self.dispatcher.log.add(
            "bridge",
            "Listening on 127.0.0.1:{}".format(self.port),
            data={"protocol": PROTOCOL_VERSION},
        )

    def stop(self):
        self._remove_connection_file()
        for socket in list(self.clients):
            socket.disconnectFromHost()
            socket.deleteLater()
        self.clients.clear()
        self.server.close()

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            peer = socket.peerAddress()
            if not peer.isLoopback():
                socket.disconnectFromHost()
                socket.deleteLater()
                continue
            self.clients[socket] = {"buffer": bytearray(), "authenticated": False}
            socket.readyRead.connect(lambda _socket=socket: self._read(_socket))
            socket.disconnected.connect(lambda _socket=socket: self._disconnected(_socket))

    def _disconnected(self, socket):
        self.clients.pop(socket, None)
        socket.deleteLater()

    def _read(self, socket):
        state = self.clients.get(socket)
        if state is None:
            return
        state["buffer"].extend(bytes(socket.readAll()))
        if len(state["buffer"]) > MAX_MESSAGE_BYTES:
            self._send_error(socket, None, -32030, "Bridge message exceeds 4 MiB")
            socket.disconnectFromHost()
            return
        while b"\n" in state["buffer"]:
            line, _, remaining = state["buffer"].partition(b"\n")
            state["buffer"] = bytearray(remaining)
            if not line.strip():
                continue
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_error(socket, None, -32700, "Parse error", str(exc))
                continue
            QTimer.singleShot(
                0, lambda _socket=socket, _request=request: self._process(_socket, _request)
            )

    def _process(self, socket, request):
        state = self.clients.get(socket)
        if state is None:
            return
        request_id = request.get("id") if isinstance(request, dict) else None
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            self._send_error(socket, request_id, -32600, "Invalid request")
            return
        method = request.get("method")
        params = request.get("params") or {}
        if not state["authenticated"]:
            if (
                method != "bridge.hello"
                or not isinstance(params, dict)
                or not secrets.compare_digest(str(params.get("token", "")), self.token)
                or int(params.get("protocol", -1)) != PROTOCOL_VERSION
            ):
                self._send_error(socket, request_id, -32031, "Authentication failed")
                socket.disconnectFromHost()
                return
            state["authenticated"] = True
            self._send_result(
                socket,
                request_id,
                {
                    "authenticated": True,
                    "protocol": PROTOCOL_VERSION,
                    "qgis_version": Qgis.QGIS_VERSION,
                    "python_execution_enabled": self.dispatcher.python_enabled,
                },
            )
            return
        if method == "bridge.hello":
            self._send_error(socket, request_id, -32600, "Already authenticated")
            return
        try:
            result = self.dispatcher.dispatch(method, params)
            self._send_result(socket, request_id, result)
        except DispatchError as exc:
            self._send_error(socket, request_id, exc.code, exc.message, exc.data)
        except Exception as exc:
            stack = traceback.format_exc()
            self.dispatcher.log.add("bridge.error", str(exc), "error", {"traceback": stack})
            self._send_error(
                socket,
                request_id,
                -32603,
                "Internal QGIS bridge error",
                {"cause": str(exc), "traceback": stack},
            )

    def _state_changed(self, change):
        for uri, revision in change.get("resources", {"qgis://session": change["revision"]}).items():
            self.broadcast(
                {
                    "type": "resource.updated",
                    "uri": uri,
                    "revision": revision,
                    "change": change,
                }
            )
        if change["event"].startswith(
            ("layer.", "layers.", "operation.started", "project.cleared", "project.read")
        ):
            self.broadcast({"type": "resources.changed", "revision": change["revision"]})

    def broadcast(self, event):
        message = {"jsonrpc": "2.0", "method": "bridge.event", "params": event}
        for socket, state in list(self.clients.items()):
            if state["authenticated"]:
                self._write(socket, message)

    def _send_result(self, socket, request_id, result):
        if request_id is not None:
            self._write(socket, {"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_error(self, socket, request_id, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write(socket, {"jsonrpc": "2.0", "id": request_id, "error": error})

    @staticmethod
    def _write(socket, message):
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
            .encode("utf-8")
            + b"\n"
        )
        socket.write(encoded)
        socket.flush()

    def _write_connection_file(self):
        self.connection_path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "host": "127.0.0.1",
            "port": self.port,
            "token": self.token,
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "qgis_version": Qgis.QGIS_VERSION,
        }
        fd, temporary = tempfile.mkstemp(
            prefix="connection-", suffix=".json", dir=str(self.connection_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.connection_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _remove_connection_file(self):
        try:
            value = json.loads(self.connection_path.read_text(encoding="utf-8"))
            if value.get("pid") == os.getpid() and value.get("port") == self.port:
                self.connection_path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
