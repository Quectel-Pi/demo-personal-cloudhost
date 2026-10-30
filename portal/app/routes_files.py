"""内置文件接口（File Browser 降级方案，设计文档 F3 / A2 / 第 18 章）。

主路径仍然是 File Browser + Caddy ``forward_auth``；当 T1 验证不通过时，
把 ``config.yaml`` 里的文件入口切到本模块（``/api/files``），验收项 A2
（上传 + 下载 100MB、SHA256 一致、期间状态接口 P95 ≤ 1s）行为不变。

所有操作限定在 ``config.files_root`` 内（home 模式下即用户 home），
路径校验见 :mod:`app.files`。
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import audit as audit_events
from .config import Config
from .deps import client_ip, current_session, require_csrf
from .errors import ApiError
from .files import (
    ensure_not_dotfile,
    list_directory,
    relative_to_root,
    remove_path,
    resolve_cloud_path,
    validate_filename,
)

router = APIRouter(prefix="/api/files", tags=["files"])

_CHUNK = 1024 * 256


def _config(request: Request) -> Config:
    return request.app.state.config


def _root(config: Config) -> Path:
    """文件入口根目录：home 模式下是用户 home，cloud 模式是旧行为。"""

    return config.files_root


@router.get("")
async def list_files(request: Request, path: str = "") -> dict[str, Any]:
    current_session(request)
    config = _config(request)
    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    directory = resolve_cloud_path(_root(config), path)
    if not directory.is_dir():
        raise ApiError("E1001", detail="not a directory")
    payload = list_directory(
        _root(config), directory, hide_dotfiles=config.file_block_dotfiles
    )
    payload["writable"] = os.access(directory, os.W_OK)
    return payload


@router.get("/download")
async def download(request: Request, path: str) -> FileResponse:
    current_session(request)
    config = _config(request)
    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    target = resolve_cloud_path(_root(config), path)
    if not target.is_file():
        raise ApiError("E9001", detail="file not found")
    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(
        target,
        media_type=media_type or "application/octet-stream",
        filename=target.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/preview")
async def preview(request: Request, path: str) -> Response:
    """文本/图片预览；限制 1MB，避免大文件拖慢门户。"""

    current_session(request)
    config = _config(request)
    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    target = resolve_cloud_path(_root(config), path)
    if not target.is_file():
        raise ApiError("E9001", detail="file not found")
    media_type, _ = mimetypes.guess_type(target.name)
    media_type = media_type or "application/octet-stream"
    if media_type.startswith(("video/", "audio/")) or target.stat().st_size > 1024 * 1024:
        raise ApiError("E1001", detail="preview not available for this file type or size")
    if not (media_type.startswith("text/") or media_type in ("application/json", "image/png", "image/jpeg", "image/gif", "image/webp")):
        raise ApiError("E1001", detail="preview not supported")
    return FileResponse(
        target,
        media_type=media_type,
        headers={
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.post("/upload")
async def upload(request: Request, path: str = "", name: str | None = None, overwrite: bool = False):
    session = current_session(request)
    require_csrf(request, session)
    config = _config(request)

    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    directory = resolve_cloud_path(_root(config), path)
    if not directory.is_dir():
        raise ApiError("E1001", detail="upload target must be a directory")

    filename = validate_filename(
        name or request.headers.get("x-filename") or request.query_params.get("filename")
    )
    ensure_not_dotfile(filename, enabled=config.file_block_dotfiles)
    parent_rel = relative_to_root(_root(config), directory).strip("/")
    target = resolve_cloud_path(
        _root(config), f"{parent_rel}/{filename}" if parent_rel else filename
    )
    if target.exists() and not overwrite:
        raise ApiError("E1001", detail="file already exists; pass overwrite=true to replace")

    body_stream = request.stream()
    written = await _write_body(target, body_stream, config.max_upload_mb * 1024 * 1024)
    request.app.state.audit.write(
        audit_events.ACTION_FILE_UPLOAD,
        user=session.user,
        client_ip=client_ip(request),
        path=relative_to_root(_root(config), target),
        bytes=written,
    )
    return JSONResponse(
        status_code=201,
        content={"path": relative_to_root(_root(config), target), "size": written},
    )


async def _write_body(target, stream, max_bytes: int) -> int:
    written = 0
    try:
        with target.open("wb") as handle:
            async for chunk in stream:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ApiError("E1001", detail=f"file exceeds upload limit ({max_bytes} bytes)")
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except ApiError:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise ApiError("E1006", detail=f"write failed: {exc}") from exc
    return written


@router.post("/mkdir", status_code=201)
async def mkdir(request: Request, path: str = "", name: str | None = None) -> dict[str, Any]:
    session = current_session(request)
    require_csrf(request, session)
    config = _config(request)
    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    parent = resolve_cloud_path(_root(config), path)
    if not parent.is_dir():
        raise ApiError("E1001", detail="parent must be a directory")
    folder = validate_filename(name)
    ensure_not_dotfile(folder, enabled=config.file_block_dotfiles)
    target = resolve_cloud_path(
        _root(config), f"{relative_to_root(_root(config), parent).lstrip('/')}/{folder}"
    )
    if target.exists():
        raise ApiError("E1001", detail="already exists")
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        raise ApiError("E1006", detail=f"mkdir failed: {exc}") from exc
    return {"path": relative_to_root(_root(config), target)}


@router.post("/rename", status_code=200)
async def rename(request: Request, path: str, name: str | None = None) -> dict[str, Any]:
    session = current_session(request)
    require_csrf(request, session)
    config = _config(request)
    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    source = resolve_cloud_path(_root(config), path)
    if source == _root(config).resolve():
        raise ApiError("E1003", detail="cannot rename cloud root")
    if not source.exists():
        raise ApiError("E9001", detail="path not found")
    new_name = validate_filename(name)
    ensure_not_dotfile(new_name, enabled=config.file_block_dotfiles)
    destination = source.with_name(new_name)
    if destination.exists():
        raise ApiError("E1001", detail="destination already exists")
    try:
        source.rename(destination)
    except OSError as exc:
        raise ApiError("E1006", detail=f"rename failed: {exc}") from exc
    return {"path": relative_to_root(_root(config), destination)}


@router.delete("/delete", status_code=204)
async def delete(request: Request, path: str) -> Response:
    session = current_session(request)
    require_csrf(request, session)
    config = _config(request)
    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    target = resolve_cloud_path(_root(config), path)
    if target == _root(config).resolve():
        raise ApiError("E1003", detail="cannot delete cloud root")
    if not target.exists() and not target.is_symlink():
        raise ApiError("E9001", detail="path not found")
    rel = relative_to_root(_root(config), target)
    remove_path(target)
    request.app.state.audit.write(
        audit_events.ACTION_FILE_DELETE,
        user=session.user,
        client_ip=client_ip(request),
        path=rel,
    )
    return Response(status_code=204)


@router.get("/stat")
async def stat(request: Request, path: str) -> dict[str, Any]:
    current_session(request)
    config = _config(request)
    ensure_not_dotfile(path, enabled=config.file_block_dotfiles)
    target = resolve_cloud_path(_root(config), path)
    if not target.exists():
        raise ApiError("E9001", detail="path not found")
    info = target.stat()
    return {
        "path": relative_to_root(_root(config), target),
        "type": "dir" if target.is_dir() else "file",
        "size": 0 if target.is_dir() else int(info.st_size),
        "mtime": int(info.st_mtime),
    }


__all__ = ["router"]
