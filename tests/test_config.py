"""配置校验（设计文档第 8 章）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import ConfigError, load_config

BASE = {
    "access_mode": "lan",
    "listen_port": 80,
    "tls_mode": "off",
    "portal_port": 8080,
    "filebrowser_port": 8081,
    "ttyd_port": 7681,
    "cloud_root": "/home/pi/cloud",
    "site_dir": "/home/pi/cloud/www",
    "site_public": True,
    "session_ttl": 3600,
    "session_secret_path": "/etc/cloudhost/secret/session.key",
    "login_max_attempts": 5,
    "login_window_sec": 300,
    "fail2ban_bantime": 600,
    "status_interval_ms": 1000,
    "ssh_key_only": True,
    "zerotier_network_id": "",
    "system_home": "/home/pi",
}


def write(tmp_path: Path, **overrides) -> Path:
    payload = dict(BASE)
    payload.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_defaults_match_document(tmp_path: Path):
    cfg = load_config(write(tmp_path))
    assert cfg.access_mode == "lan"
    assert cfg.tls_mode == "off"
    assert cfg.listen_port == 80
    assert cfg.portal_port == 8080
    assert cfg.filebrowser_port == 8081
    assert cfg.ttyd_port == 7681
    assert cfg.session_ttl == 3600
    assert cfg.login_max_attempts == 5
    assert cfg.login_window_sec == 300
    assert cfg.fail2ban_bantime == 600
    assert cfg.status_interval_ms == 1000
    assert cfg.site_public is True
    assert cfg.ssh_key_only is True
    # 实现补充项默认值
    assert cfg.admin_user == "admin"
    assert cfg.max_upload_mb == 4096
    # SSH 入口展示字段
    assert cfg.ssh_user == "pi"
    assert cfg.ssh_port == 22
    assert cfg.remote_enabled is False
    assert cfg.remote_provider == "none"
    assert cfg.remote_zerotier_network_id == ""
    assert cfg.remote_peanut_https_url == ""
    assert cfg.remote_ssh_host == ""
    assert cfg.remote_ssh_port == 22
    assert cfg.remote_config_revision == 1
    assert cfg.remote_admin_users == ()


def test_ssh_entry_fields(tmp_path: Path):
    cfg = load_config(write(tmp_path, ssh_user="tris", ssh_port=2222))
    assert cfg.ssh_user == "tris"
    assert cfg.ssh_port == 2222

    with pytest.raises(ConfigError, match="ssh_port"):
        load_config(write(tmp_path, ssh_port=0))


@pytest.mark.parametrize(
    "host",
    [
        "https://ssh.example.net", "ssh.example.net:22", "ssh.example.net/path",
        "localhost", "host.local", "192.168.1.10", "bad host", "a..example.net",
    ],
)
def test_public_ssh_host_requires_a_public_host_only(tmp_path: Path, host: str):
    with pytest.raises(ConfigError, match="remote_ssh_host"):
        load_config(write(tmp_path, remote_ssh_host=host))


def test_public_ssh_port_is_optional_but_requires_a_host(tmp_path: Path):
    cfg = load_config(write(
        tmp_path, remote_ssh_host="ssh.example.net", remote_ssh_port=41042
    ))
    assert cfg.remote_ssh_host == "ssh.example.net"
    assert cfg.remote_ssh_port == 41042
    with pytest.raises(ConfigError, match="remote_ssh_port"):
        load_config(write(tmp_path, remote_ssh_port=41042))
    assert load_config(write(tmp_path, remote_ssh_port=22)).remote_ssh_host == ""
    with pytest.raises(ConfigError, match="remote_ssh_port"):
        load_config(write(
            tmp_path, remote_ssh_host="ssh.example.net", remote_ssh_port=65536
        ))


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_config(tmp_path / "nope.yaml")


def test_illegal_acl_mode(tmp_path: Path):
    with pytest.raises(ConfigError, match="access_mode"):
        load_config(write(tmp_path, access_mode="frp"))


def test_illegal_tls_mode(tmp_path: Path):
    with pytest.raises(ConfigError, match="tls_mode"):
        load_config(write(tmp_path, tls_mode="letsencrypt"))


@pytest.mark.parametrize(
    "key,value",
    [
        ("listen_port", 0),
        ("listen_port", 70000),
        ("portal_port", 0),
        ("session_ttl", 100),
        ("session_ttl", 90000),
        ("login_max_attempts", 2),
        ("login_window_sec", 10),
        ("status_interval_ms", 100),
        ("fail2ban_bantime", 30),
    ],
)
def test_out_of_range(tmp_path: Path, key: str, value: int):
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, **{key: value}))


def test_cloud_root_cannot_be_filesystem_root(tmp_path: Path):
    with pytest.raises(ConfigError, match="根目录"):
        load_config(write(tmp_path, cloud_root="/", site_dir="/www"))


def test_site_dir_must_be_inside_cloud_root(tmp_path: Path):
    with pytest.raises(ConfigError, match="site_dir"):
        load_config(write(tmp_path, site_dir="/tmp/www"))


def test_relative_path_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="绝对路径"):
        load_config(write(tmp_path, cloud_root="cloud"))


def test_zerotier_id_validation(tmp_path: Path):
    cfg = load_config(write(tmp_path, zerotier_network_id="a09acf02337b0578"))
    assert cfg.zerotier_network_id == "a09acf02337b0578"

    with pytest.raises(ConfigError, match="zerotier_network_id"):
        load_config(write(tmp_path, zerotier_network_id="not-a-network-id"))


def test_legacy_zerotier_id_migrates_as_configuration_only(tmp_path: Path):
    cfg = load_config(write(tmp_path, zerotier_network_id="a09acf02337b0578"))
    assert cfg.remote_zerotier_network_id == "a09acf02337b0578"
    assert cfg.remote_enabled is False
    assert cfg.remote_provider == "none"

    explicit_empty = load_config(
        write(
            tmp_path,
            zerotier_network_id="a09acf02337b0578",
            remote_zerotier_network_id="",
        )
    )
    assert explicit_empty.remote_zerotier_network_id == ""


def test_unknown_remote_config_key_is_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="remote_secret"):
        load_config(write(tmp_path, remote_secret="unexpected"))


@pytest.mark.parametrize(
    "override,match",
    [
        ({"remote_enabled": "true"}, "remote_enabled"),
        ({"remote_provider": "oray"}, "remote_provider"),
        ({"remote_zerotier_network_id": "bad"}, "remote_zerotier_network_id"),
        ({"remote_config_revision": 0}, "remote_config_revision"),
        ({"remote_config_revision": "2"}, "remote_config_revision"),
        ({"remote_admin_users": "pi"}, "remote_admin_users"),
        ({"remote_admin_users": ["has spaces"]}, "remote_admin_users"),
    ],
)
def test_remote_config_values_are_strict(tmp_path: Path, override, match: str):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, **override))


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/",
        "https://user:secret@example.org/",
        "https://example.org/path",
        "https://example.org/?next=/",
        "https://example.org/#fragment",
        "https://example.org/\n",
        "https://localhost/",
        "https://host.local/",
        "https://127.0.0.1/",
        "https://192.0.2.1/",
        "https://127.1/",
        "https://0x7f.0.0.1/",
        "https://example.org:0/",
        "https://example.org:/",
    ],
)
def test_peanut_url_rejects_noncompliant_values(tmp_path: Path, url: str):
    with pytest.raises(ConfigError, match="remote_peanut_https_url"):
        load_config(write(tmp_path, remote_peanut_https_url=url))


def test_peanut_url_accepts_https_root_without_domain_allowlist(tmp_path: Path):
    cfg = load_config(write(tmp_path, remote_peanut_https_url="https://hsk.example.org:8443/"))
    assert cfg.remote_peanut_https_url == "https://hsk.example.org:8443/"


def test_bool_accepts_string_forms(tmp_path: Path):
    cfg = load_config(write(tmp_path, site_public="false", ssh_key_only="yes"))
    assert cfg.site_public is False
    assert cfg.ssh_key_only is True


def test_auth_and_file_defaults(tmp_path: Path):
    cfg = load_config(write(tmp_path))
    assert cfg.auth_mode == "system"
    assert cfg.file_root_mode == "home"
    assert cfg.file_block_dotfiles is True
    assert cfg.password_hash_fallback is True
    assert cfg.system_user == "pi"  # 未配置时回落到 ssh_user
    assert cfg.system_home == Path("/home/pi")


def test_files_root_derivation(tmp_path: Path):
    home_cfg = load_config(write(tmp_path, system_home="/home/tris"))
    assert home_cfg.files_root == Path("/home/tris")

    cloud_cfg = load_config(write(tmp_path, file_root_mode="cloud"))
    assert cloud_cfg.files_root == cloud_cfg.cloud_root


def test_system_home_required_in_home_mode(tmp_path: Path):
    with pytest.raises(ConfigError, match="system_home"):
        load_config(write(tmp_path, system_home=""))


def test_invalid_auth_mode(tmp_path: Path):
    with pytest.raises(ConfigError, match="auth_mode"):
        load_config(write(tmp_path, auth_mode="ldap"))


def test_invalid_file_root_mode(tmp_path: Path):
    with pytest.raises(ConfigError, match="file_root_mode"):
        load_config(write(tmp_path, file_root_mode="root"))


def test_system_home_must_be_absolute(tmp_path: Path):
    with pytest.raises(ConfigError, match="绝对路径"):
        load_config(write(tmp_path, system_home="home/pi"))


def test_unknown_keys_are_ignored(tmp_path: Path):
    cfg = load_config(write(tmp_path, unknown_key="x"))
    assert cfg.listen_port == 80
