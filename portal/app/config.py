"""配置加载与校验。

对应设计文档第 8 章「配置项清单」。配置文件默认位于
``/etc/cloudhost/config.yaml``，可用环境变量 ``CLOUDHOST_CONFIG`` 覆盖。

所有取值范围都按文档第 8 章校验；文档未列出、但实现需要的少量配置项
（如 ``admin_user``、``password_hash_path``）以「实现补充项」形式追加，
不影响文档既有配置项的语义。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/etc/cloudhost/config.yaml"
CONFIG_ENV = "CLOUDHOST_CONFIG"

_ALLOWED_ACCESS_MODES = ("lan", "zerotier")
_ALLOWED_TLS_MODES = ("off", "internal", "acme")
_ALLOWED_AUTH_MODES = ("system", "password_hash")
_ALLOWED_FILE_ROOT_MODES = ("home", "cloud")
_ALLOWED_REMOTE_PROVIDERS = ("none", "tailscale", "zerotier", "peanut")
_REMOTE_KEYS = frozenset(
    {
        "remote_enabled",
        "remote_provider",
        "remote_zerotier_network_id",
        "remote_peanut_https_url",
        "remote_ssh_host",
        "remote_ssh_port",
        "remote_config_revision",
        "remote_admin_users",
    }
)
_REMOTE_USERNAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,30}\$?$")
_RESERVED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".test",
    ".example",
    ".invalid",
    ".onion",
)

# 设计文档第 8 章列出的配置项
_DOC_KEYS = frozenset(
    {
        "access_mode",
        "listen_port",
        "tls_mode",
        "portal_port",
        "filebrowser_port",
        "ttyd_port",
        "cloud_root",
        "site_dir",
        "site_public",
        "session_ttl",
        "session_secret_path",
        "login_max_attempts",
        "login_window_sec",
        "fail2ban_bantime",
        "status_interval_ms",
        "ssh_key_only",
        "zerotier_network_id",
    }
)

# 实现补充项（文档未列出，随程序维护）
_EXTRA_KEYS = frozenset(
    {
        "admin_user",
        "password_hash_path",
        "log_dir",
        "state_dir",
        "audit_log_path",
        "max_upload_mb",
        "primary_iface",
        "ttyd_enabled",
        "ssh_user",
        "ssh_port",
        "auth_mode",
        "system_user",
        "system_home",
        "file_root_mode",
        "file_block_dotfiles",
        "password_hash_fallback",
    }
)


class ConfigError(ValueError):
    """配置文件缺失或取值非法。"""


@dataclass(frozen=True)
class Config:
    """已解析并校验过的运行配置。"""

    config_path: Path

    # —— 文档第 8 章 ——
    access_mode: str
    listen_port: int
    tls_mode: str
    portal_port: int
    filebrowser_port: int
    ttyd_port: int
    cloud_root: Path
    site_dir: Path
    site_public: bool
    session_ttl: int
    session_secret_path: Path
    login_max_attempts: int
    login_window_sec: int
    fail2ban_bantime: int
    status_interval_ms: int
    ssh_key_only: bool
    zerotier_network_id: str

    # —— 实现补充项 ——
    admin_user: str
    password_hash_path: Path
    log_dir: Path
    state_dir: Path
    audit_log_path: Path
    max_upload_mb: int
    primary_iface: str | None
    ttyd_enabled: bool
    ssh_user: str
    ssh_port: int

    # —— 系统账号登录 / 文件根目录（本方案新增）——
    auth_mode: str
    system_user: str
    system_home: Path
    file_root_mode: str
    file_block_dotfiles: bool
    password_hash_fallback: bool
    remote_enabled: bool = False
    remote_provider: Literal["none", "tailscale", "zerotier", "peanut"] = "none"
    remote_zerotier_network_id: str = ""
    remote_peanut_https_url: str = ""
    remote_ssh_host: str = ""
    remote_ssh_port: int = 22
    remote_config_revision: int = 1
    remote_admin_users: tuple[str, ...] = ()

    @property
    def files_root(self) -> Path:
        """文件入口的实际根目录。"""

        return self.system_home if self.file_root_mode == "home" else self.cloud_root

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Path):
                data[key] = str(value)
        return data


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """读取并校验配置；失败抛 :class:`ConfigError`。"""

    raw_path = Path(path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH).expanduser()
    if not raw_path.is_file():
        raise ConfigError(
            f"配置文件不存在：{raw_path}。请先执行 install.sh，或用 {CONFIG_ENV} 指定配置文件。"
        )

    try:
        raw = yaml.safe_load(raw_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - YAML 语法错误分支
        raise ConfigError(f"配置文件 YAML 解析失败：{exc}") from exc
    except UnicodeError as exc:
        raise ConfigError(f"配置文件编码无效：{exc}") from exc
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败：{exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是映射（key: value）")

    data: dict[str, Any] = {str(k): v for k, v in raw.items()}
    unknown = set(data) - _DOC_KEYS - _EXTRA_KEYS - _REMOTE_KEYS
    unknown_remote = sorted(key for key in unknown if key.startswith("remote_"))
    if unknown_remote:
        raise ConfigError(f"未知远程配置项：{', '.join(unknown_remote)}")
    unknown -= set(unknown_remote)
    if unknown:
        logger.warning("配置文件含未知配置项，已忽略：%s", ", ".join(sorted(unknown)))

    return Config(config_path=raw_path.resolve(), **_parse(data))


def _parse(data: dict[str, Any]) -> dict[str, Any]:
    access_mode = _str(data, "access_mode", "lan")
    if access_mode not in _ALLOWED_ACCESS_MODES:
        raise ConfigError(f"access_mode 取值必须是 {_ALLOWED_ACCESS_MODES}，当前为 {access_mode!r}")

    tls_mode = _str(data, "tls_mode", "off")
    if tls_mode not in _ALLOWED_TLS_MODES:
        raise ConfigError(f"tls_mode 取值必须是 {_ALLOWED_TLS_MODES}，当前为 {tls_mode!r}")

    cloud_root = _path(data, "cloud_root", "~/cloud")
    cloud_root_str = _posix_abspath(cloud_root)
    if cloud_root_str in ("/", "//") or Path(cloud_root_str).anchor == cloud_root_str:
        raise ConfigError("cloud_root 不允许指向文件系统根目录")
    if cloud_root_str == _posix_abspath(Path(os.path.expanduser("~"))):
        logger.warning("cloud_root 指向用户主目录，文件服务将暴露整个 home 目录，建议使用 ~/cloud")

    site_dir = _path(data, "site_dir", _posix_join(cloud_root_str, "www"))
    site_str = _posix_abspath(site_dir)
    if not _is_within(cloud_root_str, site_str):
        raise ConfigError("site_dir 必须是 cloud_root 的子目录")

    log_dir = _path(data, "log_dir", "/var/log/cloudhost")
    state_dir = _path(data, "state_dir", "/var/lib/cloudhost")

    ssh_user_value = _str(data, "ssh_user", "pi") or "pi"
    if not _REMOTE_USERNAME.fullmatch(ssh_user_value):
        raise ConfigError("ssh_user 必须是有效的系统用户名")

    auth_mode = _str(data, "auth_mode", "system")
    if auth_mode not in _ALLOWED_AUTH_MODES:
        raise ConfigError(
            f"auth_mode 取值必须是 system/password_hash，当前为 {auth_mode!r}"
        )

    file_root_mode = _str(data, "file_root_mode", "home")
    if file_root_mode not in _ALLOWED_FILE_ROOT_MODES:
        raise ConfigError(
            f"file_root_mode 取值必须是 home/cloud，当前为 {file_root_mode!r}"
        )

    system_home_raw = _str(data, "system_home", "")
    if file_root_mode == "home":
        if not system_home_raw:
            raise ConfigError("file_root_mode=home 时必须配置 system_home")
        if not system_home_raw.startswith("/") or system_home_raw.rstrip("/") == "":
            raise ConfigError("system_home 必须是绝对路径，且不能是 /")
        system_home = Path(system_home_raw)
    else:
        system_home = Path(system_home_raw) if system_home_raw else Path("")

    system_user = _str(data, "system_user", "") or ssh_user_value
    if auth_mode == "system" and system_user != ssh_user_value:
        logger.warning(
            "system_user=%s 与服务运行用户 ssh_user=%s 不一致："
            "PAM 只能校验运行用户本人，系统账号登录大概率失败",
            system_user,
            ssh_user_value,
        )
    if file_root_mode == "home" and not _is_within(
        _posix_abspath(system_home), _posix_abspath(cloud_root)
    ):
        logger.warning(
            "cloud_root=%s 不在 system_home=%s 下：站点目录会落在文件入口根目录之外",
            cloud_root_str,
            system_home,
        )

    zerotier_network_id = _str(data, "zerotier_network_id", "")
    if zerotier_network_id and not _is_zerotier_id(zerotier_network_id):
        raise ConfigError("zerotier_network_id 必须是 16 位十六进制字符串或留空")

    remote_enabled = _remote_bool(data, "remote_enabled", False)
    remote_provider = _remote_string(data, "remote_provider", "none")
    if remote_provider not in _ALLOWED_REMOTE_PROVIDERS:
        raise ConfigError(
            f"remote_provider 取值必须是 {_ALLOWED_REMOTE_PROVIDERS}，当前为 {remote_provider!r}"
        )

    # Old deployments may only have the legacy key. Treat it as saved
    # configuration, never as an instruction to join a network.
    remote_zerotier_network_id = _remote_string(
        data, "remote_zerotier_network_id", zerotier_network_id
    )
    if remote_zerotier_network_id and not _is_zerotier_id(remote_zerotier_network_id):
        raise ConfigError(
            "remote_zerotier_network_id 必须是 16 位十六进制字符串或留空"
        )

    remote_peanut_https_url = _remote_string(data, "remote_peanut_https_url", "")
    if remote_peanut_https_url:
        _validate_remote_https_url(remote_peanut_https_url)

    remote_ssh_host = _remote_string(data, "remote_ssh_host", "")
    remote_ssh_port = _int(data, "remote_ssh_port", 22, 1, 65535)
    if remote_ssh_host:
        _validate_remote_ssh_host(remote_ssh_host)
    elif remote_ssh_port != 22:
        raise ConfigError("非默认 remote_ssh_port 只能在配置 remote_ssh_host 时设置")

    revision = data.get("remote_config_revision", 1)
    if type(revision) is not int or revision < 1:
        raise ConfigError("remote_config_revision 必须是大于等于 1 的整数")

    users = data.get("remote_admin_users", [])
    _validate_remote_admin_users(users)

    return {
        "access_mode": access_mode,
        "listen_port": _int(data, "listen_port", 80, 1, 65535),
        "tls_mode": tls_mode,
        "portal_port": _int(data, "portal_port", 8080, 1, 65535),
        "filebrowser_port": _int(data, "filebrowser_port", 8081, 1, 65535),
        "ttyd_port": _int(data, "ttyd_port", 7681, 1, 65535),
        "cloud_root": cloud_root,
        "site_dir": site_dir,
        "site_public": _bool(data, "site_public", True),
        "session_ttl": _int(data, "session_ttl", 3600, 300, 86400),
        "session_secret_path": _path(
            data, "session_secret_path", "/etc/cloudhost/secret/session.key"
        ),
        "login_max_attempts": _int(data, "login_max_attempts", 5, 3, 20),
        "login_window_sec": _int(data, "login_window_sec", 300, 60, 3600),
        "fail2ban_bantime": _int(data, "fail2ban_bantime", 600, 60, 86400),
        "status_interval_ms": _int(data, "status_interval_ms", 1000, 500, 5000),
        "ssh_key_only": _bool(data, "ssh_key_only", True),
        "zerotier_network_id": zerotier_network_id,
        "remote_enabled": remote_enabled,
        "remote_provider": remote_provider,
        "remote_zerotier_network_id": remote_zerotier_network_id,
        "remote_peanut_https_url": remote_peanut_https_url,
        "remote_ssh_host": remote_ssh_host,
        "remote_ssh_port": remote_ssh_port,
        "remote_config_revision": revision,
        "remote_admin_users": tuple(users),
        "admin_user": _str(data, "admin_user", "admin") or "admin",
        "password_hash_path": _path(
            data, "password_hash_path", "/etc/cloudhost/secret/admin.hash"
        ),
        "log_dir": log_dir,
        "state_dir": state_dir,
        "audit_log_path": _path(data, "audit_log_path", _posix_join(log_dir, "audit.log")),
        "max_upload_mb": _int(data, "max_upload_mb", 4096, 1, 1024 * 1024),
        "primary_iface": _str(data, "primary_iface", "") or None,
        "ttyd_enabled": _bool(data, "ttyd_enabled", True),
        # 门户「SSH」入口展示：运行用户与端口（默认 22，非 22 时命令带 -p）
        "ssh_user": ssh_user_value,
        "ssh_port": _int(data, "ssh_port", 22, 1, 65535),
        "auth_mode": auth_mode,
        "system_user": system_user,
        "system_home": system_home,
        "file_root_mode": file_root_mode,
        "file_block_dotfiles": _bool(data, "file_block_dotfiles", True),
        "password_hash_fallback": _bool(data, "password_hash_fallback", True),
    }


def _validate_remote_admin_users(users: Any) -> None:
    if not isinstance(users, list):
        raise ConfigError("remote_admin_users 必须是用户名列表")
    if any(not isinstance(user, str) or not _REMOTE_USERNAME.fullmatch(user) for user in users):
        raise ConfigError("remote_admin_users 含有非法用户名")
    if len(set(users)) != len(users):
        raise ConfigError("remote_admin_users 不允许重复用户名")


def _remote_string(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or value != value.strip():
        raise ConfigError(f"{key} 必须是无首尾空格的字符串")
    return value


def _remote_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if type(value) is not bool:
        raise ConfigError(f"{key} 必须是布尔值")
    return value


def _validate_remote_https_url(value: str) -> None:
    if len(value) > 2048 or any(unicodedata.category(char) == "Cc" for char in value):
        raise ConfigError("remote_peanut_https_url 长度或控制字符非法")
    if "\\" in value or "?" in value or "#" in value:
        raise ConfigError("remote_peanut_https_url 不允许反斜线、query 或 fragment")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("remote_peanut_https_url 格式非法") from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ConfigError("remote_peanut_https_url 必须是 HTTPS URL")
    if parsed.netloc.endswith(":"):
        raise ConfigError("remote_peanut_https_url 端口非法")
    if "@" in parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ConfigError("remote_peanut_https_url 不允许 userinfo")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ConfigError("remote_peanut_https_url 仅允许根路径且不允许 query/fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("remote_peanut_https_url 端口非法")

    hostname = parsed.hostname
    if not hostname or "%" in hostname or any(char.isspace() for char in hostname):
        raise ConfigError("remote_peanut_https_url 主机名非法")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if all(char in "0123456789." for char in hostname):
            raise ConfigError("remote_peanut_https_url 不允许非法 IP 地址")
        try:
            ascii_host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ConfigError("remote_peanut_https_url 主机名非法") from exc
        labels = ascii_host.rstrip(".").split(".")
        numeric_ipv4_labels = all(
            re.fullmatch(r"(?:0x[0-9a-f]+|0[0-7]*|[0-9]+)", label)
            for label in labels
        )
        if (
            ascii_host.endswith(".")
            or len(ascii_host) > 253
            or len(labels) < 2
            or numeric_ipv4_labels
            or any(
                not label
                or len(label) > 63
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in labels
            )
            or ascii_host == "localhost"
            or any(ascii_host.endswith(suffix) for suffix in _RESERVED_HOST_SUFFIXES)
        ):
            raise ConfigError("remote_peanut_https_url 主机名非法或为本地/保留名称")
    else:
        if not address.is_global:
            raise ConfigError("remote_peanut_https_url 不允许本地或保留 IP 地址")


def _validate_remote_ssh_host(value: str) -> None:
    """Require an assigned public TCP hostname/IP, never a URL or host:port."""

    if (
        len(value) > 253
        or any(unicodedata.category(char) == "Cc" or char.isspace() for char in value)
        or any(char in value for char in "/\\?#@:")
        or "://" in value
    ):
        raise ConfigError("remote_ssh_host 必须是公网主机名，不含协议、端口或路径")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if all(char in "0123456789." for char in value):
            raise ConfigError("remote_ssh_host 不是有效的公网 IP 地址")
        try:
            ascii_host = value.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ConfigError("remote_ssh_host 主机名非法") from exc
        labels = ascii_host.split(".")
        numeric_ipv4_labels = all(
            re.fullmatch(r"(?:0x[0-9a-f]+|0[0-7]*|[0-9]+)", label)
            for label in labels
        )
        if (
            ascii_host.endswith(".")
            or len(labels) < 2
            or numeric_ipv4_labels
            or any(
                not label
                or len(label) > 63
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in labels
            )
            or ascii_host == "localhost"
            or any(ascii_host.endswith(suffix) for suffix in _RESERVED_HOST_SUFFIXES)
        ):
            raise ConfigError("remote_ssh_host 主机名非法或为本地/保留名称")
    else:
        if not address.is_global:
            raise ConfigError("remote_ssh_host 不允许本地或保留 IP 地址")


def _str(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, (str, int, float)):
        raise ConfigError(f"{key} 必须是字符串")
    return str(value).strip()


def _int(data: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool):
        raise ConfigError(f"{key} 必须是整数")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} 必须是整数，当前为 {value!r}") from exc
    if not lo <= parsed <= hi:
        raise ConfigError(f"{key} 必须在 {lo}–{hi} 之间，当前为 {parsed}")
    return parsed


def _bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "yes", "on", "1"):
            return True
        if normalized in ("false", "no", "off", "0"):
            return False
    raise ConfigError(f"{key} 必须是布尔值（true/false）")


def _path(data: dict[str, Any], key: str, default: str) -> Path:
    value = data.get(key, default)
    if value is None or str(value).strip() == "":
        raise ConfigError(f"{key} 不能为空")
    expanded = os.path.expanduser(str(value).strip())
    path = Path(expanded)
    # 目标平台是 Linux，配置里出现的 "/xxx" 一律视为绝对路径；
    # 在 Windows 上做单元测试时 Path("/x").is_absolute() 为 False，因此额外放行前导 "/"。
    posix_absolute = expanded.startswith("/")
    if not path.is_absolute() and not posix_absolute:
        raise ConfigError(f"{key} 必须是绝对路径，当前为 {value!r}")
    return path


def _posix_abspath(path: Path | str) -> str:
    """返回 POSIX 风格的绝对路径字符串（Windows 上仅用于本模块的包含关系判断）。"""

    text = str(path)
    if text.startswith("/"):
        return text
    return os.path.abspath(text)


def _posix_join(base: Path | str, *parts: str) -> str:
    """以 POSIX 分隔符拼接路径，避免在 Windows 开发机上写出反斜杠。"""

    text = str(base).replace("\\", "/").rstrip("/")
    for part in parts:
        text = f"{text}/{part.strip('/')}"
    return text


def _is_zerotier_id(value: str) -> bool:
    return len(value) == 16 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def _is_within(root: str, candidate: str) -> bool:
    """判断 ``candidate`` 是否位于 ``root`` 内（不依赖平台分隔符差异）。"""

    root_norm = root.rstrip("/\\") or "/"
    candidate_norm = candidate.rstrip("/\\")
    if candidate_norm == root_norm:
        return True
    for sep in ("/", "\\"):
        if candidate_norm.startswith(root_norm + sep):
            return True
    return False
