"""文件服务根目录的路径解析与安全校验（设计文档 F3 / A2 / 18 章降级方案）。

两种使用方式：

1. File Browser 作为主文件服务，本模块提供 ``resolve_cloud_path`` 给降级用；
2. 若 File Browser 与 ``forward_auth`` 不兼容（文档第 18 章风险项），
   直接启用本模块内置的文件接口，行为与验收项 A2 一致。

安全规则：

- 所有路径都相对 ``cloud_root`` 解析，解析后必须仍在根目录内；
- 拒绝 ``..`` 片段、绝对路径逃逸；
- 拒绝通过符号链接指向根目录之外的目标（``realpath`` 二次校验）。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

from .errors import ApiError

MAX_FILENAME_LEN = 200
_FORBIDDEN_CHARS = {"\x00", "/", "\\"}


def resolve_cloud_path(cloud_root: Path, relative: str | None) -> Path:
    """把用户输入的相对路径解析为 ``cloud_root`` 内的绝对路径。

    :raises ApiError: E1003 路径越界，E1001 参数非法
    """

    root = Path(os.path.realpath(cloud_root))
    if not root.is_dir():
        raise ApiError("E1003", detail=f"cloud_root not available: {cloud_root}")

    raw = (relative or "").strip()
    if "\x00" in raw:
        raise ApiError("E1001", detail="path contains NUL byte")

    # 允许前端使用 /a/b 形式，统一去掉前导分隔符后按相对路径解析
    candidate = Path(raw.lstrip("/\\")) if raw else Path()

    if any(part == ".." for part in candidate.parts):
        raise ApiError("E1003", detail="path traversal is not allowed")

    target = (root / candidate)
    # 不用 resolve(strict=False) 之前先做 realpath，避免符号链接逃逸
    resolved = Path(os.path.realpath(target))
    if resolved != root and root not in resolved.parents:
        raise ApiError("E1003", detail="path escapes cloud_root")
    return resolved


def relative_to_root(cloud_root: Path, path: Path) -> str:
    root = Path(os.path.realpath(cloud_root))
    if path == root:
        return "/"
    return "/" + path.relative_to(root).as_posix()


def validate_filename(name: str | None) -> str:
    """校验用户新建/重命名的文件名。"""

    if name is None:
        raise ApiError("E1001", detail="name is required")
    cleaned = name.strip()
    if not cleaned:
        raise ApiError("E1001", detail="name is empty")
    if len(cleaned) > MAX_FILENAME_LEN:
        raise ApiError("E1001", detail="name is too long")
    if cleaned in (".", ".."):
        raise ApiError("E1003", detail="invalid name")
    if any(ch in _FORBIDDEN_CHARS for ch in cleaned):
        raise ApiError("E1001", detail="name contains forbidden characters")
    if cleaned.startswith(".") and cleaned.count(".") == 1:
        raise ApiError("E1003", detail="hidden-name operations are not allowed")
    return cleaned


def is_dotfile_path(relative: str) -> bool:
    """路径中任意一段是点文件（以 ``.`` 开头且不是 ``.``/``..``）即为真。

    ``a/..b`` 这类以两个点开头的普通文件名不算点文件，避免误伤。
    """

    for part in str(relative or "").replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        if part.startswith(".") and not part.startswith(".."):
            return True
    return False


def ensure_not_dotfile(relative: str, *, enabled: bool) -> None:
    if enabled and is_dotfile_path(relative):
        raise ApiError("E1003", detail="access to dotfiles is disabled")


def list_directory(
    root: Path, directory: Path, *, hide_dotfiles: bool = False
) -> dict[str, object]:
    if not directory.is_dir():
        raise ApiError("E1001", detail="not a directory")
    entries = []
    for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if hide_dotfiles and child.name.startswith("."):
            continue
        try:
            stat = child.lstat()
        except OSError:
            continue
        is_link = child.is_symlink()
        entry = {
            "name": child.name,
            "path": relative_to_root(root, child),
            "type": "dir" if child.is_dir() else "file",
            "size": 0 if child.is_dir() else int(stat.st_size),
            "mtime": int(stat.st_mtime),
            "symlink": is_link,
        }
        if is_link and not child.exists():
            entry["type"] = "broken-link"
            entry["symlink"] = True
        entries.append(entry)
    return {
        "path": relative_to_root(root, directory),
        "parent": _parent_path(root, directory),
        "entries": entries,
    }


def _parent_path(root: Path, directory: Path) -> str | None:
    if directory == root:
        return None
    return relative_to_root(root, directory.parent)


def safe_copy_into(target: Path, stream, *, max_bytes: int) -> int:
    """把上传流写入 ``target``，超过上限则删除半成品并报错。"""

    if target.exists() and target.is_dir():
        raise ApiError("E1001", detail="target is a directory")
    written = 0
    try:
        with target.open("wb") as handle:
            while True:
                chunk = stream.read(1024 * 256)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ApiError(
                        "E1001", detail=f"file exceeds upload limit ({max_bytes} bytes)"
                    )
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except ApiError:
        _silent_unlink(target)
        raise
    except OSError as exc:
        _silent_unlink(target)
        raise ApiError("E1006", detail=f"write failed: {exc}") from exc
    return written


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        raise ApiError("E9001", detail="path does not exist")


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


def iter_preview_bytes(path: Path, limit: int = 1024 * 1024) -> Iterable[bytes]:
    with path.open("rb") as handle:
        remaining = limit
        while remaining > 0:
            chunk = handle.read(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
