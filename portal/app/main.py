"""门户应用入口（设计文档 F1 / F2 / F5 / F7）。

对外只监听 127.0.0.1:8080，由 Caddy 反向代理到 ``/``，并提供
``/auth/verify`` 给 ``forward_auth`` 使用。

启动方式::

    CLOUDHOST_CONFIG=/etc/cloudhost/config.yaml python -m app.main
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .authn import Authenticator, build_authenticator
from .audit import AuditLog
from .config import Config, ConfigError, load_config
from .deps import optional_session
from .errors import ApiError, error_payload
from .ratelimit import LoginRateLimiter
from .routes_api import router as api_router
from .routes_auth import router as auth_router
from .routes_files import router as files_router
from .routes_pages import router as pages_router
from .routes_peanut import router as peanut_router
from .peanut_client import PeanutClient
from .ingress import IngressMiddleware
from .routes_terminal import router as terminal_router
from .security import SessionStore, TokenSigner
from .services import ServiceManager
from .status import StatusCollector

logger = logging.getLogger("cloudhost.portal")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(
    config: Config | None = None,
    *,
    audit_enabled: bool = True,
    authenticator: Authenticator | None = None,
) -> FastAPI:
    """构造 FastAPI 应用；测试可传入自定义配置与认证器。"""

    app = FastAPI(
        title="Quectel Pi M2 个人云主机门户",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    cfg = config or load_config()
    _ensure_runtime_dirs(cfg)

    app.state.config = cfg
    app.state.version = __version__
    app.state.authenticator = authenticator or build_authenticator(cfg)
    app.state.session_store = SessionStore(cfg.session_ttl)
    app.state.limiter = LoginRateLimiter(cfg.login_max_attempts, cfg.login_window_sec)
    app.state.audit = AuditLog(cfg.audit_log_path, enabled=audit_enabled)
    app.state.status_collector = StatusCollector(primary_iface=cfg.primary_iface)
    app.state.service_manager = ServiceManager(job_dir=cfg.state_dir / "service-jobs")
    app.state.signer = _build_signer(cfg)
    app.state.peanut = PeanutClient()
    gateway_key = cfg.session_secret_path.parent / "peanut-gateway.key"
    try:
        gateway_token = gateway_key.read_text().strip()
        if len(gateway_token) < 32:
            raise ValueError("invalid gateway key")
    except FileNotFoundError:
        gateway_token = ""
    app.add_middleware(IngressMiddleware, token=gateway_token)

    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(files_router)
    app.include_router(peanut_router)
    app.include_router(terminal_router)
    app.include_router(pages_router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _register_exception_handlers(app)
    logger.info(
        "cloudhost portal %s ready: auth_mode=%s files_root=%s site_dir=%s ttyd=%s",
        __version__,
        cfg.auth_mode,
        cfg.files_root,
        cfg.site_dir,
        cfg.ttyd_enabled,
    )
    return app


def _build_signer(cfg: Config) -> TokenSigner:
    try:
        return TokenSigner.from_file(cfg.session_secret_path, ttl_sec=cfg.session_ttl)
    except (OSError, ValueError) as exc:  # pragma: no cover - 权限异常
        logger.warning("会话密钥初始化失败，改用进程内随机密钥：%s", exc)
        return TokenSigner(os.urandom(48), ttl_sec=cfg.session_ttl)


def _ensure_runtime_dirs(cfg: Config) -> None:
    for directory in (cfg.cloud_root, cfg.site_dir, cfg.log_dir, cfg.state_dir):
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - 权限问题
            logger.warning("无法创建目录 %s：%s", directory, exc)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ANN202
        headers = {"Cache-Control": "no-store", **exc.headers}
        return JSONResponse(
            status_code=exc.status_code, content=exc.payload(), headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:  # noqa: ANN202
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg')}"
            for err in exc.errors()
        )
        return JSONResponse(
            status_code=400,
            content=error_payload("E1001", detail=detail or "invalid request"),
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_payload("E9000", detail="internal_error"),
            headers={"Cache-Control": "no-store"},
        )


app = None  # 由 run() 或 ASGI 服务器在导入时创建


def run() -> None:
    """命令行入口：启动 uvicorn，仅监听 127.0.0.1。"""

    import uvicorn

    logging.basicConfig(
        level=os.environ.get("CLOUDHOST_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[cloudhost] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    application = create_app(cfg)
    uvicorn.run(
        application,
        host="127.0.0.1",
        port=cfg.portal_port,
        log_level=os.environ.get("CLOUDHOST_LOG_LEVEL", "info").lower(),
        access_log=os.environ.get("CLOUDHOST_ACCESS_LOG", "0") == "1",
        # 必须关闭：uvicorn 的 ProxyHeadersMiddleware 会拿 X-Forwarded-For 覆盖
        # scope["client"]，导致 /auth/verify 看到的来源变成真实客户端 IP 而不是
        # 转发者 Caddy，于是 E1003「仅限回环」会把正常的网关鉴权全部拒掉。
        # 保留 request.client 为真实 TCP 对端，真实客户端 IP 由 deps.client_ip()
        # 自行从 X-Forwarded-For 读取。
        proxy_headers=False,
    )


def get_app() -> FastAPI:
    """ASGI 工厂；uvicorn ``--factory app.main:get_app`` 亦可使用。"""

    global app
    if app is None:
        app = create_app()
    return app


if __name__ == "__main__":
    # systemd 的 ExecStart 是 `python -m app.main`，此时 __name__ == "__main__"。
    # 没有这个守卫的话，模块导入完就直接退出（status=0），服务看似「启动成功」
    # 却没有任何端口监听。
    run()


__all__ = ["create_app", "get_app", "run", "optional_session", "RedirectResponse"]
