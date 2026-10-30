"""业务接口（设计文档 9.2）。

- ``GET  /api/status``
- ``GET  /api/services``
- ``POST /api/services/{name}/restart``
- ``GET  /api/overview``（门户首屏聚合，实现补充项，减少首屏请求数）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.concurrency import run_in_threadpool

from . import audit as audit_events
from .deps import client_ip, current_session, require_csrf
from .errors import ApiError
from .remote_status import (
    AuthorizationState,
    DaemonState,
    HttpsState,
    InstallationState,
    ManagementIntent,
    NetworkState,
    Provider,
    ReachabilityState,
    RemoteStatus,
    public_summary,
)
from .services import ALLOWED_SERVICES

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    current_session(request)
    config = request.app.state.config
    collector = request.app.state.status_collector
    return collector.collect(interval_ms=config.status_interval_ms)


@router.get("/remote/status")
async def get_remote_status(request: Request, response: Response) -> dict[str, Any]:
    """Return a safe summary without claiming an unimplemented live provider probe."""

    current_session(request)
    config = request.app.state.config
    if config.remote_provider == "peanut":
        runtime = await run_in_threadpool(request.app.state.peanut.call, "status")
        response.headers["Cache-Control"] = "no-store"
        device = runtime["device"]
        return {
            "summary": {
                "enabled": runtime["intent"]["enabled"],
                "active_provider": "peanut",
                "connection": {
                    "online": "connected", "offline": "disconnected", "logging_in": "connecting",
                    "retrying": "degraded",
                }.get(device["state"], "unknown"),
                "https": runtime["https"]["state"] if runtime["https"]["state"] in {
                    "endpoint_ready", "failed", "off", "unknown",
                } else "unknown",
                "reachability": "unverified",
            },
            "observed_at": device["observed_at"],
            "generated_at": runtime["generated_at"],
            "stale": device["stale"],
        }
    provider = Provider.parse(config.remote_provider)
    enabled = config.remote_enabled
    status = RemoteStatus(
        installation=InstallationState.UNKNOWN,
        daemon=DaemonState.UNKNOWN,
        authorization=AuthorizationState.UNKNOWN,
        network=NetworkState.UNKNOWN,
        https=HttpsState.UNKNOWN,
        management_intent=ManagementIntent(
            enabled=enabled,
            active_provider=provider if enabled else Provider.NONE,
            paused_this_boot=None,
            config_revision=config.remote_config_revision,
        ),
        reachability=ReachabilityState.UNVERIFIED,
        task=None,
        observed_at=datetime.now(timezone.utc),
        stale=True,
    )
    response.headers["Cache-Control"] = "no-store"
    return public_summary(status)


@router.get("/services")
async def get_services(request: Request) -> dict[str, Any]:
    current_session(request)
    manager = request.app.state.service_manager
    return {
        "allowed": list(ALLOWED_SERVICES),
        "services": manager.list_services(),
    }


@router.post("/services/{name}/restart", status_code=202)
async def restart_service(name: str, request: Request) -> dict[str, Any]:
    session = current_session(request)
    require_csrf(request, session)
    if name not in ALLOWED_SERVICES:
        raise ApiError("E1001", detail=f"service {name!r} is not restartable")
    manager = request.app.state.service_manager
    audit = request.app.state.audit
    ip = client_ip(request)
    job_id = manager.restart(name, actor=session.user, client_ip=ip)
    audit.write(
        audit_events.ACTION_SERVICE_RESTART, user=session.user, client_ip=ip, service=name
    )
    return {"accepted": True, "service": name, "job_id": job_id}


@router.get("/service-jobs/{job_id}")
async def service_job(job_id: str, request: Request, response: Response) -> dict[str, Any]:
    current_session(request)
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.service_manager.job(job_id)


@router.get("/overview")
async def get_overview(request: Request) -> dict[str, Any]:
    """门户首屏：一次返回状态、服务与入口信息，保证 A1 首屏 ≤ 3s。"""

    session = current_session(request)
    config = request.app.state.config
    collector = request.app.state.status_collector
    manager = request.app.state.service_manager
    from .routes_pages import _ttyd_available  # 避免顶层循环导入

    return {
        "user": session.user,
        "status": collector.collect(interval_ms=config.status_interval_ms),
        "services": manager.list_services(),
        "links": {
            "files": "/files/",
            "site": "/site/",
            "term": "/term/" if _ttyd_available(config) else None,
        },
        "version": request.app.state.version,
        "tls_mode": config.tls_mode,
        "access_mode": config.access_mode,
    }


@router.get("/config")
async def get_public_config(request: Request) -> dict[str, Any]:
    """门户需要的只读配置（不含任何密钥）。"""

    current_session(request)
    config = request.app.state.config
    return {
        "status_interval_ms": config.status_interval_ms,
        "session_ttl": config.session_ttl,
        "site_public": config.site_public,
        "ttyd_enabled": config.ttyd_enabled,
        "tls_mode": config.tls_mode,
        "access_mode": config.access_mode,
        "auth_mode": config.auth_mode,
        "files_root": str(config.files_root),
        "files_root_mode": config.file_root_mode,
        "cloud_root": str(config.cloud_root),  # 保留：旧前端/验收脚本兼容
        "max_upload_mb": config.max_upload_mb,
    }


@router.get("/healthz")
async def healthz(request: Request, response: Response) -> dict[str, Any]:
    """轻量健康检查，systemd 与 install.sh 使用；无敏感数据。"""

    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok", "service": "cloudhost-portal"}
