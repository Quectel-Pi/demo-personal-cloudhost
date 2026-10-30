"""统一错误响应（设计文档 9.1 / 9.4）。"""

from __future__ import annotations

from typing import Any

# 设计文档 9.4 已定义的错误码
HTTP_STATUS: dict[str, int] = {
    "E5001": 400,
    "E5002": 502,
    "E5003": 504,
    "E5004": 409,
    "E5005": 409,
    "E1001": 400,
    "E1002": 401,
    "E1003": 403,
    "E1004": 401,
    "E1005": 429,
    "E1006": 500,
    "E1007": 502,
    "E2001": 500,
    "E2002": 502,
    "E3001": 500,
    "E4001": 500,
    # 实现补充（文档 9.4 未覆盖）：E9000 内部错误、E9001 资源不存在
    "E9000": 500,
    "E9001": 404,
}

MESSAGES: dict[str, str] = {
    "E5001": "invalid peanut configuration",
    "E5002": "peanut operation failed",
    "E5003": "peanut operation timed out; check state before retrying",
    "E5004": "official capability not verified",
    "E5005": "remote revision or operation conflict",
    "E1001": "invalid request parameter",
    "E1002": "authentication required",
    "E1003": "permission denied",
    "E1004": "session expired",
    "E1005": "too many login attempts",
    "E1006": "status collection failed",
    "E1007": "downstream service unreachable",
    "E2001": "caddy configuration invalid",
    "E2002": "caddy cannot reach upstream",
    "E3001": "file browser failed to start",
    "E4001": "zerotier network not joined",
    "E9000": "internal error",
    "E9001": "resource not found",
}


class ApiError(Exception):
    """携带错误码的 API 异常，由全局异常处理器转换为统一 JSON。"""

    def __init__(
        self,
        code: str,
        detail: str | None = None,
        *,
        message: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code or HTTP_STATUS.get(code, 500)
        self.message = message or MESSAGES.get(code, "unexpected error")
        self.detail = detail
        self.headers = dict(headers or {})
        super().__init__(f"{code}: {self.message}")

    def payload(self) -> dict[str, Any]:
        return error_payload(self.code, self.detail, self.message)


def error_payload(
    code: str, detail: str | None = None, message: str | None = None
) -> dict[str, Any]:
    """构造文档 9.1 约定的错误响应体。"""

    error: dict[str, Any] = {"code": code, "message": message or MESSAGES.get(code, "unexpected error")}
    if detail:
        error["detail"] = detail
    return {"error": error}
