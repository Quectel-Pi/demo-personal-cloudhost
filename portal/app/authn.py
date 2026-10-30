"""登录策略：PAM（系统账号）与 admin.hash 两种模式 + 可选回退。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import pam
from .config import Config
from .security import load_password_hash, verify_password

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    user: str
    reason: str  # ok | denied | unavailable | backend_error | fallback_denied
    mode: str  # system | password_hash


class Authenticator(Protocol):
    mode: str

    def authenticate(self, username: str, password: str) -> AuthResult: ...


class SystemAuthenticator:
    mode = "system"

    def __init__(self, system_user: str) -> None:
        self._user = system_user

    def authenticate(self, username: str, password: str) -> AuthResult:
        # 用户名必须完全一致：PAM 对非 root 进程只能校验自己，
        # 不一致直接拒绝，避免无意义的 helper 调用与账号枚举。
        if username.strip() != self._user:
            return AuthResult(False, self._user, "denied", self.mode)
        result = pam.authenticate(self._user, password)
        if result.outcome is pam.AuthOutcome.OK:
            return AuthResult(True, self._user, "ok", self.mode)
        if result.outcome is pam.AuthOutcome.DENIED:
            return AuthResult(False, self._user, "denied", self.mode)
        if result.outcome is pam.AuthOutcome.UNAVAILABLE:
            logger.warning("PAM 不可用：%s", result.message)
            return AuthResult(False, self._user, "unavailable", self.mode)
        logger.error("PAM 异常：%s", result.message)
        return AuthResult(False, self._user, "backend_error", self.mode)


class HashAuthenticator:
    mode = "password_hash"

    def __init__(self, admin_user: str, hash_path: Path) -> None:
        self._user = admin_user
        self._hash_path = hash_path

    def authenticate(self, username: str, password: str) -> AuthResult:
        stored = load_password_hash(self._hash_path)
        if stored is None:
            return AuthResult(False, self._user, "backend_error", self.mode)
        if username.strip() != self._user or not verify_password(password, stored):
            return AuthResult(False, self._user, "denied", self.mode)
        return AuthResult(True, self._user, "ok", self.mode)


class FallbackAuthenticator:
    """system 优先；仅在 PAM 不可用/后端异常时回退 admin.hash。"""

    def __init__(self, primary: Authenticator, backup: Authenticator, enabled: bool) -> None:
        self._primary = primary
        self._backup = backup
        self._enabled = enabled
        self.mode = primary.mode

    def authenticate(self, username: str, password: str) -> AuthResult:
        first = self._primary.authenticate(username, password)
        if first.ok or not self._enabled:
            return first
        if first.reason not in ("unavailable", "backend_error"):
            return first
        second = self._backup.authenticate(username, password)
        if second.ok:
            logger.warning("PAM 不可用，已回退 admin.hash 登录（user=%s）", second.user)
            return AuthResult(True, second.user, "ok", second.mode)
        return AuthResult(False, first.user, "fallback_denied", first.mode)


def build_authenticator(config: Config) -> Authenticator:
    backup = HashAuthenticator(config.admin_user, config.password_hash_path)
    if config.auth_mode == "password_hash":
        return backup
    return FallbackAuthenticator(
        SystemAuthenticator(config.system_user),
        backup,
        config.password_hash_fallback,
    )
