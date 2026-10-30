#!/usr/bin/env python3
"""Explicit root installer, never executes SDK or starts services."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import io
import json
import os
import pwd
import re
import secrets
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import yaml

from render_config import DEPLOY_DIR, _placeholders, render_template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "portal"))
from app.peanut_credentials import SDK_SHA256, inspect_sdk

ARCHIVE_SHA256 = "d821bcccb2d492c8e375984ffaf9d618f27a41f6f05b98a0f6b557a95a23b524"
ARCHIVE_URL = "https://dl.oray.com/hsk/sdk/phtunnel-all.zip"


def sdk_payload(raw: bytes) -> bytes:
    if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("official archive hash mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        binary = archive.read("linux/aarch64-unknown-linux-gnu/phtunnel")
    if hashlib.sha256(binary).hexdigest() != SDK_SHA256:
        raise ValueError("SDK hash mismatch")
    return binary


def atomic_write(
    target: Path, raw: bytes, mode: int, uid: int = 0, gid: int = 0, *,
    replace: bool = True,
) -> None:
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=".peanut-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.chown(name, uid, gid)
        if replace:
            os.replace(name, target)
        else:
            os.link(name, target, follow_symlinks=False)
    finally:
        Path(name).unlink(missing_ok=True)


def install_adapter_evidence_permissions(
    path: Path, *, sdk_gid: int, owner_uid: int = 0
) -> None:
    """Keep evidence root-owned while allowing the SDK group to read it."""

    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != owner_uid
        ):
            raise ValueError("unsafe adapter evidence file")
        os.fchown(fd, owner_uid, sdk_gid)
        os.fchmod(fd, 0o640)
    except OSError as exc:
        raise ValueError("unable to secure adapter evidence permissions") from exc
    finally:
        if fd is not None:
            os.close(fd)


def install_app_credentials(
    path: Path, app_id: str, app_key: str, *, uid: int = 0, gid: int = 0
) -> bool:
    """Create the credential file once; never replace an existing identity config."""

    info = path.parent.stat(follow_symlinks=False)
    if (
        path.parent.is_symlink()
        or not path.parent.is_dir()
        or info.st_uid != uid
        or info.st_gid != gid
        or info.st_mode & 0o777 != 0o750
    ):
        raise ValueError("unsafe application-credential directory")
    if path.exists() or path.is_symlink():
        from app.peanut_credentials import PeanutError, read_credentials
        try:
            read_credentials(path, uid=uid, gid=gid)
        except PeanutError as exc:
            raise ValueError(
                "existing application credentials are invalid; refusing to replace them"
            ) from exc
        return False
    if not re.fullmatch(r"[0-9]+", app_id, re.ASCII):
        raise ValueError("invalid application credentials")
    if (
        not app_key.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in app_key)
        or "<" in app_key
        or "YOUR_" in app_key.upper()
    ):
        raise ValueError("invalid application credentials")
    raw = (json.dumps(
        {"app_id": app_id, "app_key": app_key}, ensure_ascii=False, separators=(",", ":")
    ) + "\n").encode()
    atomic_write(path, raw, 0o640, uid, gid, replace=False)
    from app.peanut_credentials import read_credentials
    read_credentials(path, uid=uid, gid=gid)
    return True


def read_deployment_credentials(path: Path, *, uid: int) -> tuple[str, str]:
    """Read a one-time mode-0600 sidecar without following links."""

    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("unsafe deployment credential file")
        raw = os.read(fd, 8193)
        if not raw or len(raw) > 8192:
            raise ValueError("invalid deployment credential file")
        from app.peanut_credentials import strict_object
        value = json.loads(raw, object_pairs_hook=strict_object)
        if not isinstance(value, dict) or set(value) != {"app_id", "app_key"}:
            raise ValueError("invalid deployment credential file")
        app_id, app_key = value["app_id"], value["app_key"]
        if (
            not isinstance(app_id, str) or not re.fullmatch(r"[0-9]+", app_id, re.ASCII)
            or not isinstance(app_key, str) or not app_key.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in app_key)
            or "<" in app_key or "YOUR_" in app_key.upper()
        ):
            raise ValueError("invalid deployment credentials")
        return app_id, app_key
    except OSError as exc:
        raise ValueError("unable to read deployment credential file safely") from exc
    finally:
        if fd is not None:
            os.close(fd)


def import_deployment_credentials(
    source: Path, target: Path, *, source_uid: int, target_uid: int, target_gid: int,
) -> bool:
    app_id, app_key = read_deployment_credentials(source, uid=source_uid)
    installed = install_app_credentials(target, app_id, app_key, uid=target_uid, gid=target_gid)
    if installed:
        source.unlink()
    return installed


def gateway_values(config: dict, user: str, token: str) -> dict[str, str]:
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid gateway key")
    values = _placeholders(config, SimpleNamespace(run_user=user))
    values.update({
        "CADDY_BIN": "/usr/bin/caddy",
        "SITE_ADDRESS": "http://:18080",
        "GATEWAY_BIND": "\tbind 127.0.0.1",
        "GATEWAY_CONTEXT": "\t\trequest_header X-Cloudhost-Transport " + token,
        "GATEWAY_POLICY": """
        request_header -Forwarded
        request_header -X-Forwarded-Proto
        @challenge path /.well-known/cloudhost-peanut-check
        reverse_proxy @challenge 127.0.0.1:%s
        forward_auth 127.0.0.1:%s {
            uri /auth/ingress
            header_up -Upgrade
            header_up -Connection
        }
        header Strict-Transport-Security "max-age=31536000"
""" % (config["portal_port"], config["portal_port"]),
        "ACCESS_LOG": "",
    })
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--caddy-bin", required=True, type=Path)
    parser.add_argument(
        "--configure-app", action="store_true",
        help="prompt securely for application credentials only when no file exists",
    )
    parser.add_argument("--credentials-file", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("requires root")
    if os.uname().machine not in {"aarch64", "arm64"}:
        parser.error("only GNU ARM64 is supported")
    portal = pwd.getpwnam(args.user)
    sdk_user = pwd.getpwnam("cloudhost-peanut")
    os.umask(0o077)
    if args.archive:
        raw = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(ARCHIVE_URL, timeout=30) as response:
            if response.url != ARCHIVE_URL:
                raise ValueError("unexpected SDK redirect")
            raw = response.read(16 * 1024 * 1024 + 1)
    binary = sdk_payload(raw)
    target = Path("/opt/cloudhost/bin/phtunnel")
    if target.exists() and inspect_sdk(target) != "installed":
        raise ValueError("existing SDK differs: back it up and explicitly remove it before replacing")
    atomic_write(target, binary, 0o755)
    for path, uid, gid, mode in (
        (Path("/etc/cloudhost-peanut"), 0, sdk_user.pw_gid, 0o750),
        (Path("/var/lib/cloudhost-peanut"), sdk_user.pw_uid, sdk_user.pw_gid, 0o700),
        (Path("/var/lib/cloudhost-peanut/sdk"), sdk_user.pw_uid, sdk_user.pw_gid, 0o700),
        (Path("/var/lib/cloudhost-peanut-control"), 0, 0, 0o700),
        (Path("/var/log/cloudhost-peanut"), sdk_user.pw_uid, sdk_user.pw_gid, 0o700),
        (Path("/run/cloudhost-peanut"), 0, portal.pw_gid, 0o750),
    ):
        if path.is_symlink():
            raise ValueError("refusing symlink: " + str(path))
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    adapter_path = Path("/etc/cloudhost-peanut/adapter.json")
    if adapter_path.exists() or adapter_path.is_symlink():
        install_adapter_evidence_permissions(adapter_path, sdk_gid=sdk_user.pw_gid)
    for name in ("stdout.log", "stderr.log", "phtunnel.log"):
        log = Path("/var/log/cloudhost-peanut") / name
        if log.is_symlink():
            raise ValueError("refusing log symlink")
        log.touch(mode=0o600, exist_ok=True)
        os.chown(log, sdk_user.pw_uid, sdk_user.pw_gid)
        os.chmod(log, 0o600)
    app_path = Path("/etc/cloudhost-peanut/app.json")
    if app_path.exists() or app_path.is_symlink():
        from app.peanut_credentials import read_credentials
        read_credentials(app_path, uid=0, gid=sdk_user.pw_gid)
        print("Existing application credentials preserved.")
    elif args.configure_app:
        credentials_file = args.credentials_file
        if credentials_file and (credentials_file.exists() or credentials_file.is_symlink()):
            installed = import_deployment_credentials(
                credentials_file, app_path, source_uid=portal.pw_uid,
                target_uid=0, target_gid=sdk_user.pw_gid,
            )
            if installed:
                print("Application credentials installed from the private deployment sidecar.")
            else:
                print("Existing application credentials preserved.")
        elif not sys.stdin.isatty():
            raise ValueError(
                "PHTunnel credentials are missing; run the explicit installer from a terminal "
                "to enter them securely"
            )
        else:
            app_id = getpass.getpass("PHTunnel application ID (input hidden): ")
            app_key = getpass.getpass("PHTunnel application key (input hidden): ")
            install_app_credentials(
                app_path, app_id, app_key, uid=0, gid=sdk_user.pw_gid
            )
            print("Application credentials installed with restricted permissions.")
    else:
        print("Application credentials are not installed; remote startup requires deployment setup.")
    key_file = Path("/etc/cloudhost/secret/peanut-gateway.key")
    if not key_file.exists():
        atomic_write(key_file, secrets.token_hex(32).encode(), 0o640, gid=portal.pw_gid)
    token = key_file.read_text().strip()
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid gateway key")
    config = yaml.safe_load(Path("/etc/cloudhost/config.yaml").read_text())
    if config.get("remote_provider", "none") == "none":
        text = Path("/etc/cloudhost/config.yaml").read_text()
        if re.search(r"(?m)^remote_provider:", text):
            text = re.sub(r"(?m)^remote_provider:.*$", "remote_provider: peanut", text)
        else:
            text += "\nremote_provider: peanut\n"
        atomic_write(Path("/etc/cloudhost/config.yaml"), text.encode(), 0o640, gid=portal.pw_gid)
    values = gateway_values(config, args.user, token)
    caddy = args.caddy_bin.resolve(strict=True)
    info = caddy.stat()
    if info.st_uid != 0 or info.st_mode & 0o022 or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(caddy)):
        raise ValueError("unsafe Caddy executable")
    values["CADDY_BIN"] = str(caddy)
    render_template(DEPLOY_DIR / "Caddyfile.tmpl", Path("/etc/cloudhost-peanut/Caddyfile"), values, mode=0o600)
    for name in ("cloudhost-peanut", "cloudhost-peanut-control", "cloudhost-remote-gateway"):
        render_template(DEPLOY_DIR / (name + ".service.tmpl"),
                        Path("/etc/systemd/system") / (name + ".service"), values)
    render_template(DEPLOY_DIR / "peanut-logrotate.tmpl",
                    Path("/etc/logrotate.d/cloudhost-peanut"), values)
    print("Peanut installed, not started. Device identity and existing credentials were preserved.")


if __name__ == "__main__":
    main()
