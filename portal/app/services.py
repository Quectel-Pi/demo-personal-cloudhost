"""服务状态与重启（设计文档 9.2 / 10 / 8）。

- 状态来自 ``systemctl show``；无 systemd 的开发机上返回 ``stopped`` 而不是报错。
- 只允许重启白名单内的服务，防止越权调用任意 unit。
- 重启采用异步任务：接口立即返回 202，实际 ``systemctl restart`` 在后台执行。
"""

from __future__ import annotations

import logging
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .errors import ApiError

logger = logging.getLogger(__name__)

# 设计文档 9.2 允许重启的服务
ALLOWED_SERVICES: tuple[str, ...] = (
    "caddy",
    "cloudhost-portal",
    "cloudhost-files",
    "cloudhost-ttyd",
)

# 服务名 → 健康检查目标
_HEALTH_TARGETS: dict[str, tuple[str, int]] = {
    "cloudhost-portal": ("127.0.0.1", 8080),
    "cloudhost-files": ("127.0.0.1", 8081),
    "cloudhost-ttyd": ("127.0.0.1", 7681),
}

_STATE_MAP = {
    "active": "running",
    "reloading": "running",
    "activating": "running",
    "deactivating": "stopped",
    "inactive": "stopped",
    "failed": "failed",
}


@dataclass
class ServiceSnapshot:
    name: str
    state: str
    enabled: bool
    since: str | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "enabled": self.enabled,
            "since": self.since,
            "detail": self.detail,
        }


class ServiceManager:
    def __init__(
        self,
        *,
        systemctl: str = "systemctl",
        health_timeout: float = 0.4,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        job_dir: Path | None = None,
    ) -> None:
        self._systemctl = shutil.which(systemctl) if runner is None else systemctl
        self._runner = runner or subprocess.run
        self._health_timeout = health_timeout
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._job_dir = job_dir
        if job_dir is not None:
            job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            for path in job_dir.glob("*.json"):
                value = json.loads(path.read_text())
                if value["status"] in {"queued", "running"}:
                    value.update(status="recovery_required", reason="portal_interrupted")
                self._jobs[value["job_id"]] = value
                self._save_job(value)

    # ------------------------------------------------------------------ #
    def list_services(self, names: tuple[str, ...] = ALLOWED_SERVICES) -> list[dict[str, Any]]:
        return [self.snapshot(name).as_dict() for name in names]

    def snapshot(self, name: str) -> ServiceSnapshot:
        if name not in ALLOWED_SERVICES:
            raise ApiError("E1001", detail=f"service {name!r} is not managed by cloudhost")
        if not self._systemctl:
            return ServiceSnapshot(name, "stopped", False, None, "systemd not available")

        props = self._show(name)
        active = props.get("ActiveState", "inactive")
        state = _STATE_MAP.get(active, "stopped")
        if name in self._pending:
            state = "running" if state == "running" else "starting"
        enabled = props.get("UnitFileState", "") in ("enabled", "enabled-runtime", "static")
        since = props.get("ActiveEnterTimestamp") or None
        detail = self._health_detail(name, state)
        return ServiceSnapshot(name, state, enabled, since, detail)

    def restart(self, name: str, *, actor: str = "-", client_ip: str = "-") -> str:
        if name not in ALLOWED_SERVICES:
            raise ApiError("E1001", detail=f"service {name!r} is not managed by cloudhost")
        if not self._systemctl:
            raise ApiError("E2002", detail="systemd not available on this host", status_code=502)
        with self._lock:
            if name in self._pending:
                raise ApiError("E5005", detail="service_operation_in_progress")
            job_id = secrets.token_urlsafe(24)
            self._save_job({"job_id": job_id, "service": name, "actor": actor,
                            "status": "queued", "reason": None})
            self._pending.add(name)
        thread = threading.Thread(
            target=self._restart_worker,
            args=(name, job_id),
            name=f"restart-{name}",
            daemon=True,
        )
        thread.start()
        logger.warning("service restart requested: %s by %s from %s", name, actor, client_ip)
        return job_id

    def _save_job(self, value: dict[str, Any]) -> None:
        if self._job_dir is not None:
            target = self._job_dir / (value["job_id"] + ".json")
            fd, temporary = tempfile.mkstemp(dir=self._job_dir, prefix=".job-")
            try:
                with os.fdopen(fd, "w") as stream:
                    json.dump(value, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self._jobs[value["job_id"]] = dict(value)

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            value = self._jobs.get(job_id)
            if value is None:
                raise ApiError("E9001")
            return dict(value)

    def is_pending(self, name: str) -> bool:
        with self._lock:
            return name in self._pending

    # ------------------------------------------------------------------ #
    def _restart_worker(self, name: str, job_id: str) -> None:
        value = dict(self._jobs[job_id], status="running")
        try:
            with self._lock:
                self._save_job(value)
            completed = self._runner(
                [self._systemctl, "restart", name],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if completed.returncode != 0:
                value.update(status="failed", reason="systemd_restart_failed")
                logger.error(
                    "systemctl restart %s failed (%s): %s",
                    name,
                    completed.returncode,
                    (completed.stderr or "").strip(),
                )
            else:
                snapshot = self.snapshot(name)
                healthy = snapshot.state == "running" and (
                    name == "caddy" or snapshot.detail.startswith("tcp ") and " ok " in snapshot.detail
                )
                value.update(status="succeeded" if healthy else "failed",
                             reason=None if healthy else "health_check_failed")
        except subprocess.TimeoutExpired:
            value.update(status="recovery_required", reason="restart_timeout_check_state")
            logger.error("systemctl restart %s timed out", name)
        except OSError as exc:  # pragma: no cover
            value.update(status="failed", reason="systemd_unavailable")
            logger.error("systemctl restart %s error: %s", name, exc)
        finally:
            with self._lock:
                self._pending.discard(name)
                self._save_job(value)

    def _show(self, name: str) -> dict[str, str]:
        try:
            completed = self._runner(
                [
                    self._systemctl,
                    "show",
                    name,
                    "--property=ActiveState,UnitFileState,ActiveEnterTimestamp",
                    "--no-pager",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("systemctl show %s failed: %s", name, exc)
            return {}
        if completed.returncode != 0:
            return {}
        props: dict[str, str] = {}
        for line in (completed.stdout or "").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                props[key.strip()] = value.strip()
        return props

    def _health_detail(self, name: str, state: str) -> str:
        target = _HEALTH_TARGETS.get(name)
        if state != "running":
            return f"unit {state}"
        if target is None:
            return "caddy managed unit"
        host, port = target
        import socket

        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=self._health_timeout):
                elapsed_ms = (time.monotonic() - started) * 1000
                return f"tcp {host}:{port} ok ({elapsed_ms:.0f} ms)"
        except OSError:
            return f"tcp {host}:{port} unreachable"
