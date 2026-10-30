"""审计日志（设计文档 11.1 / A7）。

以 JSON Lines 追加写入 ``/var/log/cloudhost/audit.log``，记录登录成功/失败、
文件删除、服务重启等关键动作。写日志失败不影响主流程。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 审计动作类型
ACTION_LOGIN_SUCCESS = "login_success"
ACTION_LOGIN_FAILED = "login_failed"
ACTION_LOGIN_BLOCKED = "login_blocked"
ACTION_LOGOUT = "logout"
ACTION_FILE_DELETE = "file_delete"
ACTION_FILE_UPLOAD = "file_upload"
ACTION_SERVICE_RESTART = "service_restart"
ACTION_AUTH_DENIED = "auth_denied"


class AuditLog:
    def __init__(self, path: Path, *, enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:  # pragma: no cover - 权限问题
                logger.warning("无法创建审计日志目录 %s：%s", self.path.parent, exc)

    def write(self, action: str, *, user: str = "-", client_ip: str = "-", **fields: Any) -> None:
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "epoch": round(time.time(), 3),
            "action": action,
            "user": user,
            "ip": client_ip,
        }
        record.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:  # pragma: no cover
                logger.warning("审计日志写入失败：%s", exc)
        logger.info("audit %s %s", action, record.get("detail", ""))
