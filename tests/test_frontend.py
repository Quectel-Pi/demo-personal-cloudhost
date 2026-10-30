"""前端静态资源回归检查（防止「整页 JS 语法错误」与「跳出新页面」再次出现）。

历史问题一：app.js 的 HTML_ESCAPES 表里，双引号那一项曾被写成
一个未闭合的字符串字面量（连续三个双引号），导致整个 IIFE 解析失败，
看板永远停在「连接中…」，系统状态与服务列表都不渲染。这里改为断言
转义值使用 \\uXXXX 形式，并且不出现连续三个双引号。

历史问题二：index.html 曾用 target=_blank 打开文件/站点/终端，
用户希望在本页内切换，因此改为 iframe 单页视图。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "portal" / "static"

TRIPLE_QUOTE = '"' * 3

from conftest import TEST_PASSWORD, TEST_USER  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_app_js_escape_table_uses_unicode_escapes():
    js = _read("app.js")
    block = re.search(r"var HTML_ESCAPES = \{(.*?)\};", js, re.S)
    assert block, "app.js 缺少 HTML_ESCAPES 定义"
    body = block.group(1)
    # 键可能是 "x" 也可能是 'x'（双引号键只能用单引号包裹），两种都要认
    pairs = re.findall(r"""["'](.)["']\s*:\s*"([^"]*)\"""", body)
    assert len(pairs) >= 5, body
    for key, value in pairs:
        assert value.startswith("\\u0"), f"转义值必须以 \\uXXXX 开头：{key!r} -> {value!r}"
    assert TRIPLE_QUOTE not in body, "HTML_ESCAPES 出现连续三个双引号（历史语法错误）"


def test_only_official_remote_and_explicit_site_link_open_new_tabs():
    html = _read("index.html")
    links = []

    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag == "a":
                links.append(dict(attrs))

    Links().feed(html)
    external = [link for link in links if link.get("target") == "_blank"]
    assert {link["href"] for link in external if link.get("href")} == {"/site/"}
    for link in external:
        assert set(link["rel"].split()) == {"noopener", "noreferrer"}
        if link.get("href") == "/site/":
            assert link["target"] == "_blank"
            assert link["referrerpolicy"] == "no-referrer"
            assert link.get("id") in {"site-open-tab", "overview-site-link"}
            assert "data-view" not in link
            if link["id"] == "site-open-tab":
                assert link["data-i18n"] == "embed.openSiteTab"
            else:
                assert link["data-i18n-aria-label"] == "dashboard.siteCardNewTab"
        if not link.get("href"):
            assert "hidden" in link
    for view in ("files", "site"):
        assert 'data-view="%s"' % view in html
    assert 'id="embed-frame"' in html
    assert 'id="term-tab"' in html


def test_static_assets_are_cache_busted():
    """版本号作为查询串，避免板端升级后浏览器仍用旧 app.js（旧文件有语法错误）。"""

    for name in ("index.html", "login.html"):
        assert "/static/styles.css?v={{VERSION}}" in _read(name), name
    assert "/static/app.js?v={{VERSION}}" in _read("index.html")


def test_peanut_mapping_has_no_redundant_help_or_normal_console(authed):
    html = authed.get("/").text
    assert 'id="peanut-ssh-guide"' not in html
    for key in ("mappingInventory", "mappingHelp", "managementHelp"):
        assert f'data-i18n="peanut.{key}"' not in html
    assert "ssh.example.invalid" not in html
    assert "3ix9278jg908.vicp.fun" not in html
    assert "/static/i18n.js?v=" in html and "&amp;ui=38" in html
    assert 'id="peanut-mapping-step"' not in html
    assert 'id="peanut-console"' not in html
    assert 'data-peanut-operation="create_mapping"' not in html


def test_hidden_attribute_wins_over_button_flex():
    css = _read("styles.css")
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css), (
        "styles.css 必须让 [hidden] 覆盖 .btn 的 inline-flex，否则 ttyd 关闭时终端按钮仍然可见"
    )


def test_pages_render_without_leftover_placeholders(authed):
    """首页与登录页的所有 {{...}} 占位符都必须在服务端替换掉。"""

    dashboard = authed.get("/")
    assert dashboard.status_code == 200
    assert "{{" not in dashboard.text
    assert "styles.css?v=1.0" in dashboard.text or "styles.css?v=" in dashboard.text

    client = authed
    client.cookies.clear()
    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert "{{" not in login_page.text


def test_ssh_entry_is_rendered_with_real_values(authed):
    """SSH 卡片必须给出可复制的真实命令，而不是 ``<板子IP>`` 占位符。"""

    page = authed.get("/")
    assert page.status_code == 200
    text = page.text
    assert 'id="remote-ssh-title"' in text
    assert 'id="view-ssh"' not in text
    assert 'id="ssh-command"' in text
    assert 'data-copy="ssh pi@' in text
    assert "ssh://pi@" in text
    assert "{{SSH_" not in text
    assert "<板子IP>" not in text
    assert 'id="ssh-public-not-configured"' in text
    assert 'id="ssh-public-command" hidden></code>' in text


def test_app_js_has_ssh_copy_and_scroll_helpers():
    js = _read("app.js")
    assert "function copyText(" in js
    assert "[data-copy]" in js
    assert "[data-scroll]" in js
    # 局域网 HTTP 下 Clipboard API 不可用，必须有 textarea 兜底
    assert "execCommand(\"copy\")" in js or "execCommand('copy')" in js


def test_app_js_has_view_switching():
    js = _read("app.js")
    assert "function setView(" in js
    assert "VIEWS" in js
    for path in ('"/files/"', '"/site/"', '"/term/"'):
        assert path in js
    assert "location.href" not in js
    assert "window.open" not in js


def test_remote_status_panel_is_read_only_and_cache_busted(authed):
    response = authed.get("/")
    assert response.status_code == 200
    assert 'data-view="remote"' in response.text
    assert 'id="remote-status-panel"' in response.text
    assert 'id="refresh-remote"' in response.text
    assert "/static/app.js?v=" in response.text and "&ui=39" in response.text
    assert "qrcode" not in response.text.lower()
    assert "device_sn" not in response.text
    i18n = _read("i18n.js")
    assert '"remote.updated": "Response generated"' in i18n
    assert '"remote.updated": "响应生成时间"' in i18n

    js = _read("app.js")
    assert 'apiFetch("/api/remote/status", { cache: "no-store" })' in js
    assert "if (remoteStatusRequest) { return remoteStatusRequest; }" in js
    assert 'refreshButton.disabled = true' in js
    assert 'remoteLabel(value)' in js
    assert 'setText("remote-provider"' in js
    assert 'setText("remote-reachability"' in js


def test_ssh_is_integrated_with_remote_and_not_overview_content():
    html = _read("index.html")
    overview = html.split('id="view-overview"', 1)[1].split("</main>", 1)[0]
    assert 'id="remote-status-panel"' not in overview
    assert 'id="ssh-card"' not in overview
    assert 'id="view-remote" hidden' in html
    assert 'id="view-ssh"' not in html
    assert 'id="remote-ssh-title"' in html
    for view in ("remote", "ssh"):
        assert f'data-view="{view}"' in overview
    js = _read("app.js")
    assert '$("view-remote").hidden = name !== "remote"' in js
    assert 'var sshShortcut = name === "ssh"' in js
    assert 'name = "remote"' in js


def test_navigation_uses_links_and_shared_compact_breakpoint():
    css = _read("styles.css")
    assert "8.5rem" not in css
    assert ".topnav > .btn { white-space: nowrap; }" in css
    assert ".detail-page { grid-template-columns: minmax(0, 1fr); }" in css
    html = _read("index.html")
    for view in ("overview", "files", "site", "remote", "ssh", "term"):
        assert re.search(rf'<a\b[^>]*href="#{view}"[^>]*data-view="{view}"', html)
    for name in ("nav-toggle", "tools-toggle"):
        assert re.search(rf'id="{name}"[^>]*aria-controls="[^"]+"[^>]*aria-expanded="false"', html)
    js = _read("app.js")
    assert 'matchMedia("(max-width: 860px)")' in js
    assert "replaceState" not in js and "pushState" not in js
    assert 'event.key === "Escape"' in js


def test_separate_workspaces_preserve_site_and_release_terminal():
    html = _read("index.html")
    for frame in ("embed-frame", "site-frame", "term-frame"):
        assert f'id="{frame}"' in html
    assert 'data-i18n="embed.backOverview"' not in html
    assert "hero-diagram" not in html and "purpose-number" not in html
    js = _read("app.js")
    assert 'previous === "term" && name !== "term"' in js
    assert 'window.addEventListener("pagehide", releaseTerminal)' in js
    assert 'hideEmbedHint();' in js
    assert 'showEmbedHint(name === "term"' not in js


def test_remote_progressive_disclosure_keeps_gates_and_dirty_address():
    html = _read("index.html")
    for removed in ("peanut-settings", "peanut-advanced", "peanut-account-maintenance", "peanut-external"):
        assert f'id="{removed}"' not in html
    assert 'id="peanut-summary"' in html
    assert 'id="peanut-next-action"' in html
    js = _read("peanut.js")
    assert 'if (!addressDirty)' in js
    assert 'state.binding.state === "unknown"' in js
    for gate in ("qr_binding", "management"):
        assert f'!capabilities.{gate}' in js
    assert 'id="peanut-binding-step"' in html
    assert 'id="peanut-address-entry"' in html
    assert 'data-i18n="flow.addressSource"' in html
    assert "function continueEnable()" in js
    assert 'value.status === "succeeded"' in js
    assert 'state.binding.state === "bound" && !state.binding.stale' in js
    assert 'data-peanut-operation="check_config"' not in html
    assert 'data-peanut-operation="start"' not in html
    assert 'id="peanut-intent"' in html
    assert "data-peanut-operation" not in html
    assert "data-peanut-operation" not in js
    assert "peanut/external-confirmations" not in js
    assert "workbench.verifyNext" not in js


def test_embedded_workspace_is_flat_and_keeps_error_feedback():
    css = _read("styles.css")
    panel = css.split(".workspace-panel {", 1)[1].split("}", 1)[0]
    assert "box-shadow" not in panel
    assert "border:" not in panel
    frame = css.split(".workspace-frame {", 1)[1].split("}", 1)[0]
    assert "border: 0;" in frame
    assert "min-height: 0;" in frame
    assert css.count(".workspace-frame {") == 1
    assert css.count(".workspace-panel {") == 1
    assert '--portal-header-height' in css
    assert '.embed-hint[data-severity="warning"]' in css
    js = _read("app.js")
    assert 'hint.dataset.severity' in js
    assert 'document.body.classList.toggle("workspace-open", embedded)' in js


def test_workspace_actions_are_contextual_and_files_have_gutters():
    html = _read("index.html")
    assert 'id="site-preview-label"' in html
    assert 'id="site-open-tab" href="/site/" target="_blank"' in html
    assert 'class="btn btn-ghost btn-sm" id="embed-refresh"' in html
    css = _read("styles.css")
    assert '.workspace[data-view="files"]' in css
    assert "padding-inline: clamp(12px, 2.5vw, 32px);" in css
    assert "--workspace-width: 1280px;" in css
    assert "max-width: var(--workspace-width, none);" in css
    assert ".workspace-bar { justify-content: flex-end;" not in css
    js = _read("app.js")
    assert '$("site-open-tab").hidden = name !== "site"' in js


def test_remote_management_uses_one_primary_action_without_second_link(authed):
    html = authed.get("/").text
    assert 'class="remote-official"' not in html
    assert 'data-i18n="workbench.remoteHelp"' not in html
    actions = html.split('id="peanut-primary-actions"', 1)[1].split("</div>", 1)[0]
    assert 'id="peanut-next-action"' in actions
    assert 'id="peanut-management-link"' not in html
    js = _read("peanut.js")
    assert 'window.open("about:blank", "_blank")' in js
    assert "opened.opener = null" in js
    assert "opened.location.replace(value.redirect_path)" in js
    assert "invalid_management_redirect" in js
    assert 'id="peanut-mapping-step"' not in html
    assert 'id="peanut-console"' not in html
    for obsolete in ("peanut.mappingHelp", "peanut.managementHelp", "peanut.mappingInventory"):
        assert f'data-i18n="{obsolete}"' not in html


@pytest.mark.parametrize(
    ("port", "command"),
    [(22, "ssh tris@ssh.example.net"), (41042, "ssh -p 41042 tris@ssh.example.net")],
)
def test_public_ssh_command_uses_only_explicit_tcp_configuration(
    config_with, fake_auth, port, command
):
    cfg = config_with(
        ssh_user="tris", ssh_port=2222,
        remote_ssh_host="ssh.example.net", remote_ssh_port=port,
    )
    app = create_app(cfg, authenticator=fake_auth)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.post(
            "/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD}
        ).status_code == 204
        text = client.get("/").text
    public_command = re.search(r'<code id="ssh-public-command"\s*>(.*?)</code>', text)
    assert public_command and public_command.group(1) == command
    assert f'data-copy="{command}"' in text
    assert 'id="ssh-command">ssh -p 2222 tris@' in text


# --------------------------------------------------------------------------- #
# 6.6 登录页与看板展示系统账号 / home
# --------------------------------------------------------------------------- #
def test_login_prefills_system_user(client, config):
    page = client.get("/login")
    assert page.status_code == 200
    assert 'value="%s"' % config.system_user in page.text
    assert "开发板系统账号" in page.text
    assert "{{" not in page.text


def test_index_shows_files_root_and_user(authed, config):
    page = authed.get("/")
    assert page.status_code == 200
    assert str(config.files_root) in page.text
    assert config.system_user in page.text
    assert "{{" not in page.text


def test_index_no_cloud_root_when_home_mode(config_with, home_root, fake_auth):
    cfg = config_with(system_home=home_root, file_root_mode="home")
    app = create_app(cfg, audit_enabled=True, authenticator=fake_auth)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert (
            client.post(
                "/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD}
            ).status_code
            == 204
        )
        text = client.get("/").text

    assert str(home_root) in text
    assert str(cfg.cloud_root) not in text
