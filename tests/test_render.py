"""部署模板渲染（设计文档 5.2 / F6 / A6）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tools"))

import render_config  # noqa: E402

sys.path.insert(0, str(PROJECT.parent.parent))
import pack_cloudhost  # noqa: E402

BASE_CONFIG = {
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
    "auth_mode": "system",
    "system_user": "pi",
    "system_home": "/home/pi",
    "file_root_mode": "home",
    "file_block_dotfiles": True,
    "password_hash_fallback": True,
    "admin_user": "admin",
    "password_hash_path": "/etc/cloudhost/secret/admin.hash",
    "log_dir": "/var/log/cloudhost",
    "state_dir": "/var/lib/cloudhost",
    "audit_log_path": "/var/log/cloudhost/audit.log",
    "max_upload_mb": 4096,
    "primary_iface": "",
    "ttyd_enabled": True,
    "filebrowser_noauth": True,
}


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(BASE_CONFIG, sort_keys=False), encoding="utf-8")
    return path


def render(
    config_file: Path, target_dir: Path, *, ttyd: bool = True, **overrides: object
) -> None:
    config = BASE_CONFIG.copy()
    config["ttyd_enabled"] = ttyd
    config.update(overrides)
    config_file.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    argv = [
        "render",
        "--config",
        str(config_file),
        "--caddyfile",
        str(target_dir / "Caddyfile"),
        "--systemd-dir",
        str(target_dir / "systemd"),
        "--fail2ban-dir",
        str(target_dir / "fail2ban"),
        "--logrotate-dir",
        str(target_dir / "logrotate"),
        "--filebrowser-config",
        str(target_dir / "filebrowser.json"),
        "--run-user",
        "pi",
    ]
    assert render_config.main(argv) == 0


def test_render_outputs_all_files(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    for name in (
        "Caddyfile",
        "filebrowser.json",
        "systemd/cloudhost-portal.service",
        "systemd/cloudhost-files.service",
        "systemd/cloudhost-ttyd.service",
        "fail2ban/filter.d/cloudhost-caddy.conf",
        "fail2ban/jail.d/cloudhost.conf",
        "logrotate/cloudhost",
    ):
        assert (tmp_path / name).is_file(), name


def test_caddyfile_has_gateway_auth(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")

    assert "auto_https off" in text
    assert "http://:80" in text
    assert "forward_auth 127.0.0.1:8080" in text
    assert "uri /auth/verify" in text
    # 终端 WebSocket 依赖 /auth/verify：子请求若带 Upgrade，uvicorn 会按
    # WebSocket 握手处理并返回 403，浏览器侧只看到 Press ⏎ to Reconnect。
    assert "header_up -Upgrade" in text
    assert "header_up -Connection" in text
    assert "/files/*" in text and "127.0.0.1:8081" in text
    assert "/term/*" in text and "127.0.0.1:7681" in text
    assert "/site/*" in text and "/home/pi/cloud/www" in text
    # v1.0 不允许出现 TLS / HSTS / 443
    assert "tls " not in text
    assert "Strict-Transport-Security" not in text
    assert ":443" not in text
    # 占位符必须全部替换
    assert "{{" not in text and "}}" not in text


def test_files_before_fallback(tmp_path: Path, config_file: Path):
    """route 顺序：/files 必须在兜底 reverse_proxy 之前。"""

    render(config_file, tmp_path)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")
    files_pos = text.index("@files path /files/*")
    fallback_pos = text.rindex("reverse_proxy 127.0.0.1:8080")
    assert files_pos < fallback_pos


def test_bare_entry_paths_redirect_to_slash(tmp_path: Path, config_file: Path):
    """``path /files/*`` 不匹配 ``/files``，必须显式 308 补斜杠。

    缺这一步时 ``/files``、``/site``、``/term`` 会落到门户兜底，
    浏览器拿到 FastAPI 的 ``{"detail":"Not Found"}``（用户报告的终端 404）。
    """

    render(config_file, tmp_path)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")
    fallback_pos = text.rindex("reverse_proxy 127.0.0.1:8080")
    for name in ("files", "site", "term"):
        marker = "@bare_%s path /%s" % (name, name)
        assert marker in text
        assert "redir @bare_%s /%s/ 308" % (name, name) in text
        assert text.index(marker) < fallback_pos


def test_dotfile_block_precedes_files(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")

    dotfiles_pos = text.index("@files_dotfiles")
    files_pos = text.index("@files path /files/*")
    fallback_pos = text.rindex("reverse_proxy 127.0.0.1:8080")
    assert dotfiles_pos < files_pos < fallback_pos


def test_dotfile_block_disabled(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path, file_block_dotfiles=False)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")

    assert "@files_dotfiles" not in text
    assert "点文件拦截已关闭" in text


def test_dotfile_regex_covers_api_and_encoding(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")

    assert "path_regexp dotfiles" in text
    assert "(?i)^/files/" in text
    assert "%2e" in text
    assert "respond \"Forbidden\" 403" in text


def test_file_root_cloud_rollback():
    cloud_config = dict(BASE_CONFIG, file_root_mode="cloud")
    assert render_config._files_root(cloud_config) == BASE_CONFIG["cloud_root"]
    home_config = dict(BASE_CONFIG, file_root_mode="home", system_home="/home/tris")
    assert render_config._files_root(home_config) == "/home/tris"


def _site_block(text: str) -> str:
    """截出 handle_path /site/* 对应的配置块。"""

    marker = "handle_path /site/* {"
    start = text.index(marker)
    end = text.index("\n\t\t}", start)
    return text[start:end]


def test_site_handler_public_by_default(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")
    site_block = _site_block(text)
    assert "file_server browse" in site_block
    # 公开站点不需要 forward_auth
    assert "forward_auth" not in site_block


def test_site_handler_private_when_configured(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path, site_public=False)
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")
    site_block = _site_block(text)
    assert "forward_auth 127.0.0.1:8080" in site_block


def test_ttyd_disabled(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path, ttyd=False)
    assert not (tmp_path / "systemd/cloudhost-ttyd.service").exists()
    text = (tmp_path / "Caddyfile").read_text(encoding="utf-8")
    assert "ttyd_enabled=false" in text


def test_systemd_units(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    portal = (tmp_path / "systemd/cloudhost-portal.service").read_text(encoding="utf-8")
    assert "Restart=on-failure" in portal
    assert "RestartSec=3" in portal
    assert "StartLimitBurst=5" in portal
    assert "User=pi" in portal
    assert "CLOUDHOST_CONFIG=/etc/cloudhost/config.yaml" in portal
    assert "MemoryMax=150M" in portal

    files = (tmp_path / "systemd/cloudhost-files.service").read_text(encoding="utf-8")
    assert "/opt/cloudhost/bin/filebrowser" in files
    assert "filebrowser.json" in files


def test_fail2ban_matches_document_limits(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    jail = (tmp_path / "fail2ban/jail.d/cloudhost.conf").read_text(encoding="utf-8")
    assert "maxretry = 5" in jail
    assert "findtime = 300" in jail
    assert "bantime  = 600" in jail
    assert "/var/log/cloudhost/caddy-access.log" in jail


def test_filebrowser_config(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    payload = json.loads((tmp_path / "filebrowser.json").read_text(encoding="utf-8"))
    assert payload["port"] == 8081
    assert payload["address"] == "127.0.0.1"
    assert payload["baseURL"] == "/files"
    assert payload["root"] == "/home/pi"


def test_files_root_in_units(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    portal = (tmp_path / "systemd/cloudhost-portal.service").read_text(encoding="utf-8")
    files = (tmp_path / "systemd/cloudhost-files.service").read_text(encoding="utf-8")
    ttyd = (tmp_path / "systemd/cloudhost-ttyd.service").read_text(encoding="utf-8")

    assert "ReadWritePaths=/var/log/cloudhost /var/lib/cloudhost /home/pi/cloud /home/pi" in portal
    assert "WorkingDirectory=/home/pi" in files
    assert "ReadWritePaths=/home/pi /var/lib/cloudhost/filebrowser /var/log/cloudhost" in files
    assert "WorkingDirectory=/home/pi" in ttyd
    assert "ReadWritePaths=/home/pi /var/log/cloudhost" in ttyd


def test_filebrowser_root_is_home(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    payload = json.loads((tmp_path / "filebrowser.json").read_text(encoding="utf-8"))
    assert payload["root"] == "/home/pi"
    assert payload["root"] != "/home/pi/cloud"


def test_pam_conf_installed():
    conf = PROJECT / "deploy" / "pam-cloudhost.conf"
    assert conf.is_file()
    text = conf.read_text(encoding="utf-8")
    assert "auth     required   pam_unix.so" in text
    assert "account  required   pam_unix.so" in text
    # 注释里可以说明「为什么不用」，但不能有真正的 pam_faillock 规则。
    directives = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    assert all("faillock" not in line for line in directives)


def test_ttyd_binary_path(tmp_path: Path, config_file: Path):
    render(config_file, tmp_path)
    text = (tmp_path / "systemd/cloudhost-ttyd.service").read_text(encoding="utf-8")
    assert "/opt/cloudhost/bin/ttyd" in text
    assert "/usr/bin/ttyd" not in text


def test_filebrowser_config_is_readable_by_run_user(tmp_path: Path, config_file: Path):
    """回归：filebrowser.json 由 root 渲染，但 filebrowser 以运行用户启动。

    历史故障：文件是 root:root 0640，filebrowser 读不到 --config，退回默认
    8080 端口后退出 1，Caddy 对 /files/ 返回 502。
    """

    render(config_file, tmp_path)
    target = tmp_path / "filebrowser.json"
    if os.name != "nt":
        assert (target.stat().st_mode & 0o777) == 0o640
    setup = render_config.write_filebrowser_config(BASE_CONFIG, target, "pi")
    if os.name != "nt":
        assert any("chown root:pi" in cmd for cmd in setup)
        assert any("chmod 0640" in cmd for cmd in setup)


def test_install_fixes_filebrowser_config_ownership():
    """install.sh 在 render 之后必须修正属组，否则 /files/ 必然 502。"""

    text = (PROJECT / "install.sh").read_text(encoding="utf-8")
    assert 'chown root:"${RUN_USER}" "${ETC_DIR}/filebrowser.json"' in text
    assert 'chmod 0640 "${ETC_DIR}/filebrowser.json"' in text
    assert "--run-user" in text


def test_generate_config_records_ssh_user(tmp_path: Path):
    out = tmp_path / "etc" / "config.yaml"
    argv = [
        "config",
        "--output",
        str(out),
        "--home",
        str(tmp_path / "home"),
        "--run-user",
        "tris",
    ]
    assert render_config.main(argv) == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["ssh_user"] == "tris"
    assert data["ssh_port"] == 22

    # 旧配置缺键时只补齐，不覆盖已有值
    raw = out.read_text(encoding="utf-8").replace("ssh_user: tris\n", "")
    out.write_text(raw, encoding="utf-8")
    assert render_config.main(argv) == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["ssh_user"] == "tris"


def test_config_upgrade_migrates_legacy_zerotier_without_enabling_remote(tmp_path: Path):
    out = tmp_path / "etc" / "config.yaml"
    out.parent.mkdir()
    out.write_text(
        "access_mode: lan\n"
        "cloud_root: /home/pi/cloud\n"
        "site_dir: /home/pi/cloud/www\n"
        "zerotier_network_id: '0123456789012345'\n",
        encoding="utf-8",
    )
    argv = [
        "config",
        "--output",
        str(out),
        "--home",
        "/home/pi",
        "--run-user",
        "pi",
    ]
    assert render_config.main(argv) == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["remote_zerotier_network_id"] == "0123456789012345"
    assert data["remote_enabled"] is False
    assert data["remote_provider"] == "none"
    assert "remote_admin_users" not in data


@pytest.mark.parametrize(
    "auth_mode,expected,system_user,admin_user",
    [
        ("system", ["pi"], "pi", "portal-admin"),
        ("password_hash", ["portal-admin"], "pi", "portal-admin"),
    ],
)
def test_remote_admin_first_initialization_uses_effective_auth_user(
    tmp_path: Path, auth_mode: str, expected: list[str], system_user: str, admin_user: str
):
    target = tmp_path / "config.yaml"
    payload = dict(BASE_CONFIG, auth_mode=auth_mode, system_user=system_user, admin_user=admin_user)
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    assert render_config.main(
        [
            "remote-admins",
            "--config",
            str(target),
            "--auth-mode",
            auth_mode,
            "--system-user",
            system_user,
            "--admin-user",
            admin_user,
        ]
    ) == 0
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["remote_admin_users"] == expected


@pytest.mark.parametrize("existing", [[], ["existing-admin"]])
def test_remote_admin_upgrade_preserves_existing_list(tmp_path: Path, existing: list[str]):
    target = tmp_path / "config.yaml"
    target.write_text(
        yaml.safe_dump(dict(BASE_CONFIG, remote_admin_users=existing), sort_keys=False),
        encoding="utf-8",
    )
    before = target.read_text(encoding="utf-8")

    assert not render_config.initialize_remote_admin_users(
        target, system_user="new-system-user", admin_user="new-admin"
    )
    assert yaml.safe_load(target.read_text(encoding="utf-8"))["remote_admin_users"] == existing
    assert target.read_text(encoding="utf-8") == before


def test_remote_admin_initializer_rejects_unfinalized_auth_mode(tmp_path: Path):
    target = tmp_path / "config.yaml"
    target.write_text(yaml.safe_dump(BASE_CONFIG, sort_keys=False), encoding="utf-8")
    with pytest.raises(SystemExit, match="认证模式"):
        render_config.initialize_remote_admin_users(
            target,
            effective_auth_mode="password_hash",
            system_user="pi",
            admin_user="portal-admin",
        )
    assert "remote_admin_users" not in yaml.safe_load(target.read_text(encoding="utf-8"))


@pytest.mark.parametrize("contents", ["not: [valid", "- not-a-mapping"])
def test_remote_admin_initializer_fails_on_malformed_config(tmp_path: Path, contents: str):
    target = tmp_path / "config.yaml"
    target.write_text(contents, encoding="utf-8")
    with pytest.raises(SystemExit, match="配置"):
        render_config.initialize_remote_admin_users(target)


def test_remote_admin_initializer_fails_on_unreadable_config(tmp_path: Path):
    with pytest.raises(SystemExit, match="配置"):
        render_config.initialize_remote_admin_users(tmp_path / "missing.yaml")


def test_installer_initializes_remote_admin_after_effective_mode_before_restart():
    text = (PROJECT / "install.sh").read_text(encoding="utf-8")
    mode_finalized = text.index('sed -i "s/^auth_mode:.*/auth_mode: ${PAM_MODE}/')
    initialize = text.index('"${RENDER[@]}" remote-admins')
    restart = text.index("systemctl restart cloudhost-portal.service")
    assert mode_finalized < initialize < restart
    assert text.count("systemctl restart cloudhost-portal.service") == 1
    assert '--auth-mode "${PAM_MODE}"' in text[initialize:restart]


def test_leftover_placeholder_detected(tmp_path: Path):
    template = tmp_path / "x.tmpl"
    template.write_text("hello {{UNKNOWN}}", encoding="utf-8")
    with pytest.raises(SystemExit):
        render_config.render_template(template, tmp_path / "out", {"OTHER": "x"})


def test_generate_config(tmp_path: Path):
    out = tmp_path / "etc" / "config.yaml"
    argv = [
        "config",
        "--output",
        str(out),
        "--home",
        str(tmp_path / "home"),
        "--admin-user",
        "tris",
    ]
    assert render_config.main(argv) == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["cloud_root"].endswith("/home/cloud")
    assert data["site_dir"].endswith("/home/cloud/www")
    assert data["admin_user"] == "tris"
    assert data["tls_mode"] == "off"
    assert data["access_mode"] == "lan"

    # 二次执行不覆盖
    before = out.read_text(encoding="utf-8")
    assert render_config.main(argv) == 0
    assert out.read_text(encoding="utf-8") == before


def test_pack_normalizes_crlf_for_text_files(tmp_path: Path):
    """Windows 工作区的 CRLF 不能带到板端。

    历史故障：deploy/pam-cloudhost.conf 在 Windows 上是 CRLF，打包后
    /etc/pam.d/cloudhost 每行带 \r，PAM 报「illegal module type」并
    「adding faulty module: pam_unix.so」，系统密码永远校验失败。
    """

    conf = tmp_path / "pam-cloudhost.conf"
    conf.write_bytes(b"auth     required   pam_unix.so\r\naccount  required   pam_unix.so\r\n")
    payload = pack_cloudhost._payload(conf)
    assert b"\r" not in payload
    assert payload == b"auth     required   pam_unix.so\naccount  required   pam_unix.so\n"

    # 非文本文件保持原样
    binary = tmp_path / "ttyd"
    binary.write_bytes(b"\x7fELF\r\n\x00")
    assert pack_cloudhost._payload(binary) == b"\x7fELF\r\n\x00"


def test_pam_conf_is_lf_only():
    """仓库里的 pam 配置必须是 LF，否则板端 PAM 直接不可用。"""

    raw = (PROJECT / "deploy" / "pam-cloudhost.conf").read_bytes()
    assert b"\r" not in raw
