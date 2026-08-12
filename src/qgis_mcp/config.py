from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONNECTION_FILE = Path.home() / ".qgis-mcp" / "connection.json"


class ConnectionInfoError(RuntimeError):
    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ConnectionInfo:
    host: str
    port: int
    token: str
    protocol: int = 1
    pid: int | None = None
    qgis_version: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConnectionInfo":
        host = str(value.get("host", "127.0.0.1"))
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("QGIS bridge must use a loopback host")
        port = int(value["port"])
        if not 1 <= port <= 65535:
            raise ValueError("Invalid QGIS bridge port")
        token = str(value["token"])
        if len(token) < 32:
            raise ValueError("Invalid QGIS bridge token")
        return cls(
            host=host,
            port=port,
            token=token,
            protocol=int(value.get("protocol", 1)),
            pid=int(value["pid"]) if value.get("pid") is not None else None,
            qgis_version=value.get("qgis_version"),
        )


def connection_file() -> Path:
    configured = os.environ.get("QGIS_MCP_CONNECTION_FILE")
    return Path(configured).expanduser() if configured else DEFAULT_CONNECTION_FILE


def load_connection_info(path: Path | None = None) -> ConnectionInfo:
    target = path or connection_file()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConnectionInfoError(
            f"QGIS Agent MCP bridge registration was not found at {target}. "
            "If QGIS is open, QGIS Agent MCP is missing from the active profile, "
            "disabled, or not started; open Plugins > Manage and Install Plugins, "
            "then start QGIS Agent MCP. If the plugin is already active, "
            "use Connect / Repair and verify that the MCP client launches "
            "~/.qgis-mcp/bin/qgis_mcp_launcher.py rather than a legacy qgis-mcp "
            "server. Start QGIS if it is currently closed.",
            reason="registration_missing",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectionInfoError(
            f"QGIS Agent MCP bridge registration at {target} is unreadable: {exc}. "
            "Restart the plugin to recreate it.",
            reason="registration_unreadable",
        ) from exc
    try:
        return ConnectionInfo.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectionInfoError(
            f"QGIS Agent MCP bridge registration at {target} is invalid: {exc}. "
            "Restart the plugin to recreate it.",
            reason="registration_invalid",
        ) from exc

