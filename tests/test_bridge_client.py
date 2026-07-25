from __future__ import annotations

import asyncio
import json

import pytest

from qgis_mcp.bridge import BridgeClient
from qgis_mcp.config import ConnectionInfo
from qgis_mcp.errors import RpcError


@pytest.mark.asyncio
async def test_bridge_auth_multiplexing_error_and_event():
    token = "t" * 64
    events = []

    async def handler(reader, writer):
        hello = json.loads(await reader.readline())
        assert hello["method"] == "bridge.hello"
        assert hello["params"]["token"] == token
        writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": hello["id"],
                    "result": {"authenticated": True},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        requests = [json.loads(await reader.readline()) for _ in range(2)]
        writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "bridge.event",
                    "params": {"type": "test", "value": 7},
                }
            ).encode()
            + b"\n"
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
            writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
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
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_bridge_reloads_connection_and_retries_after_qgis_restart(monkeypatch):
    token_one = "a" * 64
    token_two = "b" * 64
    current = {}

    async def healthy(reader, writer):
        hello = json.loads(await reader.readline())
        writer.write(
            json.dumps({"jsonrpc": "2.0", "id": hello["id"], "result": {"authenticated": True}}).encode()
            + b"\n"
        )
        await writer.drain()
        request = json.loads(await reader.readline())
        writer.write(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"qgis": "restarted"}}).encode()
            + b"\n"
        )
        await writer.drain()

    second = await asyncio.start_server(healthy, "127.0.0.1", 0)
    second_port = second.sockets[0].getsockname()[1]

    async def failing(reader, writer):
        hello = json.loads(await reader.readline())
        writer.write(
            json.dumps({"jsonrpc": "2.0", "id": hello["id"], "result": {"authenticated": True}}).encode()
            + b"\n"
        )
        await writer.drain()
        await reader.readline()
        current["info"] = ConnectionInfo("127.0.0.1", second_port, token_two)
        writer.close()
        await writer.wait_closed()

    first = await asyncio.start_server(failing, "127.0.0.1", 0)
    first_port = first.sockets[0].getsockname()[1]
    current["info"] = ConnectionInfo("127.0.0.1", first_port, token_one)
    monkeypatch.setattr("qgis_mcp.bridge.load_connection_info", lambda: current["info"])
    client = BridgeClient()
    try:
        assert await client.request("session.snapshot") == {"qgis": "restarted"}
    finally:
        await client.close()
        first.close()
        second.close()
        await first.wait_closed()
        await second.wait_closed()
