"""鉴权接口（设计文档 9.3）。

| 方法 | 路径            | 说明                                  |
|------|-----------------|---------------------------------------|
| POST | /auth/login     | 表单 username + password，限流生效     |
| GET  | /auth/verify    | 供 Caddy forward_auth 调用            |
| POST | /auth/logout    | 清除会话                              |
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Form, Request, Response
from starlette.concurrency import run_in_threadpool

from . import audit as audit_events
from .config import Config
from .deps import client_ip, optional_session, require_csrf, require_loopback
from .errors import ApiError
from .ingress import cookie_names, public_ingress
from .security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, SessionStore

router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)


@router.get("/auth/ingress")
async def ingress_check(request: Request) -> Response:
    require_loopback(request)
    if not public_ingress(request):
        raise ApiError("E1003", detail="public_gateway_required")
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


def _cookie_secure(config: Config) -> bool:
    """v1.0 只提供 HTTP，Cookie 必须关闭 Secure（文档第 6 章）。"""

    return config.tls_mode != "off"


def _set_session_cookies(
    response: Response, config: Config, sid: str, csrf: str, request: Request | None = None
) -> None:
    secure = (request is not None and public_ingress(request)) or _cookie_secure(config)
    names = cookie_names(request) if request is not None else (SESSION_COOKIE, CSRF_COOKIE)
    max_age = config.session_ttl
    response.set_cookie(
        names[0],
        sid,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    # CSRF token 需要被前端 JS 读取，因此不设 HttpOnly；它本身不能当作凭据使用
    response.set_cookie(
        names[1],
        csrf,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _clear_session_cookies(response: Response, config: Config, request: Request) -> None:
    secure = public_ingress(request) or _cookie_secure(config)
    names = cookie_names(request)
    response.delete_cookie(names[0], path="/", httponly=True, samesite="lax", secure=secure)
    response.delete_cookie(names[1], path="/", samesite="lax", secure=secure)


@router.post("/auth/login", status_code=204)
async def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    config: Config = request.app.state.config
    store: SessionStore = request.app.state.session_store
    limiter = request.app.state.limiter
    audit = request.app.state.audit
    ip = client_ip(request)

    # 已登录会话再次提交登录时，仍要求 CSRF token，避免被第三方站点静默换绑会话
    existing = optional_session(request)
    if existing is not None:
        require_csrf(request, existing)

    limit_key = ip + ":" + username.strip() if public_ingress(request) else ip

    if limiter.is_blocked(limit_key):
        retry_after = limiter.retry_after(limit_key)
        audit.write(
            audit_events.ACTION_LOGIN_BLOCKED, user=username, client_ip=ip, retry_after=retry_after
        )
        raise ApiError(
            "E1005",
            detail=f"retry after {retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )

    authenticator = request.app.state.authenticator
    # PAM 的 crypt 校验是阻塞调用（约 30–100ms），放到线程池避免卡住 1s 状态轮询
    result = await run_in_threadpool(authenticator.authenticate, username.strip(), password)
    if not result.ok:
        attempts = limiter.record_failure(limit_key)
        audit.write(
            audit_events.ACTION_LOGIN_FAILED,
            user=username,
            client_ip=ip,
            attempts=attempts,
            limit=config.login_max_attempts,
            mode=result.mode,
            reason=result.reason,  # 便于区分密码错 / PAM 不可用
        )
        blocked = attempts >= config.login_max_attempts
        headers = {}
        if blocked:
            headers["Retry-After"] = str(limiter.retry_after(limit_key) or config.login_window_sec)
        raise ApiError(
            "E1002" if not blocked else "E1005",
            detail="invalid username or password" if not blocked else "too many failed attempts",
            headers=headers,
        )

    limiter.reset(limit_key)
    if existing is not None:
        store.revoke(existing.sid)
    session = store.create(result.user, client_ip=ip, auth_mode=result.mode)
    session.extra["ingress"] = "public" if public_ingress(request) else "lan"
    _set_session_cookies(response, config, session.sid, session.csrf, request)
    response.status_code = 204
    response.headers["Cache-Control"] = "no-store"
    audit.write(
        audit_events.ACTION_LOGIN_SUCCESS,
        user=session.user,
        client_ip=ip,
        mode=result.mode,
    )
    return response


@router.get("/auth/verify")
async def verify(request: Request) -> Response:
    """Caddy ``forward_auth`` 的校验端点，见文档 5.4 / 9.3。"""

    require_loopback(request)
    store: SessionStore = request.app.state.session_store
    session = optional_session(request)
    if session is None:
        return Response(
            status_code=401,
            media_type="application/json",
            content=b'{"error":{"code":"E1004","message":"session expired","detail":"cookie not present or invalid"}}',
            headers={"Cache-Control": "no-store"},
        )
    response = Response(status_code=200, headers={"Cache-Control": "no-store"})
    response.headers["X-Auth-User"] = session.user
    response.headers["X-Auth-Csrf"] = session.csrf
    return response


@router.post("/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    store: SessionStore = request.app.state.session_store
    config: Config = request.app.state.config
    audit = request.app.state.audit
    session = optional_session(request)
    if session is not None:
        require_csrf(request, session)
        store.revoke(session.sid)
        try:
            await run_in_threadpool(
                request.app.state.peanut.call, "clear_session", actor=session.user,
                sid=session.sid, body={"_idempotency_key": secrets.token_urlsafe(24)},
            )
        except ApiError as exc:
            logger.warning("Peanut transient session cleanup unavailable: %s", exc.code)
        audit.write(audit_events.ACTION_LOGOUT, user=session.user, client_ip=client_ip(request))
    response = Response(status_code=204)
    _clear_session_cookies(response, config, request)
    response.headers["Cache-Control"] = "no-store"
    return response
