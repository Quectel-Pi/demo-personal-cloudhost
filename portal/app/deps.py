"""FastAPI 依赖：会话读取、CSRF 校验、来源限制（设计文档 5.4 / 9.3 / 11.1）。"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any

from fastapi import Request

from .config import Config
from .errors import ApiError
from .ingress import cookie_names, public_ingress
from .security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, Session, SessionStore

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


def current_session(request: Request) -> Session:
    """要求已登录；否则抛 E1004。"""

    store: SessionStore = session_store(request)
    session = optional_session(request)
    if session is None:
        raise ApiError(
            "E1004",
            detail="cookie not present or invalid",
            headers={"Cache-Control": "no-store"},
        )
    return session


def optional_session(request: Request) -> Session | None:
    store: SessionStore = session_store(request)
    session = store.get(request.cookies.get(cookie_names(request)[0]))
    if session is not None and session.extra.get("ingress", "lan") != (
        "public" if public_ingress(request) else "lan"
    ):
        return None
    return session


def is_remote_admin(session: Session, config: Config) -> bool:
    """Check membership only against the root-managed remote-admin allowlist."""

    return session.user in config.remote_admin_users


def require_csrf(request: Request, session: Session) -> None:
    """校验 CSRF token：优先请求头，其次表单字段。

    约定 token 必须与会话绑定；同时用 ``hmac.compare_digest`` 做定时安全比较。
    """

    token = request.headers.get(CSRF_HEADER) or request.headers.get("X-CSRFToken") or ""
    if not token:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/x-www-form-urlencoded"):
            # 表单场景由调用方预先解析后以请求头复用，这里不再读取 body，
            # 避免与 FastAPI 的 body 解析冲突。
            token = request.query_params.get("_csrf", "")
    if not token or not hmac.compare_digest(token, session.csrf):
        raise ApiError("E1003", detail="csrf token missing or invalid")


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host in _LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_loopback(request: Request) -> None:
    """``/auth/verify`` 只接受来自 Caddy（本机）的调用，见文档 9.3。"""

    client = request.client.host if request.client else None
    if not is_loopback(client):
        raise ApiError("E1003", detail="auth verify is restricted to loopback")


def client_ip(request: Request) -> str:
    """取真实客户端 IP：Caddy 反代会写入 ``X-Forwarded-For``。"""

    if public_ingress(request):
        # The relay's original client IP is unverified; never ban the tunnel.
        return "peanut-relay"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "-"


def app_state(request: Request) -> Any:
    return request.app.state
