"""鉴权、会话、CSRF 与限流（设计文档 9.3 / 11.1 / A4）。"""

from __future__ import annotations

import pytest

from dataclasses import replace

from fastapi.testclient import TestClient

from app import pam
from app.authn import (
    AuthResult,
    FallbackAuthenticator,
    HashAuthenticator,
    SystemAuthenticator,
)
from app.deps import is_remote_admin
from app.main import create_app
from app.security import SessionStore
from conftest import TEST_PASSWORD, TEST_USER


def test_unauthenticated_files_redirect(client):
    """A4：未登录访问 /files/ 返回 302 跳到登录页。

    Caddy 的 forward_auth 把 portal 的 401 转成 302；这里直接校验
    /auth/verify 的 401 语义（Caddy 的转换配置见 deploy/Caddyfile.tmpl）。
    """

    response = client.get("/auth/verify", headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "E1004"


def test_verify_requires_loopback(app):
    """非回环来源直接调用 /auth/verify 必须被拒（E1003）。"""

    from fastapi.testclient import TestClient

    with TestClient(app, client=("192.168.1.20", 5000)) as remote:
        response = remote.get("/auth/verify")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_login_success_sets_cookie(client, config):
    response = client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": "192.168.1.50"},
    )
    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "")
    assert "ch_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie.lower() or "samesite=lax" in set_cookie.lower()
    # v1.0 纯 HTTP，必须不设 Secure（文档第 6 章）
    assert "Secure" not in set_cookie
    assert client.cookies.get("ch_csrf")


def test_login_wrong_password(client):
    response = client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": "wrong"},
        headers={"X-Forwarded-For": "192.168.1.51"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E1002"


def test_login_unknown_user(client):
    response = client.post(
        "/auth/login",
        data={"username": "root", "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": "192.168.1.52"},
    )
    assert response.status_code == 401


def test_verify_after_login(client, login):
    assert login().status_code == 204
    response = client.get("/auth/verify")
    assert response.status_code == 200
    assert response.headers["X-Auth-User"] == TEST_USER


def test_status_requires_session(client):
    response = client.get("/api/status")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E1004"


def test_status_after_login(authed):
    response = authed.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert "cpu" in payload and "mem" in payload and "net" in payload


def test_logout_requires_csrf(client, login):
    assert login().status_code == 204
    response = client.post("/auth/logout")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_logout_revokes_session(client, authed):
    token = client.cookies.get("ch_csrf")
    response = authed.post("/auth/logout", headers={"X-CSRF-Token": token})
    assert response.status_code == 204
    # 登出后旧 Cookie 不再可用
    client.cookies.clear()
    assert client.get("/api/status").status_code == 401


def test_rate_limit_blocks_after_max_attempts(client, config):
    """A4：同一 IP 连续 5 次错误密码后触发 429（fail2ban 侧封禁 ≥ 10 分钟）。"""

    ip = "10.1.2.3"
    for attempt in range(config.login_max_attempts):
        response = client.post(
            "/auth/login",
            data={"username": TEST_USER, "password": "bad"},
            headers={"X-Forwarded-For": ip},
        )
        assert response.status_code in (401, 429)

    blocked = client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": ip},
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "E1005"
    assert int(blocked.headers["Retry-After"]) >= 1

    # 其他 IP 不受影响
    other = client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": "10.9.9.9"},
    )
    assert other.status_code == 204


def test_success_resets_counter(client, config):
    ip = "10.4.4.4"
    for _ in range(config.login_max_attempts - 1):
        client.post(
            "/auth/login",
            data={"username": TEST_USER, "password": "bad"},
            headers={"X-Forwarded-For": ip},
        )
    ok = client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": ip},
    )
    assert ok.status_code == 204
    client.headers["X-CSRF-Token"] = client.cookies["ch_csrf"]
    # 成功后计数清零，可再次失败 max_attempts-1 次而未被封
    for _ in range(config.login_max_attempts - 1):
        client.post(
            "/auth/login",
            data={"username": TEST_USER, "password": "bad"},
            headers={"X-Forwarded-For": ip},
        )
    again = client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": ip},
    )
    assert again.status_code == 204


def test_restart_requires_csrf(authed):
    response = authed.post("/api/services/caddy/restart", headers={"X-CSRF-Token": "bad"})
    assert response.status_code == 403


def test_restart_rejects_unknown_service(authed):
    token = authed.cookies.get("ch_csrf")
    response = authed.post(
        "/api/services/sshd/restart", headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E1001"


def test_restart_accepted(authed):
    token = authed.cookies.get("ch_csrf")
    response = authed.post(
        "/api/services/caddy/restart", headers={"X-CSRF-Token": token}
    )
    assert response.status_code in (202, 502)
    if response.status_code == 202:
        assert response.json()["accepted"] is True


def test_audit_log_records_login(config, client):
    client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": "bad"},
        headers={"X-Forwarded-For": "192.168.1.77"},
    )
    client.post(
        "/auth/login",
        data={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"X-Forwarded-For": "192.168.1.77"},
    )
    content = config.audit_log_path.read_text(encoding="utf-8")
    assert "login_failed" in content
    assert "login_success" in content
    assert "192.168.1.77" in content


def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert 'data-i18n="experience.headline"' in response.text
    assert "Pi M2" in response.text
    assert "/static/styles.css" in response.text


def test_dashboard_hides_terminal_when_ttyd_missing(authed):
    """配置为 true 但板上没有 ttyd unit/二进制时必须隐藏终端入口。"""

    response = authed.get("/")
    assert response.status_code == 200
    assert 'id="term-tab"' in response.text
    assert 'ttyd: "false"' in response.text


def test_root_redirects_to_login_when_anonymous(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "登录" in response.text


@pytest.mark.parametrize("path", ["/files", "/files/", "/term", "/term/", "/site", "/site/"])
def test_gateway_paths_are_configured(client, path):
    """这些路径由 Caddy 提供；门户只负责 /auth/verify 的判定语义。

    板端 Caddyfile 过期时请求会落到门户兜底。此时必须返回可读 HTML
    （而不是 FastAPI 默认的 ``{"detail":"Not Found"}``），便于用户自查。
    """

    response = client.get(path)
    assert response.status_code in (401, 404)
    if response.status_code == 404:
        assert response.headers["content-type"].startswith("text/html")
        assert 'data-i18n="embed.backendUnavailable"' in response.text
        assert "/auth/verify" not in response.text


# --------------------------------------------------------------------------- #
# 6.2 系统账号登录（PAM）与回退
# --------------------------------------------------------------------------- #
def _make_client(app) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def _login_on(client, user: str = TEST_USER, password: str = TEST_PASSWORD, ip: str = "192.168.1.60"):
    return client.post(
        "/auth/login",
        data={"username": user, "password": password},
        headers={"X-Forwarded-For": ip},
    )


def _audit(config) -> str:
    return config.audit_log_path.read_text(encoding="utf-8")


def test_system_login_success(client, fake_auth):
    fake_auth.result = AuthResult(True, "admin", "ok", "system")
    assert _login_on(client).status_code == 204

    session = client.app.state.session_store.get(client.cookies.get("ch_session"))
    assert session.auth_mode == "system"
    verify = client.get("/auth/verify")
    assert verify.status_code == 200
    assert verify.headers["X-Auth-User"] == "admin"


def test_system_login_wrong_password(client, fake_auth, config):
    fake_auth.result = AuthResult(False, TEST_USER, "denied", "system")
    response = _login_on(client, password="wrong")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E1002"

    audit = _audit(config)
    assert '"reason":"denied"' in audit
    assert '"mode":"system"' in audit


def test_system_login_unknown_user(config, monkeypatch):
    """用户名与服务用户不一致时不进 PAM（避免 helper 调用与账号枚举）。"""

    calls: list[tuple[str, str]] = []

    def _fake_pam(username: str, password: str, **kwargs):
        calls.append((username, password))
        return pam.PamResult(pam.AuthOutcome.DENIED)

    monkeypatch.setattr("app.authn.pam.authenticate", _fake_pam)
    app2 = create_app(
        config, audit_enabled=True, authenticator=SystemAuthenticator(TEST_USER)
    )
    with _make_client(app2) as client2:
        response = _login_on(client2, user="root")

    assert response.status_code == 401
    assert calls == []


def test_pam_unavailable_fallback_success(config, monkeypatch):
    monkeypatch.setattr(
        "app.authn.pam.authenticate",
        lambda username, password, **kw: pam.PamResult(pam.AuthOutcome.UNAVAILABLE),
    )
    authenticator = FallbackAuthenticator(
        SystemAuthenticator(TEST_USER),
        HashAuthenticator(TEST_USER, config.password_hash_path),
        True,
    )
    app2 = create_app(config, audit_enabled=True, authenticator=authenticator)
    with _make_client(app2) as client2:
        assert _login_on(client2).status_code == 204
        session = app2.state.session_store.get(client2.cookies.get("ch_session"))
        assert session.auth_mode == "password_hash"
        client2.headers["X-CSRF-Token"] = client2.cookies["ch_csrf"]

    audit = _audit(config)
    assert '"mode":"password_hash"' in audit


def test_pam_unavailable_no_fallback(config_with, monkeypatch):
    cfg = config_with(password_hash_fallback=False)
    monkeypatch.setattr(
        "app.authn.pam.authenticate",
        lambda username, password, **kw: pam.PamResult(pam.AuthOutcome.UNAVAILABLE),
    )
    authenticator = FallbackAuthenticator(
        SystemAuthenticator(TEST_USER),
        HashAuthenticator(TEST_USER, cfg.password_hash_path),
        False,
    )
    app2 = create_app(cfg, audit_enabled=True, authenticator=authenticator)
    with _make_client(app2) as client2:
        response = _login_on(client2)

    assert response.status_code == 401
    audit = _audit(cfg)
    assert '"reason":"unavailable"' in audit
    assert '"mode":"system"' in audit


def test_remote_admin_membership_uses_only_configured_principal(config):
    configured = replace(config, remote_admin_users=("pi",))
    store = SessionStore(config.session_ttl)

    listed = store.create("pi", auth_mode="password_hash")
    conventional_name = store.create("admin", auth_mode="password_hash")
    unlisted_system_user = store.create(TEST_USER, auth_mode="system")

    assert is_remote_admin(listed, configured)
    assert not is_remote_admin(conventional_name, configured)
    assert not is_remote_admin(unlisted_system_user, configured)
    assert not is_remote_admin(listed, replace(config, remote_admin_users=()))


def test_password_hash_mode_unchanged(config):
    cfg = replace(config, auth_mode="password_hash")
    app2 = create_app(cfg, audit_enabled=True)
    with _make_client(app2) as client2:
        assert _login_on(client2).status_code == 204
        session = app2.state.session_store.get(client2.cookies.get("ch_session"))
        assert session.auth_mode == "password_hash"
        client2.headers["X-CSRF-Token"] = client2.cookies["ch_csrf"]
        assert _login_on(client2, password="wrong", ip="192.168.1.61").status_code == 401
