"""测试夹具：在临时目录里构造完整的 Config 与已登录客户端。"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT / "portal"))

from app.authn import AuthResult  # noqa: E402
from app.config import Config  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import ServiceManager  # noqa: E402
import subprocess

TEST_PASSWORD = "correct-horse-battery"
TEST_USER = "admin"


@pytest.fixture
def cloud_root(tmp_path: Path) -> Path:
    root = tmp_path / "cloud"
    (root / "www").mkdir(parents=True)
    (root / "upload").mkdir()
    (root / "note.txt").write_text("hello cloudhost\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "readme.md").write_text("# docs\n", encoding="utf-8")
    return root


@pytest.fixture
def home_root(tmp_path: Path, cloud_root: Path) -> Path:
    """模拟运行用户 home：含点文件、普通文件与 cloud 子目录。"""

    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("SECRET", encoding="utf-8")
    (home / "notes.txt").write_text("hello home\n", encoding="utf-8")
    (home / "cloud").mkdir()
    return home


@pytest.fixture
def config(tmp_path: Path, cloud_root: Path) -> Config:
    secret_dir = tmp_path / "etc" / "secret"
    secret_dir.mkdir(parents=True)
    log_dir = tmp_path / "log"
    state_dir = tmp_path / "state"
    config_path = tmp_path / "etc" / "config.yaml"

    hash_path = secret_dir / "admin.hash"
    hash_path.write_text(hash_password(TEST_PASSWORD) + "\n", encoding="utf-8")

    return Config(
        config_path=config_path,
        access_mode="lan",
        listen_port=18080,
        tls_mode="off",
        portal_port=8080,
        filebrowser_port=8081,
        ttyd_port=7681,
        cloud_root=cloud_root,
        site_dir=cloud_root / "www",
        site_public=True,
        session_ttl=3600,
        session_secret_path=secret_dir / "session.key",
        login_max_attempts=5,
        login_window_sec=300,
        fail2ban_bantime=600,
        status_interval_ms=1000,
        ssh_key_only=True,
        zerotier_network_id="",
        admin_user=TEST_USER,
        password_hash_path=hash_path,
        log_dir=log_dir,
        state_dir=state_dir,
        audit_log_path=log_dir / "audit.log",
        max_upload_mb=8,
        primary_iface=None,
        ttyd_enabled=True,
        ssh_user="pi",
        ssh_port=22,
        auth_mode="system",
        system_user=TEST_USER,
        # 默认把文件入口根目录指向 cloud_root，保证既有文件接口测试
        # 继续使用同一棵目录树；home 模式另有 home_root fixture。
        system_home=cloud_root,
        file_root_mode="home",
        file_block_dotfiles=True,
        password_hash_fallback=True,
    )


class FakeAuthenticator:
    """可编程认证器；默认沿用 admin.hash 的账号/密码语义。"""

    mode = "system"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result: AuthResult | None = None

    def authenticate(self, username: str, password: str) -> AuthResult:
        self.calls.append((username, password))
        if self.result is not None:
            return self.result
        ok = username.strip() == TEST_USER and password == TEST_PASSWORD
        return AuthResult(ok, TEST_USER, "ok" if ok else "denied", self.mode)


@pytest.fixture
def fake_auth() -> FakeAuthenticator:
    return FakeAuthenticator()


@pytest.fixture
def app(config: Config, fake_auth: FakeAuthenticator):
    application = create_app(config, audit_enabled=True, authenticator=fake_auth)
    application.state.service_manager = ServiceManager(
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "ActiveState=inactive\nUnitFileState=disabled\n", ""
        )
    )
    return application


@pytest.fixture
def client(app):
    """默认以回环地址作为直连来源，模拟 Caddy 内部调用 /auth/verify 的场景。"""

    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50000)) as test_client:
        yield test_client


@pytest.fixture
def login(client):
    def _login(password: str = TEST_PASSWORD, user: str = TEST_USER):
        return client.post(
            "/auth/login",
            data={"username": user, "password": password},
            headers={"X-Forwarded-For": "192.168.1.50"},
        )

    return _login


@pytest.fixture
def authed(client, login):
    response = login()
    assert response.status_code == 204, response.text
    token = client.cookies.get("ch_csrf")
    assert token
    client.headers.update({"X-CSRF-Token": token})
    return client


@pytest.fixture
def config_with(tmp_path: Path, config: Config):
    """派生配置：config_with(login_max_attempts=3)"""

    def _build(**changes):
        new = replace(config, **changes)
        return new

    return _build
