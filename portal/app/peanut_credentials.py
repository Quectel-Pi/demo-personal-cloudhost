"""Fixed-path PHTunnel application credentials; never serialize Credentials."""

from __future__ import annotations

import hashlib
import grp
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

APP_FILE = Path("/etc/cloudhost-peanut/app.json")
SDK_FILE = Path("/opt/cloudhost/bin/phtunnel")
SDK_SHA256 = "562202abbda41ff5400e6b07b240ff9df1fb56eb3ff4c57d938bee0e914026f0"
SDK_VERSION = "1.1.5.1"


class PeanutError(Exception):
    def __init__(self, reason: str, *, code: str = "E5002") -> None:
        self.reason = reason
        self.code = code
        super().__init__(reason)


@dataclass(frozen=True)
class Credentials:
    app_id: str = field(repr=False)
    app_key: str = field(repr=False)
    fingerprint: str = field(repr=False)


def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def read_credentials(
    path: Path = APP_FILE, *, uid: int = 0, gid: int | None = None
) -> Credentials:
    """Open the directory and file without following links, then validate fstat."""
    directory_fd = file_fd = None
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent = os.fstat(directory_fd)
        if gid is None:
            try:
                gid = grp.getgrnam("cloudhost-peanut").gr_gid
            except KeyError as exc:
                raise PeanutError("unsafe_permissions", code="E5001") from exc
        if (
            parent.st_uid != uid
            or stat.S_IMODE(parent.st_mode) != 0o750
            or (gid is not None and parent.st_gid != gid)
        ):
            raise PeanutError("unsafe_permissions", code="E5001")
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        info = os.fstat(file_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != uid
            or info.st_gid != parent.st_gid
            or stat.S_IMODE(info.st_mode) != 0o640
        ):
            raise PeanutError("unsafe_permissions", code="E5001")
        raw = os.read(file_fd, 8193)
        if not raw or len(raw) > 8192:
            raise PeanutError("invalid", code="E5001")
        value = json.loads(raw, object_pairs_hook=strict_object)
        if not isinstance(value, dict) or set(value) != {"app_id", "app_key"}:
            raise ValueError("schema")
        app_id, app_key = value["app_id"], value["app_key"]
        if not isinstance(app_id, str) or not re.fullmatch(r"[0-9]+", app_id, re.ASCII):
            raise ValueError("app_id")
        if (
            not isinstance(app_key, str)
            or not app_key.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in app_key)
            or "<" in app_key or "YOUR_" in app_key.upper()
        ):
            raise ValueError("app_key")
        return Credentials(app_id, app_key, hashlib.sha256(raw).hexdigest())
    except FileNotFoundError as exc:
        raise PeanutError("missing", code="E5001") from exc
    except PermissionError as exc:
        raise PeanutError("unreadable", code="E5001") from exc
    except (ValueError, UnicodeError) as exc:
        raise PeanutError("invalid", code="E5001") from exc
    except OSError as exc:
        raise PeanutError("unsafe_permissions", code="E5001") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def inspect_sdk(path: Path = SDK_FILE, *, uid: int = 0) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != uid
                or stat.S_IMODE(info.st_mode) != 0o755
            ):
                return "incompatible"
            raw = stream.read(2 * 1024 * 1024)
        if (
            raw[:5] != b"\x7fELF\x02"
            or raw[5:6] != b"\x01"
            or raw[18:20] != b"\xb7\x00"
            or hashlib.sha256(raw).hexdigest() != SDK_SHA256
        ):
            return "incompatible"
        return "installed"
    except FileNotFoundError:
        return "not_installed"
    except OSError:
        return "unknown"
