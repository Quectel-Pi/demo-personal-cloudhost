#!/usr/bin/env python3
"""部署模板渲染与初始化工具（供 install.sh 调用）。

子命令：

``config``   生成 ``/etc/cloudhost/config.yaml``（已存在且未 --force 时跳过）
``hash``     读取 stdin 的密码，输出哈希（argon2 / bcrypt / pbkdf2）
``session``  生成 48 字节会话密钥文件（权限 600）
``render``   渲染 deploy/*.tmpl 到目标路径

只依赖标准库 + PyYAML（板端由 install.sh 安装）；密码哈希优先使用
venv 内的 argon2-cffi，缺失时自动降级。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("[render_config] 需要 PyYAML：pip install pyyaml", file=sys.stderr)
    raise SystemExit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
PORTAL_DIR = REPO_ROOT / "portal"


# --------------------------------------------------------------------------- #
# 配置生成
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: dict[str, object] = {
    "access_mode": "lan",
    "listen_port": 80,
    "tls_mode": "off",
    "portal_port": 8080,
    "filebrowser_port": 8081,
    "ttyd_port": 7681,
    "cloud_root": "",  # 运行时按目标用户 home 填充
    "site_dir": "",
    "site_public": True,
    "session_ttl": 3600,
    "session_secret_path": "/etc/cloudhost/secret/session.key",
    "login_max_attempts": 5,
    "login_window_sec": 300,
    "fail2ban_bantime": 600,
    "status_interval_ms": 1000,
    "ssh_key_only": True,
    "zerotier_network_id": "",
    "remote_enabled": False,
    "remote_provider": "none",
    "remote_zerotier_network_id": "",
    "remote_peanut_https_url": "",
    "remote_ssh_host": "",
    "remote_ssh_port": 22,
    "remote_config_revision": 1,
    # 实现补充项
    "admin_user": "admin",
    "password_hash_path": "/etc/cloudhost/secret/admin.hash",
    "log_dir": "/var/log/cloudhost",
    "state_dir": "/var/lib/cloudhost",
    "audit_log_path": "/var/log/cloudhost/audit.log",
    "max_upload_mb": 4096,
    "primary_iface": "",
    "ttyd_enabled": True,
    # SSH 入口展示用（门户首页的「SSH」卡片）；端口 22 时不显示 -p 参数
    "ssh_user": "pi",
    "ssh_port": 22,
    # 系统账号登录 / 文件根目录（v1.1）
    "auth_mode": "system",
    "system_user": "",  # 运行时按 --run-user 填充
    "system_home": "",  # 运行时按 --system-home / pwd 填充
    "file_root_mode": "home",
    "file_block_dotfiles": True,
    "password_hash_fallback": True,
    # File Browser 自身匿名访问开关：true 表示由 Caddy forward_auth 独占鉴权，
    # 避免出现「门户登录一次、文件页再登录一次」的双重登录（见 README 实现说明）。
    "filebrowser_noauth": True,
}


def build_config(args: argparse.Namespace) -> dict[str, object]:
    data = dict(DEFAULT_CONFIG)
    home = Path(args.home).expanduser()
    cloud_root = Path(args.cloud_root or (home / "cloud"))
    # 目标平台是 Linux：统一输出 POSIX 风格路径，避免在其它平台开发时写出反斜杠
    data.update(
        {
            "cloud_root": cloud_root.as_posix(),
            "site_dir": (cloud_root / "www").as_posix(),
            "admin_user": args.admin_user,
            "primary_iface": args.primary_iface or "",
            "ttyd_enabled": not args.no_ttyd,
            "ssh_user": args.run_user,
            "system_user": args.run_user,
            "system_home": (getattr(args, "system_home", "") or _home_of(args.run_user)),
            "file_root_mode": getattr(args, "file_root", "home") or "home",
        }
    )
    if args.trusted_proxy_note:
        data["session_secret_path"] = str(Path(args.etc_dir) / "secret" / "session.key")
        data["password_hash_path"] = str(Path(args.etc_dir) / "secret" / "admin.hash")
        data["log_dir"] = str(Path(args.log_dir))
        data["state_dir"] = str(Path(args.state_dir))
        data["audit_log_path"] = str(Path(args.log_dir) / "audit.log")
    return data


def _home_of(user: str) -> str:
    """目标用户的 home 目录；非 POSIX 或用户不存在时返回空串。"""

    if os.name == "nt":
        return ""
    try:
        import pwd

        return pwd.getpwnam(user).pw_dir
    except (ImportError, KeyError):
        return ""


def _ensure_upgrade_keys(target: Path, args: argparse.Namespace) -> None:
    """给旧版 config.yaml 补齐新增键（保留原文件与注释）。

    升级时 ``install.sh`` 不会覆盖既有配置，但门户需要 ``ssh_user`` /
    ``ssh_port`` 才能渲染出正确的 SSH 连接命令；v1.1 起还需要
    ``auth_mode`` / ``system_user`` / ``system_home`` / ``file_root_mode``
    / ``file_block_dotfiles`` / ``password_hash_fallback``。缺键时只追加，
    比整份重写安全（不会丢用户手改过的取值）。
    """

    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SystemExit(f"[render_config] 无法读取或解析既有配置：{exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit("[render_config] 既有配置顶层必须是映射")

    additions: list[tuple[str, object]] = []
    if not data.get("ssh_user"):
        additions.append(("ssh_user", args.run_user))
    if not data.get("ssh_port"):
        additions.append(("ssh_port", 22))
    if "auth_mode" not in data:
        additions.append(("auth_mode", "system"))
    if not data.get("system_user"):
        additions.append(("system_user", args.run_user))
    if not data.get("system_home"):
        system_home = getattr(args, "system_home", "") or _home_of(args.run_user)
        if system_home:
            additions.append(("system_home", system_home))
    if "file_root_mode" not in data:
        additions.append(("file_root_mode", getattr(args, "file_root", "home") or "home"))
    if "file_block_dotfiles" not in data:
        additions.append(("file_block_dotfiles", "true"))
    if "password_hash_fallback" not in data:
        additions.append(("password_hash_fallback", "true"))
    if "remote_enabled" not in data:
        additions.append(("remote_enabled", False))
    if "remote_provider" not in data:
        additions.append(("remote_provider", "none"))
    if "remote_zerotier_network_id" not in data:
        additions.append(
            ("remote_zerotier_network_id", str(data.get("zerotier_network_id", "") or ""))
        )
    if "remote_peanut_https_url" not in data:
        additions.append(("remote_peanut_https_url", ""))
    if "remote_config_revision" not in data:
        additions.append(("remote_config_revision", 1))
    if not additions:
        return

    text = target.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    text += "# 以下为实现补充项（由 render_config 自动补齐）\n"
    for key, value in additions:
        text += yaml.safe_dump(
            {key: value}, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
    target.write_text(text, encoding="utf-8")
    os.chmod(target, 0o640)
    print(f"[render_config] 已补齐配置项：{', '.join(k for k, _ in additions)}")


def cmd_config(args: argparse.Namespace) -> int:
    target = Path(args.output)
    if target.exists() and not args.force:
        _ensure_upgrade_keys(target, args)
        print(f"[render_config] 配置已存在，保留：{target}")
        return 0

    data = build_config(args)
    target.parent.mkdir(parents=True, exist_ok=True)

    header = (
        "# 个人云主机配置文件（设计文档第 8 章）\n"
        "# 权限：640；修改后执行 systemctl restart cloudhost-portal 生效。\n"
        "# 备份建议：/etc/cloudhost/ 与 ~/cloud/\n"
    )
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    target.write_text(header + body, encoding="utf-8")
    os.chmod(target, 0o640)
    print(f"[render_config] 已生成配置：{target}")
    return 0


def initialize_remote_admin_users(
    config_path: str | os.PathLike[str],
    *,
    effective_auth_mode: str | None = None,
    system_user: str | None = None,
    admin_user: str | None = None,
) -> bool:
    """Initialize the root-managed allowlist once, based on the effective auth mode."""

    target = Path(config_path)
    try:
        text = target.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SystemExit(f"[render_config] 无法读取或解析配置，未初始化远程管理员：{exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("[render_config] 配置文件顶层必须是映射，未初始化远程管理员")

    sys.path.insert(0, str(PORTAL_DIR))
    try:
        from app.config import ConfigError, _validate_remote_admin_users, load_config

        config = load_config(target)
    except (ConfigError, OSError) as exc:
        raise SystemExit(f"[render_config] 配置校验失败，未初始化远程管理员：{exc}") from exc

    if effective_auth_mode and config.auth_mode != effective_auth_mode:
        raise SystemExit(
            "[render_config] 配置认证模式与安装器最终模式不一致，未初始化远程管理员"
        )

    if "remote_admin_users" in raw:
        print("[render_config] 保留既有 remote_admin_users（含显式空名单）")
        return False

    user = (
        (system_user or config.system_user)
        if config.auth_mode == "system"
        else (admin_user or config.admin_user)
    )
    try:
        _validate_remote_admin_users([user])
    except ConfigError as exc:
        raise SystemExit(f"[render_config] 有效登录账号不能初始化远程管理员：{exc}") from exc

    addition = yaml.safe_dump(
        {"remote_admin_users": [user]}, allow_unicode=True, sort_keys=False
    )
    updated = text + ("" if text.endswith("\n") else "\n") + addition
    try:
        original_stat = target.stat()
    except OSError as exc:
        raise SystemExit(f"[render_config] 无法检查配置权限，未初始化远程管理员：{exc}") from exc
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.remote-admin-",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, stat.S_IMODE(original_stat.st_mode))
        if (original_stat.st_uid, original_stat.st_gid) != (os.getuid(), os.getgid()):
            os.chown(temporary_path, original_stat.st_uid, original_stat.st_gid)
        os.replace(temporary_path, target)
    except OSError as exc:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise SystemExit(f"[render_config] 无法写入远程管理员初始化配置：{exc}") from exc
    print(f"[render_config] 首次初始化 remote_admin_users：{user}")
    return True


def cmd_remote_admins(args: argparse.Namespace) -> int:
    initialize_remote_admin_users(
        args.config,
        effective_auth_mode=args.auth_mode or None,
        system_user=args.system_user,
        admin_user=args.admin_user,
    )
    return 0


# --------------------------------------------------------------------------- #
# 密码哈希 / 会话密钥
# --------------------------------------------------------------------------- #
def cmd_hash(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(PORTAL_DIR))
    from app.security import hash_password  # noqa: PLC0415 - 延迟导入，避免依赖缺失时阻塞其它子命令

    password = args.password
    if password is None:
        password = sys.stdin.readline().rstrip("\n")
    if not password:
        print("[render_config] 密码不能为空", file=sys.stderr)
        return 2
    digest = hash_password(password)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(digest + "\n", encoding="utf-8")
        os.chmod(target, 0o600)
        print(f"[render_config] 已写入密码哈希：{target}")
    else:
        print(digest)
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    target = Path(args.output)
    if target.exists() and not args.force:
        print(f"[render_config] 会话密钥已存在，保留：{target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(48)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    os.chmod(target, 0o600)
    print(f"[render_config] 已生成会话密钥：{target}")
    return 0


# --------------------------------------------------------------------------- #
# 模板渲染
# --------------------------------------------------------------------------- #
def _files_root(config: dict[str, object]) -> str:
    """文件入口根目录：home 模式取 system_home，cloud 模式回退旧行为。"""

    if str(config.get("file_root_mode", "home")) == "home":
        home = str(config.get("system_home") or "")
        if not home:
            raise SystemExit("[render_config] file_root_mode=home 但缺少 system_home")
        return home
    return str(config["cloud_root"])


def _dotfile_handler(config: dict[str, object]) -> str:
    """生成 /files 的点文件 403 块（必须排在 @files 之前）。"""

    if not config.get("file_block_dotfiles", True):
        return "\t\t# 点文件拦截已关闭（file_block_dotfiles=false）"
    # 用单个 path_regexp 表达「/files 前缀 + 任意层级的点文件」，
    # 避免在同一个 matcher set 里混用 path 与 path_regexp。
    # 同时覆盖页面(/files/.ssh)、API(/files/api/resources/.ssh)与 %2e 编码。
    return "\n".join(
        [
            "\t\t# —— 屏蔽点文件：File Browser 无路径 ACL，只能在网关层拦 —— #",
            "\t\t@files_dotfiles path_regexp dotfiles (?i)^/files/(?:.*/)?(?:\\.|%2e)[^/]*",
            "\t\thandle @files_dotfiles {",
            '\t\t\trespond "Forbidden" 403',
            "\t\t}",
        ]
    )


def _placeholders(config: dict[str, object], args: argparse.Namespace) -> dict[str, str]:
    return {
        "SITE_ADDRESS": "http://:" + str(config["listen_port"]),
        "GATEWAY_BIND": "",
        "GATEWAY_CONTEXT": "\t\trequest_header -X-Cloudhost-Transport",
        "GATEWAY_POLICY": "",
        "ACCESS_LOG": """\tlog {
        output file /var/log/cloudhost/caddy-access.log {
            roll_size 20MiB
            roll_keep 5
        }
        format json
    }""",
        "LISTEN_PORT": str(config["listen_port"]),
        "PORTAL_PORT": str(config["portal_port"]),
        "FILEBROWSER_PORT": str(config["filebrowser_port"]),
        "TTYD_PORT": str(config["ttyd_port"]),
        "CLOUD_ROOT": str(config["cloud_root"]),
        "FILES_ROOT": _files_root(config),
        "DOTFILE_HANDLER": _dotfile_handler(config),
        "SITE_DIR": str(config["site_dir"]),
        "RUN_USER": args.run_user,
        "LOGIN_MAX_ATTEMPTS": str(config["login_max_attempts"]),
        "LOGIN_WINDOW_SEC": str(config["login_window_sec"]),
        "FAIL2BAN_BANTIME": str(config["fail2ban_bantime"]),
        "SITE_HANDLER": _site_handler(config),
        "TERM_HANDLER": _term_handler(config),
    }


def _site_handler(config: dict[str, object]) -> str:
    """生成 /site/* 处理块。

    ``handle_path`` 会自动剥掉 ``/site`` 前缀，因此 ``root`` 直接指向站点目录，
    不需要再拼一段路径。``site_public=false`` 时插入 ``forward_auth``，与
    ``/files``、``/term`` 保持一致的门户统一鉴权。
    """

    site_dir = config["site_dir"]
    lines = [
        "\t\t# —— 静态站点（设计文档 5.4）—— #",
        "\t\thandle_path /site/* {",
    ]
    if not config.get("site_public", True):
        lines.extend(
            [
                "\t\t\tforward_auth 127.0.0.1:%s {" % config["portal_port"],
                "\t\t\t\turi /auth/verify",
                "\t\t\t\t@error status 401",
                "\t\t\t\thandle_response @error {",
                "\t\t\t\t\tredir * /login?redirect={http.request.uri} 302",
                "\t\t\t\t}",
                "\t\t\t}",
            ]
        )
    lines.extend(
        [
            "\t\t\troot * %s" % site_dir,
            "\t\t\tfile_server browse",
            "\t\t}",
        ]
    )
    return "\n".join(lines)


def _term_handler(config: dict[str, object]) -> str:
    """生成 /term/* 处理块；ttyd 以 --base-path /term 运行，因此保留前缀。"""

    portal_port = config["portal_port"]
    ttyd_port = config["ttyd_port"]
    if not config.get("ttyd_enabled", True):
        return "\t\t# Web 终端（E1）已按配置禁用：ttyd_enabled=false"
    return "\n".join(
        [
            "\t\t# —— Web 终端（E1）：forward_auth → ttyd —— #",
            "\t\t@term path /term/*",
            "\t\thandle @term {",
            "\t\t\t@terminal_ws path /term/ws",
            "\t\t\treverse_proxy @terminal_ws 127.0.0.1:%s" % portal_port,
            "\t\t\tforward_auth 127.0.0.1:%s {" % portal_port,
            "\t\t\t\turi /auth/verify",
            "\t\t\t\t# 子请求不能带 Upgrade/Connection：否则 uvicorn 会把 /auth/verify\n"
            "\t\t\t\t# 当成 WebSocket 握手直接 403，浏览器的 /term/ws 连不上。",
            "\t\t\t\theader_up -Upgrade",
            "\t\t\t\theader_up -Connection",
            "\t\t\t\t@error status 401",
            "\t\t\t\thandle_response @error {",
            "\t\t\t\t\tredir * /login?redirect={http.request.uri} 302",
            "\t\t\t\t}",
            "\t\t\t}",
            "\t\t\treverse_proxy 127.0.0.1:%s {" % ttyd_port,
            "\t\t\t\theader_up X-Forwarded-Prefix /term",
            "\t\t\t}",
            "\t\t}",
        ]
    )


def render_template(template: Path, target: Path, values: dict[str, str], *, mode: int = 0o644) -> None:
    text = template.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{%s}}" % key, value)
    leftover = [tok for tok in ("{{", "}}") if tok in text]
    if leftover:
        raise SystemExit(f"[render_config] 模板 {template.name} 存在未替换的占位符：{leftover}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    os.chmod(target, mode)


def cmd_render(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    values = _placeholders(config, args)

    plan = {
        "Caddyfile.tmpl": (Path(args.caddyfile), 0o644),
        "cloudhost-portal.service.tmpl": (
            Path(args.systemd_dir) / "cloudhost-portal.service",
            0o644,
        ),
        "cloudhost-files.service.tmpl": (
            Path(args.systemd_dir) / "cloudhost-files.service",
            0o644,
        ),
        "cloudhost-ttyd.service.tmpl": (
            Path(args.systemd_dir) / "cloudhost-ttyd.service",
            0o644,
        ),
        "fail2ban-filter.tmpl": (
            Path(args.fail2ban_dir) / "filter.d" / "cloudhost-caddy.conf",
            0o644,
        ),
        "fail2ban-jail.tmpl": (
            Path(args.fail2ban_dir) / "jail.d" / "cloudhost.conf",
            0o644,
        ),
        "logrotate.tmpl": (Path(args.logrotate_dir) / "cloudhost", 0o644),
    }

    rendered: list[str] = []
    for name, (target, mode) in plan.items():
        template = DEPLOY_DIR / name
        if not template.is_file():
            raise SystemExit(f"[render_config] 缺少模板：{template}")
        if name == "cloudhost-ttyd.service.tmpl" and not config.get("ttyd_enabled", True):
            continue
        render_template(template, target, values, mode=mode)
        rendered.append(str(target))

    setup: list[str] = []
    if args.filebrowser_config:
        setup = write_filebrowser_config(config, Path(args.filebrowser_config), args.run_user)
        rendered.append(args.filebrowser_config)

    print(json.dumps({"rendered": rendered, "filebrowser_setup": setup}, ensure_ascii=False))
    return 0


def write_filebrowser_config(
    config: dict[str, object], target: Path, run_user: str
) -> list[str]:
    """File Browser 2.x 配置：baseURL=/files、仅 127.0.0.1、根目录限定 files_root。

    返回 install.sh 需要执行的 CLI 命令列表（数据库初始化与权限修正）。
    """

    files_root = _files_root(config)
    db_path = "/var/lib/cloudhost/filebrowser/filebrowser.db"
    noauth = bool(config.get("filebrowser_noauth", True))
    payload = {
        "port": int(config["filebrowser_port"]),
        "address": "127.0.0.1",
        "baseURL": "/files",
        "root": files_root,
        "database": db_path,
        "log": "stdout",
        "authMethod": "json",
        "signup": False,
        "hideLoginButton": noauth,
        "noauth": noauth,
        "disablePreviewResize": True,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(target, 0o640)

    setup = [
        "install -d -o {user} -g {user} -m 750 /var/lib/cloudhost/filebrowser".format(user=run_user),
        "chown -R {user}:{user} /var/lib/cloudhost/filebrowser".format(user=run_user),
    ]

    # Directory creation belongs to the installer's returned setup commands.
    # Rendering into an isolated test/output tree must not write /var/lib.
    if os.name != "nt":
        # 配置由 root 生成，但 filebrowser 以 ${run_user} 运行；必须给运行用户
        # 组读权限，否则 filebrowser 读不到 --config，会退回默认 8080 端口并退出。
        try:
            import pwd  # noqa: PLC0415 - 仅 POSIX 存在
            import shutil

            gid = pwd.getpwnam(run_user).pw_gid
            shutil.chown(target, user=0, group=gid)
        except (KeyError, OSError):
            pass
        setup += [
            f"chown root:{run_user} {target}",
            f"chmod 0640 {target}",
        ]
    return setup


def cmd_filebrowser(args: argparse.Namespace) -> int:
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    setup = write_filebrowser_config(config, Path(args.output), args.run_user)
    print(json.dumps({"filebrowser_setup": setup}, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CloudHost 部署辅助工具")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--run-user", default="pi", help="服务运行用户（默认 pi）")

    p_config = sub.add_parser("config", help="生成 config.yaml")
    p_config.add_argument("--output", required=True)
    p_config.add_argument("--home", required=True, help="目标用户 home")
    p_config.add_argument("--cloud-root", default="")
    p_config.add_argument("--admin-user", default="admin")
    p_config.add_argument("--run-user", default="pi", help="服务运行用户（写入 ssh_user）")
    p_config.add_argument(
        "--system-home",
        default="",
        help="允许登录的系统账号 home（默认取 --run-user 的 home）",
    )
    p_config.add_argument(
        "--file-root",
        choices=("home", "cloud"),
        default="home",
        help="文件入口根目录策略：home=用户 home，cloud=旧行为",
    )
    p_config.add_argument("--primary-iface", default="")
    p_config.add_argument("--etc-dir", default="/etc/cloudhost")
    p_config.add_argument("--log-dir", default="/var/log/cloudhost")
    p_config.add_argument("--state-dir", default="/var/lib/cloudhost")
    p_config.add_argument("--trusted-proxy-note", action="store_true", default=True)
    p_config.add_argument("--no-ttyd", action="store_true")
    p_config.add_argument("--force", action="store_true")
    p_config.set_defaults(func=cmd_config)

    p_hash = sub.add_parser("hash", help="生成密码哈希")
    p_hash.add_argument("--password", default=None)
    p_hash.add_argument("--output", default="")
    p_hash.set_defaults(func=cmd_hash)

    p_remote_admins = sub.add_parser(
        "remote-admins", help="首次按有效认证模式初始化远程管理员名单"
    )
    p_remote_admins.add_argument("--config", required=True)
    p_remote_admins.add_argument("--auth-mode", choices=("system", "password_hash"), default="")
    p_remote_admins.add_argument("--system-user", default="")
    p_remote_admins.add_argument("--admin-user", default="")
    p_remote_admins.set_defaults(func=cmd_remote_admins)

    p_session = sub.add_parser("session", help="生成会话密钥")
    p_session.add_argument("--output", required=True)
    p_session.add_argument("--force", action="store_true")
    p_session.set_defaults(func=cmd_session)

    p_render = sub.add_parser("render", help="渲染部署模板")
    p_render.add_argument("--config", required=True)
    p_render.add_argument("--caddyfile", required=True)
    p_render.add_argument("--systemd-dir", required=True)
    p_render.add_argument("--fail2ban-dir", default="/etc/fail2ban")
    p_render.add_argument("--logrotate-dir", default="/etc/logrotate.d")
    p_render.add_argument("--filebrowser-config", default="")
    p_render.add_argument("--home", default="~")
    p_render.add_argument("--run-user", default="pi")
    p_render.set_defaults(func=cmd_render)

    p_fb = sub.add_parser("filebrowser", help="生成 File Browser 配置")
    p_fb.add_argument("--config", required=True)
    p_fb.add_argument("--output", required=True)
    p_fb.add_argument("--run-user", default="pi")
    p_fb.set_defaults(func=cmd_filebrowser)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
