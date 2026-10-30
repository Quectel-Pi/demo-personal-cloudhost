"""核心单元：密码哈希、会话、限流、服务白名单、错误码（设计文档 9.4 / 11.1）。"""

from __future__ import annotations

import time

import pytest

from app import errors
from app.ratelimit import LoginRateLimiter
from app.security import (
    SessionStore,
    TokenSigner,
    hash_password,
    load_password_hash,
    verify_password,
)
from app.services import ALLOWED_SERVICES, ServiceManager


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
def test_password_roundtrip():
    digest = hash_password("s3cret-pass")
    assert digest != "s3cret-pass"
    assert verify_password("s3cret-pass", digest)
    assert not verify_password("wrong", digest)
    assert not verify_password("", digest)
    assert not verify_password("s3cret-pass", "garbage")


def test_password_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_load_password_hash(tmp_path):
    path = tmp_path / "admin.hash"
    assert load_password_hash(path) is None
    path.write_text("  abc123  \n", encoding="utf-8")
    assert load_password_hash(path) == "abc123"
    path.write_text("   \n", encoding="utf-8")
    assert load_password_hash(path) is None


# --------------------------------------------------------------------------- #
# 会话与签名
# --------------------------------------------------------------------------- #
def test_session_lifecycle():
    store = SessionStore(ttl_sec=60)
    session = store.create("admin", client_ip="192.168.1.2")
    assert store.get(session.sid) is session
    assert store.get("bogus") is None
    store.revoke(session.sid)
    assert store.get(session.sid) is None


def test_session_expires(monkeypatch):
    store = SessionStore(ttl_sec=60)
    session = store.create("admin")
    session.expires_at = time.time() - 1
    assert store.get(session.sid) is None
    assert len(store) == 0


def test_signature_roundtrip():
    signer = TokenSigner(b"k" * 32)
    token = signer.sign("session-id")
    assert signer.unsign(token) == "session-id"
    assert signer.unsign(token + "x") is None
    assert signer.unsign("nonsense") is None


def test_signature_ttl():
    signer = TokenSigner(b"k" * 32, ttl_sec=1)
    token = signer.sign("s")
    assert signer.unsign(token) == "s"
    time.sleep(1.2)
    assert signer.unsign(token) is None


def test_signer_rejects_short_secret():
    with pytest.raises(ValueError):
        TokenSigner(b"short")


# --------------------------------------------------------------------------- #
# 限流
# --------------------------------------------------------------------------- #
def test_limiter_blocks_and_recovers():
    limiter = LoginRateLimiter(max_attempts=3, window_sec=1)
    assert not limiter.is_blocked("ip")
    for _ in range(3):
        limiter.record_failure("ip")
    assert limiter.is_blocked("ip")
    assert limiter.is_blocked("ip") is True
    assert limiter.retry_after("ip") >= 1
    time.sleep(1.1)
    assert not limiter.is_blocked("ip")


def test_limiter_reset():
    limiter = LoginRateLimiter(max_attempts=2, window_sec=60)
    limiter.record_failure("a")
    limiter.record_failure("a")
    assert limiter.is_blocked("a")
    limiter.reset("a")
    assert not limiter.is_blocked("a")
    assert limiter.failures("a") == 0


# --------------------------------------------------------------------------- #
# 服务管理
# --------------------------------------------------------------------------- #
def test_allowed_services_match_document():
    assert ALLOWED_SERVICES == (
        "caddy",
        "cloudhost-portal",
        "cloudhost-files",
        "cloudhost-ttyd",
    )


def test_service_manager_rejects_unknown():
    manager = ServiceManager(systemctl="/bin/false")
    with pytest.raises(errors.ApiError) as exc:
        manager.snapshot("sshd")
    assert exc.value.code == "E1001"


def test_service_manager_without_systemd():
    manager = ServiceManager(systemctl="")
    snapshot = manager.snapshot("caddy")
    assert snapshot.state == "stopped"
    assert snapshot.enabled is False
    assert "systemd" in snapshot.detail


def test_service_manager_parses_systemctl(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = (
            "ActiveState=active\n"
            "UnitFileState=enabled\n"
            "ActiveEnterTimestamp=Sun 2026-10-19 10:00:00 CST\n"
        )
        stderr = ""

    manager = ServiceManager(systemctl="systemctl", runner=lambda *a, **k: FakeCompleted())
    snapshot = manager.snapshot("cloudhost-portal")
    assert snapshot.state == "running"
    assert snapshot.enabled is True
    assert snapshot.since.startswith("Sun 2026-10-19")


# --------------------------------------------------------------------------- #
# 错误码
# --------------------------------------------------------------------------- #
def test_error_codes_match_document():
    expected = {
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
    }
    for code, status in expected.items():
        assert errors.HTTP_STATUS[code] == status
        assert code in errors.MESSAGES


def test_error_payload_shape():
    payload = errors.error_payload("E1004", detail="cookie not present or invalid")
    assert payload["error"]["code"] == "E1004"
    assert payload["error"]["message"] == "session expired"
    assert payload["error"]["detail"] == "cookie not present or invalid"


def test_api_error_headers():
    exc = errors.ApiError("E1005", headers={"Retry-After": "120"})
    assert exc.status_code == 429
    assert exc.headers["Retry-After"] == "120"
