from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
import traceback
from pathlib import Path

from qgis.core import Qgis
from qgis.PyQt.QtCore import QObject, QTimer
from qgis.PyQt.QtNetwork import QHostAddress, QTcpServer

from .bridge_scheduler import BridgeScheduler
from .dispatcher import MUTATION_METHODS, DispatchError

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
        self.scheduler = BridgeScheduler(
            MUTATION_METHODS,
            max_pending=int(os.environ.get("QGIS_MCP_MAX_PENDING", "50")),
            control_reserve=int(os.environ.get("QGIS_MCP_CONTROL_RESERVE", "5")),
            deadline_seconds=float(
                os.environ.get("QGIS_MCP_QUEUE_DEADLINE_SECONDS", "120")
            ),
        )
        self._drain_scheduled = False
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
        if not self.server.listen(
            QHostAddress(QHostAddress.SpecialAddress.LocalHost), requested_port
        ):
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
        self.scheduler.clear()
        self._drain_scheduled = False
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
        self.scheduler.discard_socket(socket)
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
            if not self.scheduler.enqueue(socket, request):
                request_id = request.get("id") if isinstance(request, dict) else None
                self._send_error(
                    socket,
                    request_id,
                    -32029,
                    "QGIS bridge is busy",
                    self.scheduler.snapshot(),
                )
                continue
            self._schedule_drain()

    def _schedule_drain(self):
        if self._drain_scheduled or not self.scheduler.pending:
            return
        self._drain_scheduled = True
        QTimer.singleShot(0, self._drain_one)

    def _drain_one(self):
        self._drain_scheduled = False
        item = self.scheduler.pop_next()
        if item is None:
            return
        queue_wait_ms = (time.monotonic() - item.enqueued_at) * 1000.0
        if item.socket not in self.clients:
            self.scheduler.record(queue_wait_ms)
        elif item.expired():
            request_id = (
                item.request.get("id") if isinstance(item.request, dict) else None
            )
            self._send_error(
                item.socket,
                request_id,
                -32028,
                "QGIS bridge request expired in the queue",
                {"queue_wait_ms": round(queue_wait_ms, 3)},
            )
            self.scheduler.record(queue_wait_ms, expired=True)
        else:
            self._process(item.socket, item.request, queue_wait_ms=queue_wait_ms)
            self.scheduler.record(queue_wait_ms)
        self._schedule_drain()

    def _process(self, socket, request, queue_wait_ms=0.0):
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
                    "python_execution_enabled": False,
                    "scheduler": self.scheduler.snapshot(),
                },
            )
            return
        if method == "bridge.hello":
            self._send_error(socket, request_id, -32600, "Already authenticated")
            return
        if method == "bridge.cancel":
            target_id = params.get("request_id") if isinstance(params, dict) else None
            cancelled = self.scheduler.cancel_request(socket, target_id)
            self._send_result(
                socket,
                request_id,
                {"request_id": target_id, "cancelled": cancelled},
            )
            return
        started = time.perf_counter()
        success = False
        error_code = None
        response_bytes = 0
        serialization_ms = 0.0
        try:
            result = self.dispatcher.dispatch(method, params)
            qgis_main_thread_ms = (time.perf_counter() - started) * 1000.0
            serialization_started = time.perf_counter()
            response_bytes = self._send_result(
                socket,
                request_id,
                result,
                serialized_result=self.dispatcher.take_serialized_result(result),
            )
            serialization_ms = (time.perf_counter() - serialization_started) * 1000.0
            success = True
        except DispatchError as exc:
            qgis_main_thread_ms = (time.perf_counter() - started) * 1000.0
            error_code = exc.code
            serialization_started = time.perf_counter()
            response_bytes = self._send_error(
                socket, request_id, exc.code, exc.message, exc.data
            )
            serialization_ms = (time.perf_counter() - serialization_started) * 1000.0
        except Exception as exc:
            qgis_main_thread_ms = (time.perf_counter() - started) * 1000.0
            error_code = -32603
            stack = traceback.format_exc()
            self.dispatcher.log.add("bridge.error", str(exc), "error", {"traceback": stack})
            serialization_started = time.perf_counter()
            response_bytes = self._send_error(
                socket,
                request_id,
                -32603,
                "Internal QGIS bridge error",
                {"cause": str(exc), "traceback": stack},
            )
            serialization_ms = (time.perf_counter() - serialization_started) * 1000.0
        self.dispatcher.log.add(
            "bridge.metric",
            "Completed {}".format(method),
            "info" if success else "warning",
            {
                "request_id": request_id,
                "tool_name": method,
                "queue_wait_ms": round(queue_wait_ms, 3),
                "qgis_main_thread_ms": round(qgis_main_thread_ms, 3),
                "serialization_ms": round(serialization_ms, 3),
                "response_bytes": response_bytes,
                "project_revision": self.dispatcher.state.revision,
                "success": success,
                "error_code": error_code,
                "queue_depth": self.scheduler.pending,
            },
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
        if change["event"] in {
            "layer.name",
            "layers.added",
            "layers.removed",
            "operation.started",
            "project.cleared",
            "project.read",
        }:
            self.broadcast({"type": "resources.changed", "revision": change["revision"]})

    def broadcast(self, event):
        message = {"jsonrpc": "2.0", "method": "bridge.event", "params": event}
        for socket, state in list(self.clients.items()):
            if state["authenticated"]:
                self._write(socket, message)

    def _send_result(self, socket, request_id, result, serialized_result=None):
        if request_id is not None:
            if serialized_result is not None:
                encoded = (
                    b'{"jsonrpc":"2.0","id":'
                    + json.dumps(request_id, ensure_ascii=False).encode("utf-8")
                    + b',"result":'
                    + serialized_result
                    + b"}\n"
                )
                socket.write(encoded)
                socket.flush()
                return len(encoded)
            return self._write(
                socket, {"jsonrpc": "2.0", "id": request_id, "result": result}
            )
        return 0

    def _send_error(self, socket, request_id, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return self._write(
            socket, {"jsonrpc": "2.0", "id": request_id, "error": error}
        )

    @staticmethod
    def _write(socket, message):
        encoded = (
            json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
            .encode("utf-8")
            + b"\n"
        )
        socket.write(encoded)
        socket.flush()
        return len(encoded)

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
