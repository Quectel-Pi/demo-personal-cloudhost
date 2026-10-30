"""文件接口与路径穿越防护（设计文档 F3 / A2 / 11.1）。"""

from __future__ import annotations

import hashlib
import os
import urllib.parse
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.errors import ApiError
from app.files import is_dotfile_path, resolve_cloud_path, validate_filename
from app.main import create_app
from conftest import TEST_PASSWORD, TEST_USER


def test_resolve_allows_normal_paths(cloud_root: Path):
    assert resolve_cloud_path(cloud_root, "") == cloud_root.resolve()
    assert resolve_cloud_path(cloud_root, "note.txt") == (cloud_root / "note.txt").resolve()
    assert resolve_cloud_path(cloud_root, "/docs/readme.md").name == "readme.md"


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "docs/../../escape", "..", "a/../../b", "/../../etc/shadow"],
)
def test_resolve_blocks_traversal(cloud_root: Path, bad: str):
    with pytest.raises(ApiError) as exc:
        resolve_cloud_path(cloud_root, bad)
    assert exc.value.code == "E1003"


def test_resolve_blocks_symlink_escape(tmp_path: Path, cloud_root: Path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret", encoding="utf-8")
    link = cloud_root / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限
        pytest.skip("当前系统不支持创建符号链接")
    with pytest.raises(ApiError) as exc:
        resolve_cloud_path(cloud_root, "link.txt")
    assert exc.value.code == "E1003"


def test_resolve_rejects_nul_byte(cloud_root: Path):
    with pytest.raises(ApiError):
        resolve_cloud_path(cloud_root, "a\x00b")


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "x" * 300])
def test_validate_filename_rejects_bad_names(name: str):
    with pytest.raises(ApiError):
        validate_filename(name)


def test_validate_filename_accepts_normal(name="报告-v1.2.txt"):
    assert validate_filename(name) == name


def test_list_root(authed):
    response = authed.get("/api/files")
    assert response.status_code == 200
    payload = response.json()
    names = {entry["name"] for entry in payload["entries"]}
    assert {"note.txt", "docs", "www", "upload"} <= names
    assert payload["path"] == "/"


def test_download_and_checksum(authed, cloud_root: Path):
    response = authed.get("/api/files/download", params={"path": "note.txt"})
    assert response.status_code == 200
    assert response.content == (cloud_root / "note.txt").read_bytes()


def test_download_traversal_rejected(authed):
    response = authed.get("/api/files/download", params={"path": "../config.yaml"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_upload_requires_csrf(client, login):
    assert login().status_code == 204
    response = client.post(
        "/api/files/upload", params={"path": "", "name": "x.bin"}, content=b"data"
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_upload_download_sha256(authed, cloud_root: Path):
    payload = os.urandom(512 * 1024)
    digest_before = hashlib.sha256(payload).hexdigest()

    response = authed.post(
        "/api/files/upload",
        params={"path": "", "name": "blob.bin"},
        content=payload,
    )
    assert response.status_code == 201, response.text
    assert response.json()["size"] == len(payload)

    saved = cloud_root / "blob.bin"
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == digest_before

    download = authed.get("/api/files/download", params={"path": "blob.bin"})
    assert hashlib.sha256(download.content).hexdigest() == digest_before


def test_upload_existing_requires_overwrite(authed):
    first = authed.post(
        "/api/files/upload", params={"path": "", "name": "dup.txt"}, content=b"a"
    )
    assert first.status_code == 201
    second = authed.post(
        "/api/files/upload", params={"path": "", "name": "dup.txt"}, content=b"b"
    )
    assert second.status_code == 400
    third = authed.post(
        "/api/files/upload",
        params={"path": "", "name": "dup.txt", "overwrite": "true"},
        content=b"c",
    )
    assert third.status_code == 201


def test_upload_exceeds_limit(authed, config):
    limit = config.max_upload_mb * 1024 * 1024
    response = authed.post(
        "/api/files/upload",
        params={"path": "", "name": "big.bin"},
        content=b"x" * (limit + 1024),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E1001"


def test_mkdir_and_rename(authed, cloud_root: Path):
    created = authed.post("/api/files/mkdir", params={"path": "", "name": "newdir"})
    assert created.status_code == 201
    assert (cloud_root / "newdir").is_dir()

    renamed = authed.post(
        "/api/files/rename", params={"path": "newdir", "name": "renamed"}
    )
    assert renamed.status_code == 200
    assert (cloud_root / "renamed").is_dir()
    assert not (cloud_root / "newdir").exists()


def test_delete(authed, cloud_root: Path):
    target = cloud_root / "tmp.txt"
    target.write_text("x", encoding="utf-8")
    response = authed.delete("/api/files/delete", params={"path": "tmp.txt"})
    assert response.status_code == 204
    assert not target.exists()

    audit = authed.app.state.config.audit_log_path.read_text(encoding="utf-8")
    assert "file_delete" in audit


def test_delete_cloud_root_rejected(authed):
    response = authed.delete("/api/files/delete", params={"path": "/"})
    assert response.status_code == 403


def test_preview_text(authed):
    response = authed.get("/api/files/preview", params={"path": "note.txt"})
    assert response.status_code == 200
    assert "hello cloudhost" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


def test_files_api_requires_login(client):
    assert client.get("/api/files").status_code == 401
    assert client.get("/api/files/download", params={"path": "note.txt"}).status_code == 401


# --------------------------------------------------------------------------- #
# 6.4 文件入口根目录跟随 files_root
# --------------------------------------------------------------------------- #
def test_files_api_uses_system_home(config_with, home_root: Path, fake_auth):
    """home 模式下 /api/files 以 system_home 为根，而不是 cloud_root。"""

    cfg = config_with(system_home=home_root, file_root_mode="home")
    app = create_app(cfg, audit_enabled=True, authenticator=fake_auth)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD}
        )
        assert login.status_code == 204

        payload = client.get("/api/files").json()
        assert payload["path"] == "/"
        names = {entry["name"] for entry in payload["entries"]}
        assert {"notes.txt", "cloud"} <= names
        assert "www" not in names  # cloud_root 的内容不在 home 根下直接出现

        download = client.get("/api/files/download", params={"path": "notes.txt"})
        assert download.status_code == 200
        assert download.content.strip() == b"hello home"

        config = client.get("/api/config").json()
        assert config["files_root"] == str(home_root)
        assert config["files_root_mode"] == "home"
        assert config["cloud_root"] == str(cfg.cloud_root)


# --------------------------------------------------------------------------- #
# 6.5.1 点文件在门户内置 API 也拦截
# --------------------------------------------------------------------------- #
def _home_cfg(config_with, home_root, *, block: bool = True):
    return config_with(
        system_home=home_root, file_root_mode="home", file_block_dotfiles=block
    )


def _open_home_client(cfg, authenticator):
    app = create_app(cfg, audit_enabled=True, authenticator=authenticator)
    return TestClient(app, client=("127.0.0.1", 50000))


def _login_home(client):
    response = client.post(
        "/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert response.status_code == 204
    client.headers.update({"X-CSRF-Token": client.cookies.get("ch_csrf")})
    return client


@pytest.fixture
def home_client(config_with, home_root: Path, fake_auth):
    with _open_home_client(_home_cfg(config_with, home_root), fake_auth) as client:
        yield _login_home(client)


@pytest.fixture
def home_client_allow_dotfiles(config_with, home_root: Path, fake_auth):
    with _open_home_client(
        _home_cfg(config_with, home_root, block=False), fake_auth
    ) as client:
        yield _login_home(client)


def test_api_list_hides_dotfiles(home_client):
    payload = home_client.get("/api/files").json()
    names = {entry["name"] for entry in payload["entries"]}
    assert ".ssh" not in names
    assert "notes.txt" in names


def test_api_download_dotfile_forbidden(home_client):
    response = home_client.get("/api/files/download", params={"path": ".ssh/id_ed25519"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_api_preview_dotfile_forbidden(home_client):
    response = home_client.get("/api/files/preview", params={"path": ".ssh/id_ed25519"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_api_upload_dotfile_name_forbidden(home_client):
    response = home_client.post(
        "/api/files/upload",
        params={"path": ""},
        headers={"X-Filename": ".bashrc"},
        content=b"evil",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_api_mkdir_dotfile_forbidden(home_client):
    response = home_client.post("/api/files/mkdir", params={"path": "", "name": ".trash"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E1003"


def test_api_stat_dotfile_forbidden(home_client):
    response = home_client.get("/api/files/stat", params={"path": ".ssh"})
    assert response.status_code == 403


def test_dotfile_check_can_be_disabled(home_client_allow_dotfiles):
    response = home_client_allow_dotfiles.get(
        "/api/files/download", params={"path": ".ssh/id_ed25519"}
    )
    assert response.status_code == 200
    assert response.content == b"SECRET"


@pytest.mark.parametrize("value", ["/.ssh", ".gnupg/key", "a/.b/c", "\\a\\.b"])
def test_dotfile_check_ignores_nested_and_special_true(value: str):
    assert is_dotfile_path(value) is True


@pytest.mark.parametrize("value", ["a/b.c", "..", ".", "a/..b", "a/b.", "", "docs/readme.md"])
def test_dotfile_check_ignores_nested_and_special_false(value: str):
    assert is_dotfile_path(value) is False
