"""页面路由：登录页与门户首页（设计文档 F1 / F2 / 5.4）。

页面本身不含业务数据，首屏通过 ``GET /api/overview`` 拉取；未登录时
服务端直接渲染登录页并返回 200，与 SPA 前端行为保持一致。
"""

from __future__ import annotations

import shutil
from html import escape
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .deps import is_remote_admin, optional_session
from .security import CSRF_COOKIE
from .ingress import public_ingress

router = APIRouter(tags=["pages"])


@router.get("/.well-known/cloudhost-peanut-check")
def peanut_challenge(request: Request):
    from fastapi.responses import PlainTextResponse
    from .errors import ApiError

    if not public_ingress(request):
        raise ApiError("E9001")
    value = request.app.state.peanut.call("challenge")
    return PlainTextResponse(value["challenge"], headers={"Cache-Control": "no-store"})

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _render(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def _no_store(response) -> None:
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"


_TTYD_UNIT = Path("/etc/systemd/system/cloudhost-ttyd.service")
_TTYD_BIN = Path("/opt/cloudhost/bin/ttyd")


def _ttyd_available(config) -> bool:
    """ttyd 入口是否真的可用。

    只看 ``ttyd_enabled`` 不够：安装时 apt 取不到 ttyd 会把该值写回 false，
    但已存在的 config.yaml 不会覆盖；反过来配置为 true 时也可能没有渲染出
    systemd unit。任一条件不满足就隐藏入口，避免用户点进 404 / 502。
    """

    if not config.ttyd_enabled:
        return False
    return (
        _TTYD_UNIT.is_file()
        or _TTYD_BIN.is_file()
        or shutil.which("ttyd") is not None
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    session = optional_session(request)
    if session is None:
        return _login_page(request)

    config = request.app.state.config
    html = _render("index.html").replace("{{CSRF}}", session.csrf)
    html = html.replace("{{STATUS_INTERVAL}}", str(config.status_interval_ms))
    html = html.replace("{{TTYD_ENABLED}}", "true" if _ttyd_available(config) else "false")
    html = html.replace("{{FILES_ROOT}}", str(config.files_root))
    html = html.replace("{{VERSION}}", request.app.state.version)
    html = _apply_auth_tokens(html, config)
    html = _apply_ingress_copy(html, request)
    if not is_remote_admin(session, config):
        html = html.replace('id="peanut-reset-options"', 'id="peanut-reset-options" hidden')
    if public_ingress(request):
        html = html.replace('id="ssh-card"', 'id="ssh-card" data-web-only="true"')
    for token, value in _ssh_tokens(request, config).items():
        html = html.replace(token, value)
    response = HTMLResponse(html)
    _no_store(response)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, redirect: str | None = None) -> HTMLResponse:
    session = optional_session(request)
    if session is not None and not redirect:
        return RedirectResponse(url="/", status_code=302)
    html = _render("login.html").replace("{{REDIRECT}}", _safe_redirect(redirect))
    html = html.replace("{{VERSION}}", request.app.state.version)
    html = _apply_auth_tokens(html, request.app.state.config)
    html = _apply_ingress_copy(html, request)
    response = HTMLResponse(html)
    _no_store(response)
    return response


def _auth_tokens(config) -> dict[str, str]:
    """登录表单的预填账号与提示：系统账号模式与 admin.hash 模式不同。"""

    if config.auth_mode == "system":
        return {
            "{{AUTH_MODE}}": "system",
            "{{AUTH_FALLBACK}}": "true" if config.password_hash_fallback else "false",
            "{{LOGIN_USER}}": escape(config.system_user),
            "{{AUTH_HINT}}": (
                f"使用开发板系统账号 <code>{escape(config.system_user)}</code> 的密码登录；"
                "修改系统密码后这里同步生效。"
                + ("PAM 不可用时可使用已配置的管理员后备密码。" if config.password_hash_fallback else "")
            ),
        }
    return {
        "{{AUTH_MODE}}": "password_hash",
        "{{AUTH_FALLBACK}}": "false",
        "{{LOGIN_USER}}": escape(config.admin_user),
        "{{AUTH_HINT}}": "使用门户管理员密码登录。",
    }


def _apply_auth_tokens(html: str, config) -> str:
    for token, value in _auth_tokens(config).items():
        html = html.replace(token, value)
    return html


def _apply_ingress_copy(html: str, request: Request) -> str:
    if public_ingress(request):
        for key in ("experience.boundary", "dashboard.footerPrefix", "login.brandSub", "login.footNetwork"):
            html = html.replace(f'data-i18n="{key}"', f'data-i18n="{key}Public"')
        html = html.replace(
            "LAN edition · HTTP only. Use a trusted local network; this connection is not encrypted.",
            "Public HTTPS portal. Signed-in users retain the existing Web permissions.",
        ).replace("Quectel Pi M2 · v1.0 LAN edition", "Quectel Pi M2 · HTTPS portal").replace(
            "Local network access only. Password sign-in is protected by an attempt limit.",
            "Public HTTPS access. Password sign-in is protected by an attempt limit.",
        ).replace(
            "LAN edition · HTTP port 80 only · Status refresh every ",
            "Public HTTPS · Status refresh every ",
        )
    return html


def _ssh_tokens(request: Request, config) -> dict[str, str]:
    """生成 SSH 入口卡片所需的占位符。

    LAN 主机名取浏览器访问本页时的 Host。公网 SSH 使用单独部署配置，
    不能从当前 HTTPS 主机推断。
    """

    host = "LAN_ADDRESS" if public_ingress(request) else (request.url.hostname or "").strip()
    if not host:
        host = request.headers.get("host", "").split(":")[0].strip() or "127.0.0.1"
    user = (config.ssh_user or "pi").strip()
    port = int(config.ssh_port)

    command = f"ssh {user}@{host}" if port == 22 else f"ssh -p {port} {user}@{host}"
    uri_host = f"[{host}]" if ":" in host else host
    uri = "" if public_ingress(request) else f"ssh://{user}@{uri_host}:{port}"
    public_host = "" if config.remote_provider == "peanut" else config.remote_ssh_host
    public_port = config.remote_ssh_port
    public_command = ""
    if public_host:
        public_command = (
            f"ssh {user}@{public_host}" if public_port == 22
            else f"ssh -p {public_port} {user}@{public_host}"
        )
    return {
        "{{SSH_CMD}}": escape(command),
        "{{SSH_URI}}": escape(uri, quote=True),
        "{{SSH_USER}}": escape(user),
        "{{SSH_HOST}}": escape(host),
        "{{SSH_PORT}}": str(port),
        "{{SSH_KEY_ONLY}}": "true" if config.ssh_key_only else "false",
        "{{PUBLIC_SSH_CMD}}": escape(public_command),
        "{{PUBLIC_SSH_CMD_ATTR}}": escape(public_command, quote=True),
        "{{PUBLIC_SSH_HOST_ATTR}}": escape(public_host, quote=True),
        "{{PUBLIC_SSH_PORT}}": str(public_port),
        "{{PUBLIC_SSH_STATUS_HIDDEN}}": "hidden" if public_host else "",
        "{{PUBLIC_SSH_COMMAND_HIDDEN}}": "" if public_host else "hidden",
    }


def _login_page(request: Request) -> HTMLResponse:
    session = optional_session(request)
    html = _render("login.html").replace("{{REDIRECT}}", "/")
    html = html.replace("{{VERSION}}", request.app.state.version)
    html = _apply_auth_tokens(html, request.app.state.config)
    html = _apply_ingress_copy(html, request)
    if session is not None:
        html = html.replace("{{CSRF}}", session.csrf)
    else:
        html = html.replace("{{CSRF}}", "")
    response = HTMLResponse(html)
    _no_store(response)
    return response


_GATEWAY_HINTS = {
    "/files": "文件服务",
    "/site": "静态站点",
    "/term": "Web 终端",
}


def _gateway_fallback(prefix: str) -> HTMLResponse:
    """Caddy 未把入口转发到后端时的兜底页。

    正常情况下 ``/files/``、``/site/``、``/term/`` 由 Caddy 直接代理到
    File Browser / file_server / ttyd，不会走到门户。一旦请求落到这里，
    说明板端 Caddyfile 仍是旧版本（例如先 --no-ttyd 部署、之后又打开了
    ttyd_enabled），此时返回 FastAPI 默认的 ``{"detail":"Not Found"}``
    对用户毫无帮助，改为可读的 HTML 提示。
    """

    label = _GATEWAY_HINTS.get(prefix, "入口")
    service = {
        "/files": "cloudhost-files",
        "/term": "cloudhost-ttyd",
        "/site": "caddy",
    }.get(prefix, "caddy")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title data-i18n="embed.backendUnavailable">Backend is unavailable · Pi M2</title>
  <script src="/static/theme-boot.js?ui=36"></script>
  <script src="/static/i18n.js?ui=36"></script>
  <link rel="stylesheet" href="/static/styles.css?ui=36" />
  <link rel="stylesheet" href="/static/themes.css?ui=37" />
  <script src="/static/theme.js?ui=36" defer></script>
</head>
<body class="login-body">
  <main class="login-card" role="main">
    <header class="brand">
      <span class="brand-mark" aria-hidden="true">M2</span>
      <div>
        <h1 data-i18n="embed.backendUnavailable">Backend is unavailable</h1>
        <p class="brand-sub">{prefix}/ · {service}</p>
      </div>
    </header>
    <div class="theme-control" data-theme-control>
      <button class="btn btn-outline theme-toggle" id="theme-toggle" type="button" aria-controls="theme-panel" aria-expanded="false" hidden>Theme</button>
    </div>
    <div class="language-controls" role="group" aria-label="Language">
      <button class="btn" type="button" data-language="en">EN</button>
      <button class="btn" type="button" data-language="zh">中文</button>
    </div>
    <p class="theme-status" data-theme-status role="status" aria-live="polite" data-i18n="theme.unavailable">Theme switching is unavailable. The basic page remains usable.</p>
    <p id="gateway-message" class="hint"></p>
    <p class="hint"><code>sudo ./install.sh && sudo systemctl restart caddy</code></p>
    <p class="hint"><code>systemctl status {service}</code></p>
    <p class="hint"><a class="btn btn-outline" href="/" data-i18n="embed.backOverview">Back to overview</a></p>
  </main>
  <script>
    function renderGatewayMessage() {{
      document.getElementById("gateway-message").textContent =
        window.CloudhostI18n.t("embed.gatewayMissing", {{path: "{prefix}/"}});
    }}
    document.addEventListener("DOMContentLoaded", renderGatewayMessage);
    window.addEventListener("cloudhost:languagechange", renderGatewayMessage);
  </script>
</body>
</html>
"""
    response = HTMLResponse(html, status_code=404)
    response.headers["X-Cloudhost-Gateway-Fallback"] = "true"
    _no_store(response)
    return response


@router.get("/files", include_in_schema=False)
@router.get("/files/", include_in_schema=False)
@router.get("/site", include_in_schema=False)
@router.get("/site/", include_in_schema=False)
@router.get("/term", include_in_schema=False)
@router.get("/term/", include_in_schema=False)
async def gateway_fallback(request: Request) -> HTMLResponse:
    prefix = "/" + request.url.path.strip("/").split("/")[0]
    return _gateway_fallback(prefix)


def _safe_redirect(value: str | None) -> str:
    """只接受站内路径，避免登录后被带到外部站点。"""

    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or "\n" in value or "\r" in value:
        return "/"
    return value


__all__ = ["router", "CSRF_COOKIE"]
