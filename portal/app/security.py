"""密码哈希、会话与签名（设计文档 5.4 / 11.1）。

- 密码哈希：优先 argon2id，其次 bcrypt，最后回退 PBKDF2-SHA256。
  回退实现仅用于开发机缺少依赖的场景，板端由 install.sh 安装 argon2-cffi。
- 会话：Cookie 内只放随机 session id + HMAC 签名，会话数据保存在服务端内存，
  因此登出可立即失效。
- CSRF：token 与会话绑定，所有 POST 必须携带。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 依赖可用性随环境变化
    from argon2 import PasswordHasher
    from argon2.exceptions import VerificationError as Argon2VerificationError
    from argon2.exceptions import VerifyMismatchError as Argon2MismatchError

    _ARGON2 = PasswordHasher()
except Exception:  # noqa: BLE001 - 缺少 argon2-cffi 时回退
    _ARGON2 = None
    Argon2VerificationError = Exception  # type: ignore[assignment,misc]
    Argon2MismatchError = Exception  # type: ignore[assignment,misc]

try:  # pragma: no cover
    import bcrypt as _bcrypt
except Exception:  # noqa: BLE001
    _bcrypt = None

SESSION_COOKIE = "ch_session"
CSRF_COOKIE = "ch_csrf"
CSRF_HEADER = "X-CSRF-Token"
_PBKDF2_ROUNDS = 260_000


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """生成密码哈希字符串，可直接写入 ``admin.hash``。"""

    if not password:
        raise ValueError("密码不能为空")
    if _ARGON2 is not None:
        return _ARGON2.hash(password)
    if _bcrypt is not None:
        return "bcrypt$" + _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码；任何异常都视为校验失败，避免信息泄漏。"""

    if not password or not stored:
        return False
    stored = stored.strip()
    try:
        if stored.startswith("bcrypt$") and _bcrypt is not None:
            return _bcrypt.checkpw(password.encode("utf-8"), stored[len("bcrypt$") :].encode("utf-8"))
        if stored.startswith("pbkdf2_sha256$"):
            _, rounds, salt_b64, digest_b64 = stored.split("$", 3)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
            return hmac.compare_digest(actual, expected)
        if _ARGON2 is not None:
            return _ARGON2.verify(stored, password)
    except (Argon2MismatchError, Argon2VerificationError, ValueError, TypeError):
        return False
    except Exception:  # noqa: BLE001 - 任何异常一律失败
        return False
    return False


def load_password_hash(path: Path) -> str | None:
    """读取密码哈希文件；不存在或为空返回 ``None``。"""

    try:
        content = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        # 权限问题绝不能静默降级：否则登录会一律返回「密码错误」，
        # 实际原因是进程读不到 /etc/cloudhost/secret/admin.hash。
        logger.error("密码哈希文件不可读（%s）：%s", path, exc)
        return None
    return content or None


# --------------------------------------------------------------------------- #
# HMAC 签名
# --------------------------------------------------------------------------- #
class TokenSigner:
    """基于 HMAC-SHA256 的 ``payload.signature`` 签名器。"""

    def __init__(self, secret: bytes, *, ttl_sec: int | None = None) -> None:
        if len(secret) < 16:
            raise ValueError("签名密钥长度至少 16 字节")
        self._secret = secret
        self._ttl = ttl_sec

    @classmethod
    def from_file(cls, path: Path, *, ttl_sec: int | None = None) -> "TokenSigner":
        secret = _read_or_create_secret(path)
        return cls(secret, ttl_sec=ttl_sec)

    def sign(self, payload: str) -> str:
        body = payload
        if self._ttl:
            body = f"{payload}|{int(time.time()) + self._ttl}"
        mac = hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{_b64e(body)}.{mac}"

    def unsign(self, token: str) -> str | None:
        try:
            encoded, mac = token.split(".", 1)
            body = _b64d(encoded)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        if self._ttl is None:
            # 未启用 TTL：payload 即整体，不做切分
            return body
        payload, sep, issued = body.rpartition("|")
        if not sep or not issued.strip().isdigit():
            return None
        # 用浮点比较，避免 int() 截断导致过期判定滞后 1 秒
        if float(issued) <= time.time():
            return None
        return payload


def _read_or_create_secret(path: Path) -> bytes:
    try:
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    return secret


def _b64e(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(text: str) -> str:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding).decode("utf-8")


# --------------------------------------------------------------------------- #
# 会话存储
# --------------------------------------------------------------------------- #
@dataclass
class Session:
    sid: str
    user: str
    csrf: str
    created_at: float
    expires_at: float
    client_ip: str = ""
    auth_mode: str = "system"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return self.expires_at <= time.time()


class SessionStore:
    """进程内会话表，登出即删除。"""

    def __init__(self, ttl_sec: int) -> None:
        if ttl_sec < 60:
            raise ValueError("session ttl 过短")
        self._ttl = ttl_sec
        self._sessions: dict[str, Session] = {}

    @property
    def ttl(self) -> int:
        return self._ttl

    def create(
        self, user: str, *, client_ip: str = "", auth_mode: str = "system"
    ) -> Session:
        now = time.time()
        session = Session(
            sid=secrets.token_urlsafe(32),
            user=user,
            csrf=secrets.token_urlsafe(24),
            created_at=now,
            expires_at=now + self._ttl,
            client_ip=client_ip,
            auth_mode=auth_mode,
        )
        self._sessions[session.sid] = session
        self.prune()
        return session

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        if session.expired:
            self._sessions.pop(sid, None)
            return None
        return session

    def revoke(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)

    def prune(self) -> int:
        dead = [sid for sid, session in self._sessions.items() if session.expired]
        for sid in dead:
            self._sessions.pop(sid, None)
        return len(dead)

    def __len__(self) -> int:
        return len(self._sessions)
