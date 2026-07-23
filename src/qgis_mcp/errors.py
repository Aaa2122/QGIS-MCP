from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RpcError(Exception):
    code: int
    message: str
    data: Any = None

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            value["data"] = self.data
        return value


INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
BRIDGE_UNAVAILABLE = -32001
BRIDGE_ERROR = -32002

