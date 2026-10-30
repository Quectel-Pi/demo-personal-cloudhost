"""Bounded local transport; portal never invokes systemctl for Peanut."""

import json
import socket

from .errors import ApiError
from .peanut_control import SOCKET_PATH


class PeanutClient:
    def call(self, action: str, *, actor: str = "", sid: str = "", body: dict | None = None) -> dict:
        request = dict(action=action, actor=actor, sid=sid, body=body or {})
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(20)
                connection.connect(SOCKET_PATH)
                connection.sendall(json.dumps(request).encode() + b"\n")
                with connection.makefile("rb") as stream:
                    raw = stream.readline(1500001)
            if len(raw) > 1500000:
                raise ValueError("response too large")
            response = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ApiError("E5002", detail="controller_unavailable") from exc
        if "error" in response:
            error = response["error"]
            raise ApiError(error["code"], detail=error["reason"])
        if not isinstance(response.get("data"), dict):
            raise ApiError("E5002", detail="controller_protocol_error")
        return response["data"]
