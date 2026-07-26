from __future__ import annotations

import asyncio
import json
import socketserver
import threading

import pytest

from qgis_mcp.bridge import BridgeClient
from qgis_mcp.config import ConnectionInfo
from qgis_mcp.errors import RpcError


class _BridgeTestServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _BridgeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.server.callback(self.rfile, self.wfile)


def _start_bridge(callback):
    server = _BridgeTestServer(("127.0.0.1", 0), _BridgeRequestHandler)
    server.callback = callback
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _write(stream, message):
    stream.write(json.dumps(message).encode() + b"\n")
    stream.flush()


@pytest.mark.asyncio
async def test_bridge_auth_multiplexing_error_and_event():
    token = "t" * 64
    events = []

    def handler(reader, writer):
        hello = json.loads(reader.readline())
        assert hello["method"] == "bridge.hello"
        assert hello["params"]["token"] == token
        _write(
            writer,
            {
                "jsonrpc": "2.0",
                "id": hello["id"],
                "result": {"authenticated": True},
            },
        )
        requests = [json.loads(reader.readline()) for _ in range(2)]
        _write(
            writer,
            {
                "jsonrpc": "2.0",
                "method": "bridge.event",
                "params": {"type": "test", "value": 7},
            },
        )
        for request in reversed(requests):
            if request["method"] == "fail":
                response = {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32099, "message": "expected"},
                }
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"method": request["method"]},
                }
            _write(writer, response)

    server, port = _start_bridge(handler)
    client = BridgeClient(ConnectionInfo("127.0.0.1", port, token))
    client.add_event_handler(lambda event: events.append(event))
    try:
        good, bad = await asyncio.gather(
            client.request("ok"),
            client.request("fail"),
            return_exceptions=True,
        )
        assert good == {"method": "ok"}
        assert isinstance(bad, RpcError)
        assert bad.code == -32099
        await asyncio.sleep(0)
        assert events == [{"type": "test", "value": 7}]
    finally:
        await client.close()
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_bridge_reloads_connection_and_retries_after_qgis_restart(monkeypatch):
    token_one = "a" * 64
    token_two = "b" * 64
    current = {}

    def healthy(reader, writer):
        hello = json.loads(reader.readline())
        _write(
            writer,
            {"jsonrpc": "2.0", "id": hello["id"], "result": {"authenticated": True}},
        )
        request = json.loads(reader.readline())
        _write(
            writer,
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"qgis": "restarted"},
            },
        )

    second, second_port = _start_bridge(healthy)

    def failing(reader, writer):
        hello = json.loads(reader.readline())
        _write(
            writer,
            {"jsonrpc": "2.0", "id": hello["id"], "result": {"authenticated": True}},
        )
        reader.readline()
        current["info"] = ConnectionInfo("127.0.0.1", second_port, token_two)

    first, first_port = _start_bridge(failing)
    current["info"] = ConnectionInfo("127.0.0.1", first_port, token_one)
    monkeypatch.setattr("qgis_mcp.bridge.load_connection_info", lambda: current["info"])
    client = BridgeClient()
    try:
        assert await client.request("session.snapshot") == {"qgis": "restarted"}
    finally:
        await client.close()
        first.shutdown()
        second.shutdown()
        first.server_close()
        second.server_close()
