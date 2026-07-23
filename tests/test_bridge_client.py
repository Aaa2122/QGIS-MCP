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

