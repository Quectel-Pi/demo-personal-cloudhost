"""Real-browser redesign checks; no host deployment or system service changes.

Optional: CLOUDHOST_CHROMIUM selects an existing Chromium executable.
CLOUDHOST_{CADDY,FILEBROWSER,TTYD} enable official-binary integration.
CLOUDHOST_EVIDENCE_DIR saves reproducible screenshots and measured layouts.
All listeners are loopback high ports; all data and logs use pytest's base tree.
"""

from __future__ import annotations

import base64
from dataclasses import replace
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from fastapi.responses import FileResponse
from starlette.routing import Route

from app.main import create_app
from app import routes_pages
from app.services import ServiceManager
from conftest import TEST_PASSWORD, TEST_USER

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
import filebrowser_branding
import render_config

VIEWPORTS = [(1440, 900), (1280, 800), (375, 812), (390, 844)]


def free_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def wait_http(url, processes=()):
    for _ in range(100):
        if any(process.poll() is not None for process in processes):
            raise AssertionError("Isolated test process exited; inspect pytest base-tree logs")
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.05)
    raise AssertionError(f"Isolated listener not ready: {url}")


@pytest.fixture
def live_portal(config, fake_auth, monkeypatch):
    config = replace(config, remote_admin_users=(TEST_USER,))
    app = create_app(config, authenticator=fake_auth)
    app.state.service_manager = ServiceManager(
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, "ActiveState=active\nUnitFileState=enabled\n", ""
        )
    )
    restarted = []
    monkeypatch.setattr(app.state.service_manager, "restart", lambda name, **kw: restarted.append(name))

    async def site(request):
        return FileResponse(PROJECT / "deploy" / "site-index.html")

    # Only the test harness serves Site without Caddy; production routes are untouched.
    app.router.routes.insert(0, Route("/site/", site))
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    wait_http(base + "/api/healthz")
    yield SimpleNamespace(app=app, base=base, restarted=restarted, config=config)
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()


@pytest.fixture
def browser():
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as playwright:
        engine = os.getenv("CLOUDHOST_BROWSER", "chromium")
        instance = getattr(playwright, engine).launch(
            executable_path=os.getenv("CLOUDHOST_" + engine.upper()) or None
        )
        yield instance
        instance.close()


def collect_page(page, label, evidence, diagnostics):
    page.evaluate("document.fonts.ready")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
    assert "{{" not in page.locator("body").inner_text(), label
    targets = page.locator("button:visible, .btn:visible, summary:visible").evaluate_all(
        "nodes => nodes.map(n => ({text:n.textContent.trim(),w:n.getBoundingClientRect().width,h:n.getBoundingClientRect().height})).filter(n => n.w < 44 || n.h < 44)"
    )
    assert not targets, (label, targets)
    assert not diagnostics["errors"], diagnostics
    assert not diagnostics["missing"], diagnostics
    assert not diagnostics["externalRequests"], diagnostics
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence / f"{label}.png"), full_page=True)
        (evidence / f"{label}.json").write_text(json.dumps({
            "url": page.url, "viewport": page.viewport_size,
            "documentWidth": page.evaluate("document.documentElement.scrollWidth"),
            "browser": page.context.browser.version, "diagnostics": diagnostics,
            "smallTargets": targets,
            "navigationTiming": page.evaluate("(() => {const n=performance.getEntriesByType('navigation')[0];return {domContentLoaded:n.domContentLoadedEventEnd,load:n.loadEventEnd};})()"),
        }, indent=2), encoding="utf-8")


def diagnostics_for(page):
    result = {"errors": [], "missing": [], "externalRequests": []}
    page.on("pageerror", lambda error: result["errors"].append(str(error)))
    page.on("console", lambda message: result["errors"].append(message.text)
            if message.type == "error" and "Failed to load resource" not in message.text else None)
    page.on("response", lambda response: result["missing"].append(response.url)
            if response.status >= 400 and "/favicon.ico" not in response.url else None)
    page.on("request", lambda request: result["externalRequests"].append(request.url)
            if request.url.startswith(("http:", "https:")) and urlsplit(request.url).hostname not in ("127.0.0.1", "localhost", "cloudhost.test") else None)
    return result


def sign_in(page, base):
    page.goto(base + "/login")
    page.locator("#username").fill(TEST_USER)
    page.locator("#password").fill(TEST_PASSWORD)
    page.locator("#login-submit").click()
    page.wait_for_selector("#view-overview")


def navigate_view(page, name):
    if page.locator("#nav-toggle").is_visible():
        page.locator("#nav-toggle").click()
    page.locator(f'.nav-tab[data-view="{name}"]').click()
    page.wait_for_function(
        "name => document.querySelector('.nav-tab[data-view=\"' + name + '\"]').getAttribute('aria-current') === 'page'",
        arg=name,
    )


def change_language(page, name):
    if page.locator("#account-menu").is_hidden():
        page.locator("#tools-toggle").click()
    page.locator(f'[data-language="{name}"]').click()


def mock_remote(page, **changes):
    layer = lambda state, reason=None: {
        "state": state, "source": "browser_fixture", "observed_at": None,
        "stale": False, "reason": reason,
    }
    value = {
        "app_config": layer("valid"), "installation": layer("installed"),
        "daemon": layer("running"), "device": layer("online"),
        "binding": layer("unknown", "adapter_not_verified"), "https": layer("endpoint_ready"),
        "intent": {"enabled": True, "paused_this_boot": False}, "revision": 1,
        "address": {"https_url": "https://example.invalid"},
        "capabilities": {"qr_binding": False, "management": False, "unbind": False,
                         "unavailable_reason": "adapter_not_verified"},
    }
    value.update(changes)
    page.route("**/api/remote/peanut", lambda route: route.fulfill(json=value))
    return value


@pytest.mark.parametrize("size", VIEWPORTS, ids=lambda size: f"{size[0]}x{size[1]}")
@pytest.mark.parametrize("language", ["en", "zh"])
def test_bilingual_real_rendering(browser, live_portal, size, language, monkeypatch):
    monkeypatch.setattr(routes_pages, "_ttyd_available", lambda cfg: False)
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    page = context.new_page()
    diagnostics = diagnostics_for(page)
    evidence = Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) if os.getenv("CLOUDHOST_EVIDENCE_DIR") else None
    suffix = f"{language}-{size[0]}x{size[1]}"
    try:
        page.goto(live_portal.base + "/login")
        page.locator(f'[data-language="{language}"]').click()
        page.keyboard.press("Tab")
        assert page.evaluate("getComputedStyle(document.activeElement).outlineWidth") == "3px"
        page.locator("#username").focus()
        assert page.locator("#username").evaluate("node => getComputedStyle(node).outlineWidth") == "3px"
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
        page.locator("#username").blur()
        collect_page(page, f"login-{suffix}", evidence, diagnostics)
        assert page.locator("#auth-hint").inner_text()
        assert page.locator("#username").input_value() == TEST_USER
        assert page.locator("#password").input_value() == ""
        sign_in(page, live_portal.base)
        page.wait_for_function("document.querySelector('#cpu-percent').textContent !== '—'")
        assert page.locator("html").get_attribute("lang") == language
        assert page.locator("#term-card").is_hidden()
        assert page.locator("#shell-fallback").is_visible()
        # Use cases precede telemetry and services in both DOM and visual order.
        assert page.locator(".purpose-panel").bounding_box()["y"] < page.locator(".metrics").bounding_box()["y"]
        if size[0] >= 1280:
            assert page.locator(".metrics").bounding_box()["y"] < size[1]
            cpu = page.locator("#cpu-percent").bounding_box()
            assert cpu["y"] + cpu["height"] <= size[1]
        collect_page(page, f"portal-{suffix}", evidence, diagnostics)
        assert page.locator(".topbar").bounding_box()["height"] <= 80
        navigate_view(page, "site")
        page.wait_for_function("location.hash === '#site'")
        frame = page.frame_locator("#site-frame")
        frame.locator("#markdown-input").wait_for()
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        editor = frame.locator("#markdown-input")
        editor.click()
        editor.fill("# Browser evidence\n\n[unsafe](javascript:alert(1))\n\n- Real local preview")
        frame.get_by_role("heading", name="Browser evidence", exact=True).wait_for()
        assert frame.locator('#markdown-preview a[href^="javascript:"]').count() == 0
        assert frame.locator("[data-language]").count() == 0
        # Same-origin storage events update the embedded demo, without replacing its document.
        page.locator("#tools-toggle").click()
        page.locator(f'[data-language="{"zh" if language == "en" else "en"}"]').click()
        assert editor.input_value().startswith("# Browser evidence")
        page.locator(f'[data-language="{language}"]').click()
        page.locator("#tools-toggle").click()
        collect_page(page, f"site-{suffix}", evidence, diagnostics)
        page.route("**/files/", lambda route: route.fulfill(
            content_type="text/html", body='<title>Fixture file app</title><h1>Files fixture</h1><input aria-label="Draft filename">'
        ))
        mock_remote(page)
        for view in ("files", "remote", "ssh"):
            navigate_view(page, view)
            assert page.url.endswith("#" + view)
            assert page.title().endswith(" · Pi M2")
            if view == "files":
                page.frame_locator("#embed-frame").get_by_role("heading", name="Files fixture").wait_for()
                page.wait_for_function("document.querySelector('#embed-hint').hidden")
                assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
                geometry = page.locator("#embed-frame").bounding_box()
                gutter = 12 if size[0] < 600 else 32
                assert geometry["x"] >= gutter
                assert size[0] - geometry["x"] - geometry["width"] >= gutter
                assert geometry["width"] <= 1280
                assert page.locator("#embed-refresh").bounding_box()["x"] == pytest.approx(geometry["x"])
                assert page.locator("#site-open-tab").is_hidden()
            if view == "remote":
                page.wait_for_function("document.querySelector('#peanut-summary').textContent.includes('Device') || document.querySelector('#peanut-summary').textContent.includes('设备')")
                assert page.locator("#peanut-settings").count() == 0
                assert page.locator("#peanut-public-address").inner_text() == "https://example.invalid"
            collect_page(page, f"{view}-{suffix}", evidence, diagnostics)
        navigate_view(page, "site")
        with page.expect_download() as download:
            frame.locator('[data-download="markdown"]').click()
        assert download.value.suggested_filename == "pi-m2-example.md"
        editor.fill("")
        assert frame.locator("#markdown-preview").inner_text()
        frame.locator("#reset-example").click()
        assert editor.input_value().startswith("# ")
        page.goto(live_portal.base + "/#term")
        assert page.locator("#view-embed").is_hidden()
        assert page.locator("#term-tab").is_hidden()
        page.locator("#tools-toggle").click()
        page.locator("#logout-btn").click()
        page.wait_for_selector("#login-form")
        assert page.locator("html").get_attribute("lang") == language
    finally:
        context.close()


@pytest.mark.parametrize("width", [1440, 1280, 375, 390])
@pytest.mark.parametrize("locale", ["en", "zh"])
def test_site_preview_and_explicit_new_tab_preserve_draft(browser, live_portal, width, locale):
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    try:
        sign_in(page, live_portal.base)
        change_language(page, locale)
        page.keyboard.press("Escape")
        overview_link = page.locator("#overview-site-link")
        assert overview_link.get_attribute("href") == "/site/"
        assert overview_link.get_attribute("target") == "_blank"
        assert set(overview_link.get_attribute("rel").split()) == {"noopener", "noreferrer"}
        assert "新标签" in overview_link.get_attribute("aria-label") if locale == "zh" else \
            "new tab" in overview_link.get_attribute("aria-label")
        with context.expect_page() as opened_from_overview:
            overview_link.click()
        overview_tab = opened_from_overview.value
        overview_tab.wait_for_load_state()
        overview_tab.locator("#markdown-input").wait_for()
        assert overview_tab.url == live_portal.base + "/site/"
        assert overview_tab.evaluate("window.opener === null")
        assert page.locator("#view-overview").is_visible()
        assert not page.url.endswith("#site")
        overview_tab.close()
        navigate_view(page, "site")
        editor = page.frame_locator("#site-frame").locator("#markdown-input")
        editor.click()
        editor.fill("# Synthetic preview-only draft")
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        refresh = page.locator("#embed-refresh")
        link = page.locator("#site-open-tab")
        assert link.is_visible()
        assert link.get_attribute("href") == "/site/"
        assert link.inner_text() == ("在新标签打开" if locale == "zh" else "Open in new tab")
        refresh_box, link_box = refresh.bounding_box(), link.bounding_box()
        assert refresh_box["y"] == pytest.approx(link_box["y"])
        assert refresh_box["x"] + refresh_box["width"] + 8 <= link_box["x"]
        assert link_box["x"] + link_box["width"] <= width
        assert refresh_box["height"] >= 44 and link_box["height"] >= 44
        refresh.focus()
        assert refresh.evaluate("node => getComputedStyle(node).outlineWidth") == "3px"
        page.keyboard.press("Tab")
        assert link.evaluate("node => node === document.activeElement")
        page.once("dialog", lambda dialog: dialog.dismiss())
        refresh.click()
        assert editor.input_value() == "# Synthetic preview-only draft"
        with context.expect_page() as opened:
            link.click()
        second = opened.value
        second.wait_for_load_state()
        second.locator("#markdown-input").wait_for()
        assert second.url == live_portal.base + "/site/"
        assert second.evaluate("window.opener === null")
        assert second.locator("html").get_attribute("lang") == locale
        assert page.url.endswith("#site")
        assert editor.input_value() == "# Synthetic preview-only draft"
        assert not second.locator("#markdown-input").input_value().startswith("# Synthetic")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        second.close()
        navigate_view(page, "ssh")
        assert link.is_hidden()
        navigate_view(page, "site")
        assert editor.input_value() == "# Synthetic preview-only draft"
    finally:
        context.close()


@pytest.mark.parametrize("width,height", [(1440, 900), (1280, 800), (375, 812), (390, 844)])
def test_page_content_edges_align_across_views(browser, live_portal, width, height):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.route("**/files/", lambda route: route.fulfill(
        content_type="text/html", body="<title>Files fixture</title><h1>Files fixture</h1>"
    ))
    mock_remote(page)
    try:
        sign_in(page, live_portal.base)

        def layout_edges(selector):
            return page.locator(selector).evaluate("""node => {
              const rect = node.getBoundingClientRect();
              const style = getComputedStyle(node);
              return {
                left: rect.left + parseFloat(style.paddingLeft),
                right: rect.right - parseFloat(style.paddingRight)
              };
            }""")

        overview_edges = layout_edges("#view-overview")
        for view in ("remote", "ssh"):
            navigate_view(page, view)
            edges = layout_edges("#view-remote")
            assert edges["left"] == pytest.approx(overview_edges["left"])
            assert edges["right"] == pytest.approx(overview_edges["right"])

        navigate_view(page, "files")
        page.frame_locator("#embed-frame").get_by_role("heading", name="Files fixture").wait_for()
        frame = page.locator("#embed-frame").bounding_box()
        if width >= 1280:
            assert frame["x"] == pytest.approx(overview_edges["left"])
            assert frame["x"] + frame["width"] == pytest.approx(overview_edges["right"])
        else:
            assert frame["x"] >= 12
            assert frame["x"] + frame["width"] <= width - 12
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        context.close()


def test_native_history_menu_focus_and_preserved_drafts(browser, live_portal, monkeypatch):
    monkeypatch.setattr(routes_pages, "_ttyd_available", lambda cfg: True)
    context = browser.new_context(viewport={"width": 375, "height": 812})
    page = context.new_page()
    diagnostics = diagnostics_for(page)
    visits = []
    page.route("**/files/", lambda route: (
        visits.append(route.request.is_navigation_request()),
        route.fulfill(content_type="text/html", body='<input aria-label="Draft filename">')
    )[-1])
    mock_remote(page)
    try:
        sign_in(page, live_portal.base)
        menu = page.locator("#nav-toggle")
        menu.focus()
        page.keyboard.press("Enter")
        assert menu.get_attribute("aria-expanded") == "true"
        assert page.locator('.nav-tab[data-view="overview"]').evaluate("node => node === document.activeElement")
        page.keyboard.press("Escape")
        assert menu.get_attribute("aria-expanded") == "false"
        assert menu.evaluate("node => node === document.activeElement")
        assert menu.evaluate("node => getComputedStyle(node).outlineWidth") == "3px"
        page.set_viewport_size({"width": 1280, "height": 800})
        page.wait_for_function("!document.querySelector('#main-nav').hidden")
        assert page.locator("#main-nav").is_visible()
        assert menu.is_hidden()
        page.set_viewport_size({"width": 375, "height": 812})
        page.wait_for_function("document.querySelector('#main-nav').hidden")
        assert page.locator("#main-nav").is_hidden()
        assert menu.is_visible()
        menu.click()
        for _ in range(6):
            page.keyboard.press("Tab")
        assert page.locator("#theme-toggle").evaluate("node => node === document.activeElement")
        page.keyboard.press("Tab")
        assert page.locator("#tools-toggle").evaluate("node => node === document.activeElement")
        assert menu.get_attribute("aria-expanded") == "false"
        navigate_view(page, "site")
        editor = page.frame_locator("#site-frame").locator("#markdown-input")
        editor.click()
        editor.fill("# Synthetic unsaved draft")
        navigate_view(page, "files")
        file_input = page.frame_locator("#embed-frame").get_by_role("textbox", name="Draft filename")
        file_input.click()
        file_input.fill("synthetic-name")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#embed-refresh").click()
        assert file_input.input_value() == "synthetic-name"
        page.go_back()
        page.wait_for_function("document.activeElement.id === 'embed-title'")
        assert page.url.endswith("#site")
        assert editor.input_value() == "# Synthetic unsaved draft"
        assert page.locator("#embed-title").evaluate("node => node === document.activeElement")
        page.go_forward()
        assert file_input.input_value() == "synthetic-name"
        assert visits.count(True) == 1
        navigate_view(page, "site")
        change_language(page, "zh")
        page.frame_locator("#site-frame").locator('html[lang="zh"]').wait_for()
        assert page.url.endswith("#site")
        assert page.title() == "站点 · Pi M2"
        assert editor.input_value() == "# Synthetic unsaved draft"
        page.keyboard.press("Escape")
        assert page.locator("#tools-toggle").evaluate("node => node === document.activeElement")
        assert page.locator("#tools-toggle").get_attribute("aria-expanded") == "false"
        # Native modifier-click must open the deep link, not replace the current view.
        menu.click()
        with context.expect_page() as opened:
            page.locator('.nav-tab[data-view="ssh"]').click(modifiers=["Control"])
        second = opened.value
        second.wait_for_selector("#view-remote:not([hidden])")
        second.locator("#remote-ssh-title").wait_for()
        assert second.url.endswith("#ssh")
        assert second.locator('.nav-tab[data-view="ssh"]').get_attribute("aria-current") == "page"
        assert page.url.endswith("#site")
        second.close()
        page.keyboard.press("Escape")
        page.reload()
        page.frame_locator("#site-frame").locator("#markdown-input").wait_for()
        assert page.url.endswith("#site")
        assert page.locator('.nav-tab[data-view="site"]').get_attribute("aria-current") == "page"
        assert not diagnostics["errors"]
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("size", VIEWPORTS, ids=lambda size: f"{size[0]}x{size[1]}")
def test_remote_public_addresses_update_independent_ssh_command(browser, live_portal, language, size):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'clipboard', {value: {writeText: text => "
        "{window.copiedText = text; return Promise.resolve();}}})"
    )
    value = mock_remote(page)
    value["binding"].update(state="bound", reason=None)
    value["capabilities"]["management"] = True
    writes, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def save(route):
        data = route.request.post_data_json
        assert route.request.method == "PUT"
        assert route.request.headers.get("x-csrf-token")
        assert route.request.headers.get("idempotency-key")
        assert data["expected_revision"] == value["revision"]
        writes.append(data)
        if data["ssh"]["host"] == "localhost":
            route.fulfill(status=400, json={"error": {"code": "E5001", "detail": "invalid_ssh_mapping"}})
            return
        value["address"].update(https_url=data["https_url"], ssh=data["ssh"])
        value["revision"] += 1
        route.fulfill(json={"revision": value["revision"]})

    page.route("**/api/remote/peanut/address", save)
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        page.keyboard.press("Escape")
        navigate_view(page, "remote")
        page.wait_for_function(
            "() => /^(Device online|设备在线)$/.test(document.querySelector('#peanut-summary').textContent)"
        )
        assert page.locator("#peanut-next").is_hidden()
        assert page.locator("#peanut-management-note").is_hidden()
        assert page.locator("#peanut-address-source").is_hidden()
        assert page.locator('[data-i18n="flow.addressHelp"], [data-i18n="ssh.publicBoundary"], .ssh-help').count() == 0
        assert page.locator("#ssh-public-not-configured").is_visible()
        assert page.locator("#ssh-public-command").is_hidden()
        assert page.locator("#peanut-address-entry > summary").inner_text() == (
            "配置公网地址" if language == "zh" else "Configure public addresses"
        )
        page.locator("#peanut-address-entry > summary").click()
        endpoint = page.locator("#peanut-ssh-address")
        assert page.locator("#peanut-ssh-host, #peanut-ssh-port").count() == 0
        assert endpoint.get_attribute("placeholder") == "host:22"
        endpoint.fill("ssh.example.net:41042")
        page.locator("#refresh-remote").click()
        page.wait_for_timeout(100)
        change_language(page, "zh" if language == "en" else "en")
        page.keyboard.press("Escape")
        assert endpoint.input_value() == "ssh.example.net:41042"
        submit = page.locator('#peanut-address-form button[type="submit"]')
        user = live_portal.config.ssh_user
        https = value["address"]["https_url"]
        cases = [
            ("ssh.example.net:41042", "ssh.example.net", 41042),
            ("other.example.net:22", "other.example.net", 22),
            ("default.example.net", "default.example.net", 22),
            ("ssh.example.net:1", "ssh.example.net", 1),
            ("ssh.example.net:65535", "ssh.example.net", 65535),
            ("", "", 22),
        ]
        for text, ssh_host, ssh_port in cases:
            if not page.locator("#peanut-address-entry").evaluate("node => node.open"):
                page.locator("#peanut-address-entry > summary").click()
            endpoint.fill(text)
            submit.click()
            command = "" if not ssh_host else (
                f"ssh {user}@{ssh_host}" if ssh_port == 22 else f"ssh -p {ssh_port} {user}@{ssh_host}"
            )
            page.wait_for_function(
                "command => document.querySelector('#ssh-public-command').textContent === command",
                arg=command,
            )
            assert writes[-1]["https_url"] == https
            assert writes[-1]["ssh"] == {"host": ssh_host, "port": ssh_port}
            assert page.locator("#ssh-public-copy").get_attribute("data-copy") == command
            if command:
                page.locator("#ssh-public-copy").click()
                page.wait_for_function("command => window.copiedText === command", arg=command)
            else:
                assert page.locator("#ssh-public-not-configured").is_visible()
                assert page.locator("#ssh-public-copy").is_hidden()
            navigate_view(page, "ssh")
            page.reload()
            page.wait_for_function(
                "() => !document.querySelector('#peanut-address-form button[type=\"submit\"]').disabled"
            )
            page.wait_for_function(
                "command => document.querySelector('#ssh-public-command').textContent === command",
                arg=command,
            )
            assert page.locator("#ssh-command").inner_text().startswith(f"ssh {user}@")
            assert endpoint.input_value() == (f"{ssh_host}:{ssh_port}" if ssh_host else "")
            assert page.url.endswith("#ssh")
            navigate_view(page, "remote")
        page.locator("#peanut-address-entry > summary").click()
        for invalid in [
            "https://bad.example.net", "ssh.example.net:", ":22", "ssh.example.net:0",
            "ssh.example.net:65536", "ssh.example.net:-1", "ssh.example.net:2.5",
            "ssh.example.net:2e2", "ssh.example.net/path", "ssh.example.net?port=22",
            "user@ssh.example.net:22", "ssh.example.net:22:23", "ssh example.net:22",
        ]:
            endpoint.fill(invalid)
            submit.click()
            page.wait_for_function(
                "() => /Invalid SSH|SSH 映射信息无效/.test(document.querySelector('#peanut-feedback').textContent)"
            )
            assert endpoint.input_value() == invalid
            assert endpoint.evaluate("node => node === document.activeElement")
            assert len(writes) == len(cases)
        endpoint.fill("localhost:22")
        submit.click()
        page.wait_for_function(
            "() => /Invalid SSH|SSH 映射信息无效/.test(document.querySelector('#peanut-feedback').textContent)"
        )
        page.wait_for_function(
            "() => !document.querySelector('#peanut-address-form button[type=\"submit\"]').disabled"
        )
        assert endpoint.input_value() == "localhost:22"
        assert page.locator("#ssh-public-command").is_hidden()
        assert len(writes) == len(cases) + 1
        assert not errors
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


def test_remote_unknown_gates_dirty_address_and_error(browser, live_portal):
    context = browser.new_context()
    page = context.new_page()
    mock_remote(page)
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "remote")
        page.wait_for_function("document.querySelector('#peanut-summary').textContent === 'Device online'")
        assert "adapter_not_verified" in page.locator("#peanut-next").inner_text()
        assert page.locator(".remote-diagnostics").get_attribute("open") is None
        assert page.locator("#peanut-settings").count() == 0
        for name in ("peanut-qr-create", "peanut-management"):
            assert page.locator("#" + name).is_disabled()
        assert page.locator('[data-peanut-operation="unbind"]').count() == 0
        page.locator("#peanut-address-entry > summary").click()
        address = page.locator("#peanut-address")
        address.fill("https://draft.invalid")
        page.locator("#refresh-remote").click()
        page.wait_for_timeout(200)
        assert address.input_value() == "https://draft.invalid"
        change_language(page, "zh")
        assert address.input_value() == "https://draft.invalid"
        assert page.locator("#peanut-summary").inner_text() == "设备在线"
        page.locator(".remote-diagnostics > summary").click()
        assert "未知" in page.locator("#peanut-layers").inner_text()
        page.keyboard.press("Escape")
        page.route("**/api/remote/peanut", lambda route: route.abort())
        page.locator("#refresh-remote").click()
        page.wait_for_function("document.querySelector('#peanut-summary').textContent.includes('无法获取')")
        assert page.locator("#peanut-next-action").is_disabled()
        assert page.locator("#peanut-feedback").inner_text()
    finally:
        context.close()


@pytest.mark.parametrize(
    ("reason", "english", "chinese"),
    [
        ("missing", "credentials were not provided", "未提供 PHTunnel 应用凭据"),
        ("invalid", "credential configuration is invalid", "凭据配置无效"),
        ("unreadable", "cannot be read", "无法读取"),
        ("unsafe_permissions", "ownership or permissions are unsafe", "所有者或权限不安全"),
    ],
)
def test_remote_credential_errors_explain_deployment_action(
    browser, live_portal, reason, english, chinese
):
    context = browser.new_context()
    page = context.new_page()
    value = mock_remote(page)
    value["daemon"]["state"] = "failed"
    page.route("**/api/remote/peanut/actions", lambda route: route.fulfill(
        status=400, json={"error": {"code": "E5001", "detail": reason}}
    ))
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "remote")
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#peanut-next-action").click()
        page.wait_for_function(
            "reason => document.querySelector('#peanut-feedback').textContent.includes(reason)",
            arg=english,
        )
        change_language(page, "zh")
        assert chinese in page.locator("#peanut-feedback").inner_text()
        assert "app_key" not in page.locator("#peanut-feedback").inner_text()
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("size", VIEWPORTS, ids=lambda size: f"{size[0]}x{size[1]}")
def test_remote_maintenance_removed_in_all_states(browser, live_portal, language, size):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    value = mock_remote(page)
    value["capabilities"].update(qr_binding=True, management=True, unbind=True)
    value["binding"]["state"] = "bound"
    value["https"].update(state="stale", stale=True)
    errors, writes = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: writes.append(request.method + " " + request.url)
            if "/api/remote/" in request.url and request.method not in ("GET", "HEAD") else None)
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        navigate_view(page, "remote")
        for daemon, enabled, paused in (
            ("running", True, False), ("stopped", True, True),
            ("failed", False, False), ("running", False, False),
        ):
            value["daemon"]["state"] = daemon
            value["intent"].update(enabled=enabled, paused_this_boot=paused)
            page.locator("#refresh-remote").click()
            page.wait_for_function(
                """expected => {
                  const t = window.CloudhostI18n.t;
                  const label = v => t('peanut.state.' + v);
                  return document.querySelector('#peanut-intent').textContent ===
                    t('peanut.intentSummary', {enabled: label(expected.enabled ? 'enabled' : 'disabled'),
                      paused: label(expected.paused ? 'yes' : 'no')});
                }""",
                arg={"enabled": enabled, "paused": paused},
            )
            assert page.locator(
                "#peanut-settings, #peanut-advanced, #peanut-account-maintenance, "
                "#peanut-external, [data-peanut-operation]"
            ).count() == 0
            assert page.locator("#peanut-next-action").is_visible()
            assert page.locator("#peanut-address-entry").is_visible()
            assert page.locator(".remote-diagnostics").is_visible()
            assert page.locator("#peanut-management").is_disabled() == (daemon != "running")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        navigate_view(page, "ssh")
        assert page.locator("#ssh-command").is_visible()
        assert not writes
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_remote_resume_without_maintenance_reports_final_result(
    browser, live_portal, language
):
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    value = mock_remote(page)
    operations = []
    jobs = []

    def action(route):
        body = route.request.post_data_json
        operations.append(body)
        route.fulfill(status=202, json={"job_id": "synthetic-start", "status": "queued"})

    def job(route):
        jobs.append(route.request.url)
        value["daemon"]["state"] = "running"
        value["revision"] += 1
        route.fulfill(json={
            "job_id": "synthetic-start", "status": "succeeded",
            "phase": "completed", "reason": "", "next_action": "",
        })

    page.route("**/api/remote/peanut/actions", action)
    page.route("**/api/remote/jobs/synthetic-start", job)
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        navigate_view(page, "remote")
        page.wait_for_function(
            "text => document.querySelector('#peanut-summary').textContent === text",
            arg="Device online" if language == "en" else "设备在线",
        )
        assert ("Boot start: Enabled" if language == "en" else "开机自启：已启用") in \
            page.locator("#peanut-intent").inner_text()
        assert page.locator("#peanut-settings, #peanut-advanced").count() == 0
        assert page.locator("[data-peanut-operation]").count() == 0

        value["daemon"]["state"] = "stopped"
        page.locator("#refresh-remote").click()
        page.wait_for_function(
            "text => document.querySelector('#peanut-summary').textContent.includes(text)",
            arg="Stopped" if language == "en" else "已停止",
        )
        assert page.locator("[data-peanut-operation]").count() == 0
        assert page.locator("#peanut-next-action").inner_text() == (
            "Start remote access" if language == "en" else "启动远程访问"
        )
        value["daemon"]["state"] = "failed"
        page.locator("#refresh-remote").click()
        page.wait_for_function(
            "text => document.querySelector('#peanut-summary').textContent.includes(text)",
            arg="Failed" if language == "en" else "失败",
        )
        assert page.locator("[data-peanut-operation]").count() == 0
        confirmation = []
        page.once("dialog", lambda dialog: (confirmation.append(dialog.message), dialog.dismiss()))
        page.locator("#peanut-next-action").click()
        assert ("Start the SDK now" if language == "en" else "立即启动 SDK") in confirmation[0]
        assert operations == []
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#peanut-next-action").click()
        page.wait_for_function(
            "text => document.querySelector('#peanut-feedback').textContent.includes(text)",
            arg="Succeeded" if language == "en" else "已完成",
        )
        assert [operation["operation"] for operation in operations] == ["start"]
        assert all("confirmed_account_unbind" not in operation for operation in operations)
        assert jobs
        assert "synthetic-start" in page.locator("#peanut-feedback").inner_text()
        change_language(page, "zh" if language == "en" else "en")
        translated_result = "已完成" if language == "en" else "Succeeded"
        page.wait_for_function(
            "text => document.querySelector('#peanut-feedback').textContent.includes(text)",
            arg=translated_result,
        )
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_remote_summary_separates_device_freshness_from_binding(browser, live_portal, language):
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    value = mock_remote(page)
    value["binding"].update(stale=True, reason="binding_unverified")
    value["https"]["stale"] = True
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        page.keyboard.press("Escape")
        navigate_view(page, "remote")
        online = "设备在线" if language == "zh" else "Device online"
        unknown = "设备状态待确认" if language == "zh" else "Device status unavailable"
        page.wait_for_function("text => document.querySelector('#peanut-summary').textContent === text", arg=online)
        assert page.locator("#peanut-management").is_disabled()
        assert page.locator("#peanut-management-link").count() == 0
        assert "binding_unverified" in page.locator("#peanut-next").inner_text()
        page.locator(".remote-diagnostics > summary").click()
        details = page.locator("#peanut-layers")
        assert "binding_unverified" in details.inner_text()
        assert ("未知" if language == "zh" else "Unknown") in details.inner_text()
        binding_detail = details.locator("dt").filter(
            has_text=re.compile("^(账号|Account)$")).locator("xpath=following-sibling::dd[1]")
        assert ("尚未采样" if language == "zh" else "Not observed") in binding_detail.inner_text()
        assert ("过期" if language == "zh" else "stale") not in binding_detail.inner_text().lower()
        for layer in ("device", "daemon"):
            value[layer]["stale"] = True
            page.locator("#refresh-remote").click()
            page.wait_for_function("text => document.querySelector('#peanut-summary').textContent === text", arg=unknown)
            value[layer]["stale"] = False
            page.locator("#refresh-remote").click()
            page.wait_for_function("text => document.querySelector('#peanut-summary').textContent === text", arg=online)
        for state in ("offline", "logging_in", "retrying", "unknown"):
            value["device"]["state"] = state
            page.locator("#refresh-remote").click()
            page.wait_for_function("text => document.querySelector('#peanut-summary').textContent !== text", arg=online)
            assert page.locator("#peanut-management-link").count() == 0
            value["device"]["state"] = "online"
            page.locator("#refresh-remote").click()
            page.wait_for_function("text => document.querySelector('#peanut-summary').textContent === text", arg=online)
        value["daemon"]["state"] = "stopped"
        page.locator("#refresh-remote").click()
        page.wait_for_function("text => document.querySelector('#peanut-summary').textContent !== text", arg=online)
        assert ("已停止" if language == "zh" else "Stopped") in page.locator("#peanut-summary").inner_text()
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_remote_mapping_new_tab_can_background_portal(browser, live_portal, language):
    context = browser.new_context()
    page = context.new_page()
    value = mock_remote(page)
    value["binding"].update(state="bound", reason=None)
    value["capabilities"]["management"] = True
    pending = []
    page.route("**/api/remote/peanut/management-intents", lambda route: pending.append(route))
    context.route("**/api/remote/peanut/management-redirect/synthetic", lambda route: route.fulfill(
        content_type="text/html", body="<h1>Synthetic management</h1>"
    ))
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        page.keyboard.press("Escape")
        navigate_view(page, "remote")
        page.wait_for_function(
            "() => document.querySelector('#peanut-next-action').textContent === "
            "window.CloudhostI18n.t('peanut.management')"
        )
        with page.expect_popup() as opened:
            page.locator("#peanut-next-action").click()
        popup = opened.value
        page.wait_for_function("document.querySelector('#peanut-next-action').disabled")
        assert len(pending) == 1
        page.evaluate("""() => {
            Object.defineProperty(document, 'hidden', {configurable: true, value: true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        pending[0].fulfill(json={"redirect_path": "/api/remote/peanut/management-redirect/synthetic"})
        popup.wait_for_url(live_portal.base + "/api/remote/peanut/management-redirect/synthetic")
        assert popup.locator("h1").inner_text() == "Synthetic management"
        assert popup.evaluate("window.opener === null")
        assert page.url.endswith("#remote")
        assert len(pending) == 1
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_remote_mapping_popup_blocked_sends_no_write(browser, live_portal, language):
    context = browser.new_context()
    page = context.new_page()
    value = mock_remote(page)
    value["binding"].update(state="bound", reason=None)
    value["capabilities"]["management"] = True
    writes = []
    page.route("**/api/remote/peanut/management-intents", lambda route: (
        writes.append(route.request.method), route.fulfill(json={})
    ))
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        page.keyboard.press("Escape")
        navigate_view(page, "remote")
        page.wait_for_function(
            "() => document.querySelector('#peanut-next-action').textContent === "
            "window.CloudhostI18n.t('peanut.management')"
        )
        page.evaluate("window.open = () => null")
        page.locator("#peanut-next-action").click()
        page.wait_for_function(
            "() => document.querySelector('#peanut-feedback').textContent === "
            "window.CloudhostI18n.t('peanut.popupBlocked')"
        )
        assert not writes
        assert len(context.pages) == 1
        assert page.locator("#peanut-next-action").is_enabled()
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("outcome", [
    "denied", "network", "invalid_redirect", "leave", "stale", "closed",
])
def test_remote_management_direct_open_errors_and_cancelled_context(browser, live_portal, language, outcome):
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    value = mock_remote(page)
    value["binding"].update(state="bound", reason=None)
    value["capabilities"]["management"] = True
    pending, redirects, errors = [], [], []
    page.route("**/api/remote/peanut/management-intents", lambda route: pending.append(route))
    page.on("pageerror", lambda error: errors.append(str(error)))
    context.route("**/api/remote/peanut/management-redirect/**", lambda route: (
        redirects.append(route.request.url), route.fulfill(body="Unexpected navigation")
    ))
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        page.keyboard.press("Escape")
        navigate_view(page, "remote")
        page.wait_for_function(
            "() => document.querySelector('#peanut-next-action').textContent === "
            "window.CloudhostI18n.t('peanut.management')"
        )
        button = page.locator("#peanut-next-action")
        with page.expect_popup() as opened:
            button.click()
        popup = opened.value
        assert popup.evaluate("window.opener === null")
        page.bring_to_front()
        page.wait_for_function("document.querySelector('#peanut-next-action').disabled")
        assert len(pending) == 1
        assert pending[0].request.headers.get("x-csrf-token")
        assert pending[0].request.headers.get("idempotency-key")
        assert pending[0].request.post_data_json == {"expected_revision": 1}
        button.evaluate("node => node.click()")
        assert len(pending) == 1
        if outcome == "leave":
            navigate_view(page, "overview")
        elif outcome == "stale":
            value["binding"]["stale"] = True
            page.locator("#refresh-remote").click()
            page.wait_for_function(
                "() => document.querySelector('#peanut-next').textContent.includes('adapter_not_verified')"
            )
        elif outcome == "closed":
            popup.close()
        if outcome == "denied":
            pending[0].fulfill(status=403, json={"error": {"code": "E5004", "detail": "management_unverified"}})
            reason = "management_unverified"
        elif outcome == "network":
            pending[0].abort()
            reason = "outcome_unknown"
        else:
            pending[0].fulfill(json={"redirect_path": (
                "https://untrusted.example.net/" if outcome == "invalid_redirect"
                else "/api/remote/peanut/management-redirect/synthetic"
            )})
            reason = "invalid_management_redirect" if outcome == "invalid_redirect" else "management_context_changed"
        page.wait_for_function(
            "reason => document.querySelector('#peanut-feedback').textContent.includes(reason)", arg=reason,
        )
        page.wait_for_timeout(100)
        assert len(pending) == 1
        assert not redirects
        assert popup.is_closed()
        assert len(context.pages) == 1
        assert page.url.endswith("#overview" if outcome == "leave" else "#remote")
        assert page.locator("#peanut-management-link").count() == 0
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("size", VIEWPORTS, ids=lambda size: f"{size[0]}x{size[1]}")
@pytest.mark.parametrize("language", ["en", "zh"])
def test_remote_guided_qr_binding_management_and_address(browser, live_portal, size, language):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    value = mock_remote(page)
    value["intent"]["enabled"] = True
    value["binding"].update(state="unbound", stale=False)
    value["address"]["https_url"] = ""
    value["capabilities"].update(qr_binding=True, management=True)
    writes = []
    qr = {
        "binding_id": "synthetic-binding", "generation": 1, "state": "waiting",
        "expires_at": "2099-01-01T00:00:00Z",
        "image_path": "/api/remote/peanut/bindings/synthetic-binding/image?generation=1",
    }

    def bindings(route):
        if route.request.method == "POST":
            writes.append("qr")
        route.fulfill(json=qr)

    page.route("**/api/remote/peanut/bindings", bindings)
    page.route("**/api/remote/peanut/bindings/synthetic-binding", bindings)
    page.route("**/api/remote/peanut/bindings/synthetic-binding/image?generation=1", lambda route: route.fulfill(
        content_type="image/png", body=base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6D8sAAAAASUVORK5CYII="
        )
    ))
    page.route("**/api/remote/peanut/management-intents", lambda route: (
        writes.append("management"),
        route.fulfill(json={"redirect_path": "/api/remote/peanut/management-redirect/synthetic"})
    ))

    def address(route):
        assert route.request.method == "PUT"
        assert route.request.headers.get("x-csrf-token")
        assert route.request.headers.get("idempotency-key")
        value["address"]["https_url"] = route.request.post_data_json["https_url"]
        value["https"]["state"] = "unverified"
        writes.append("address")
        route.fulfill(json={"revision": 2})

    page.route("**/api/remote/peanut/address", address)
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        page.keyboard.press("Escape")
        navigate_view(page, "remote")
        page.wait_for_function("document.querySelector('#peanut-step').textContent.includes('2 / 3')")
        assert not writes
        assert page.locator("#peanut-settings").count() == 0
        assert page.locator("#peanut-address-actions").is_hidden()
        page.locator("#peanut-next-action").click()
        page.wait_for_function("document.querySelector('#peanut-qr-image').naturalWidth > 0")
        assert page.locator("#peanut-qr-image").is_visible()
        assert page.locator("#peanut-next-action").is_hidden()
        assert page.locator("#peanut-qr-state").evaluate("node => node === document.activeElement")
        assert page.locator("#peanut-qr-refresh").is_visible()
        assert page.locator("#peanut-qr-cancel").is_visible()
        # A successful image request or an online SDK must not unlock management.
        assert page.locator("#peanut-management-link").count() == 0
        value["binding"].update(state="unknown", reason="binding_unverified")
        page.locator("#refresh-remote").click()
        assert page.locator("#peanut-qr-image").is_visible()
        value["binding"].update(state="bound", reason=None, stale=True)
        page.locator("#refresh-remote").click()
        page.wait_for_timeout(100)
        assert page.locator("#peanut-qr-image").is_visible()
        value["binding"]["stale"] = False
        qr["state"] = "bound"
        page.locator("#refresh-remote").click()
        page.wait_for_function("document.querySelector('#peanut-step').textContent.includes('3 / 3')")
        assert page.locator("#peanut-qr-image").is_hidden()
        assert page.locator("#peanut-qr-image").get_attribute("src") is None
        assert page.locator("#peanut-binding-step").is_hidden()
        assert page.locator("#peanut-console, #peanut-mapping-step").count() == 0
        assert page.locator("#peanut-next-action").get_attribute("aria-label") == (
            "Configure mapping (opens in a new tab)" if language == "en"
            else "配置映射（在新标签页打开）"
        )
        context.route(
            "**/api/remote/peanut/management-redirect/synthetic",
            lambda route: route.fulfill(content_type="text/html", body="<h1>Synthetic management</h1>"),
        )
        portal_url = page.url
        with page.expect_popup() as opened:
            if language == "en":
                page.locator("#peanut-next-action").focus()
                page.keyboard.press("Enter")
            else:
                page.locator("#peanut-next-action").click()
        popup = opened.value
        popup.wait_for_url(live_portal.base + "/api/remote/peanut/management-redirect/synthetic")
        assert popup.locator("h1").inner_text() == "Synthetic management"
        assert popup.evaluate("window.opener === null")
        assert len(context.pages) == 2
        assert page.url == portal_url
        popup.close()
        page.bring_to_front()
        page.locator("#peanut-next-action").wait_for(state="visible")
        assert page.locator("#peanut-management-link").count() == 0
        page.locator("#peanut-address-entry > summary").click()
        page.locator("#peanut-address").fill("https://registered.example.invalid")
        page.locator('#peanut-address-form button[type="submit"]').click()
        page.wait_for_function(
            "document.querySelector('#peanut-public-address').textContent === 'https://registered.example.invalid'"
        )
        assert page.locator("#peanut-address-source").is_hidden()
        page.locator(".remote-diagnostics > summary").click()
        assert page.locator("#peanut-address-source").is_visible()
        source = page.locator("#peanut-address-source").inner_text()
        assert ("非官方自动同步" if language == "zh" else "not synced") in source
        assert page.locator("#peanut-open").get_attribute("href") == "https://registered.example.invalid"
        assert writes == ["qr", "management", "address"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if os.getenv("CLOUDHOST_EVIDENCE_DIR"):
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) /
                                     f"remote-guided-{language}-{size[0]}x{size[1]}.png"))
    finally:
        context.close()


@pytest.mark.parametrize("failure", [None, "start", "network", "qr"])
def test_remote_enable_waits_for_final_jobs_and_never_retries(browser, live_portal, failure):
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    value = mock_remote(page)
    value["daemon"]["state"] = "stopped"
    value["intent"]["enabled"] = False
    value["capabilities"]["qr_binding"] = failure == "qr"
    value["address"]["https_url"] = ""
    value["https"]["state"] = "unverified"
    operations, polls = [], []

    def actions(route):
        data = route.request.post_data_json
        assert data["expected_revision"] == value["revision"]
        assert route.request.headers.get("idempotency-key")
        assert route.request.headers.get("x-csrf-token")
        operation = data["operation"]
        operations.append(operation)
        if failure == "network":
            route.abort()
            return
        route.fulfill(json={"job_id": operation, "operation": operation, "status": "queued"})

    def jobs(route):
        operation = route.request.url.rsplit("/", 1)[1]
        polls.append(operation)
        failed = failure == "start"
        if not failed:
            value["daemon"]["state"] = "running"
            value["revision"] += 1
        route.fulfill(json={
            "job_id": operation, "operation": operation, "status": "failed" if failed else "succeeded",
            "phase": "completed", "reason": "synthetic_failure" if failed else "",
            "next_action": "check_state",
        })

    page.route("**/api/remote/peanut/actions", actions)
    page.route("**/api/remote/jobs/*", jobs)
    qr_requests = []

    def automatic_qr(route):
        assert operations == ["start"]
        assert value["intent"]["enabled"] is False
        assert value["daemon"]["state"] == "running"
        qr_requests.append(True)
        route.fulfill(json={
            "binding_id": "synthetic-expired", "generation": 1, "state": "expired",
            "expires_at": "2000-01-01T00:00:00Z", "image_path": "/never-loaded",
        })

    page.route("**/api/remote/peanut/bindings", automatic_qr)
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "remote")
        page.wait_for_function("document.querySelector('#peanut-next-action').textContent === 'Enable remote access'")
        assert operations == []
        assert page.locator("#peanut-binding-step").is_hidden()
        if failure is None and os.getenv("CLOUDHOST_EVIDENCE_DIR"):
            page.screenshot(path=str(Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) / "remote-enable-390x844.png"))
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#peanut-next-action").click()
        assert operations == []
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#peanut-next-action").click()
        if failure == "network":
            page.wait_for_function("document.querySelector('#peanut-feedback').textContent.includes('outcome unknown')")
        elif failure == "start":
            page.wait_for_function("document.querySelector('#peanut-feedback').textContent.includes('synthetic_failure')")
        else:
            page.wait_for_function("document.querySelector('#peanut-step').textContent.includes('2 / 3')")
        if failure == "qr":
            page.wait_for_function("document.querySelector('#peanut-qr-state').textContent.includes('Expired')")
        assert qr_requests == ([True] if failure == "qr" else [])
        assert operations == ["start"]
        assert polls == ([] if failure == "network" else operations)
        assert page.locator("#peanut-qr-image").is_hidden()
        if failure == "start":
            assert value["intent"]["enabled"] is False
            assert value["daemon"]["state"] == "stopped"
    finally:
        context.close()


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("theme", ["classic", "bento", "swiss", "cyber"])
@pytest.mark.parametrize("account", [False, True])
def test_remote_reset_confirmation_and_persistent_result(browser, live_portal, language, theme, account):
    context = browser.new_context(viewport={"width": 375, "height": 812}, reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(f"localStorage.setItem('cloudhost.theme', '{theme}')")
    value = mock_remote(page, address={
        "https_url": "https://example.invalid", "ssh": {"host": "ssh.example.net", "port": 22},
    })
    value["capabilities"]["unbind"] = account
    value["binding"]["state"] = "bound"
    writes, qr_requests = [], []
    operation = "reset_account_config" if account else "reset_local_config"
    reason = "account_unbound_config_cleared" if account else "local_config_cleared_binding_retained"

    def actions(route):
        body = route.request.post_data_json
        writes.append(body)
        assert body["operation"] == operation
        value["last_job"] = {"job_id": "reset-task", "operation": operation, "status": "queued"}
        route.fulfill(status=202, json=value["last_job"])

    def jobs(route):
        value.update(address={"https_url": "", "ssh": None}, reset_pending=False)
        value["intent"]["enabled"] = False
        value["daemon"]["state"] = "stopped"
        value["last_job"].update(status="succeeded", phase="completed", reason=reason)
        route.fulfill(json=value["last_job"])

    page.route("**/api/remote/peanut/actions", actions)
    page.route("**/api/remote/jobs/*", jobs)
    page.route("**/api/remote/peanut/bindings", lambda route: (
        qr_requests.append(True), route.abort()
    ))
    try:
        sign_in(page, live_portal.base)
        change_language(page, language)
        navigate_view(page, "remote")
        page.locator("#peanut-reset-options > summary").click()
        opener = page.locator("#peanut-reset-open")
        opener.click()
        dialog = page.locator("#peanut-reset-dialog")
        assert dialog.is_visible()
        assert page.locator("#peanut-reset-account").is_disabled() is not account
        for _ in range(10):
            page.keyboard.press("Tab")
            assert page.evaluate("document.querySelector('#peanut-reset-dialog').contains(document.activeElement)")
        page.keyboard.press("Escape")
        assert dialog.is_hidden()
        assert opener.evaluate("node => node === document.activeElement")
        assert not writes
        opener.click()
        if account:
            page.locator("#peanut-reset-account").check()
        page.locator("#peanut-reset-submit").click()
        assert not writes
        page.locator("#peanut-reset-loss").check()
        if account:
            page.locator("#peanut-reset-submit").click()
            assert not writes
            page.locator("#peanut-reset-unbind").check()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#peanut-reset-submit").click()
        page.wait_for_function(
            "() => /Configuration cleared|Account unbinding confirmed|配置已清除|账号解绑已确认/.test(document.querySelector('#peanut-feedback').textContent)"
        )
        assert len(writes) == 1
        assert writes[0]["confirmed_connection_loss"] is True
        assert writes[0]["confirmed_account_unbind"] is account
        assert page.locator("#peanut-autostart").is_checked() is False
        assert "1 / 3" in page.locator("#peanut-step").inner_text()
        assert page.locator("#peanut-public-address").inner_text() == ""
        assert page.locator("#ssh-public-command").is_hidden()
        assert not qr_requests
        page.reload()
        page.wait_for_function(
            "() => /Configuration cleared|Account unbinding confirmed|配置已清除|账号解绑已确认/.test(document.querySelector('#peanut-feedback').textContent)"
        )
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("outcome", ["network", "timeout"])
def test_remote_autostart_independent_and_unknown_retry_keeps_key(browser, live_portal, running, outcome):
    context = browser.new_context()
    page = context.new_page()
    value = mock_remote(page)
    value["daemon"]["state"] = "running" if running else "stopped"
    value["binding"]["state"] = "bound"
    value["intent"]["enabled"] = running
    writes, keys = [], []

    def actions(route):
        body = route.request.post_data_json
        writes.append(body)
        keys.append(route.request.headers["idempotency-key"])
        if len(writes) == 1:
            if outcome == "network":
                route.abort()
            else:
                route.fulfill(status=504, json={"error": {"code": "E5003", "detail": "outcome_unknown"}})
            return
        value["intent"]["enabled"] = body["enabled"]
        route.fulfill(status=202, json={
            "job_id": "autostart-task", "operation": "set_autostart", "status": "queued",
        })

    page.route("**/api/remote/peanut/actions", actions)
    page.route("**/api/remote/jobs/*", lambda route: route.fulfill(json={
        "job_id": "autostart-task", "operation": "set_autostart", "status": "succeeded",
        "phase": "completed",
    }))
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "remote")
        checkbox = page.locator("#peanut-autostart")
        checkbox.wait_for()
        page.wait_for_function("!document.querySelector('#peanut-autostart').disabled")
        if not running:
            page.once("dialog", lambda dialog: dialog.dismiss())
            checkbox.click()
            assert not writes
            assert not checkbox.is_checked()
            page.once("dialog", lambda dialog: dialog.accept())
        checkbox.click()
        page.wait_for_function("document.querySelector('#peanut-feedback').textContent.includes('outcome unknown')")
        assert checkbox.is_disabled()
        assert checkbox.is_checked() is running
        page.locator("#peanut-reconcile").click()
        page.wait_for_function("document.querySelector('#peanut-feedback').textContent.includes('setting saved')")
        assert writes[0] == writes[1]
        assert keys[0] == keys[1]
        assert writes[0]["operation"] == "set_autostart"
        assert writes[0]["enabled"] is not running
        assert checkbox.is_checked() is not running
        assert value["daemon"]["state"] == ("running" if running else "stopped")
        assert ("3 / 3" if running else "1 / 3") in page.locator("#peanut-step").inner_text()
        assert page.locator("#peanut-public-address").inner_text() == "https://example.invalid"
    finally:
        context.close()


def test_remote_reset_disconnection_and_recovery_from_new_page(browser, live_portal):
    context = browser.new_context()
    page = context.new_page()
    value = mock_remote(page, reset_pending=True, last_job={
        "job_id": "lost-reset", "operation": "reset_local_config", "status": "running",
    })
    page.route("**/api/remote/jobs/*", lambda route: route.abort())
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "remote")
        page.wait_for_function("document.querySelector('#peanut-feedback').textContent.includes('Connection interrupted')")
        assert page.locator("#peanut-autostart").is_disabled()
        assert page.locator("#peanut-next-action").is_disabled()
        value["last_job"].update(status="recovery_required", phase="stopping", reason="sdk_not_stopped")
        page.reload()
        page.wait_for_function("document.querySelector('#peanut-feedback').textContent.includes('sdk_not_stopped')")
        assert page.locator("#peanut-recover-reset").is_visible()
        assert page.locator("#peanut-recover-reset").is_enabled()
        assert page.locator("#peanut-next-action").is_disabled()
    finally:
        context.close()


@pytest.mark.parametrize("outcome", ["expired", "cancelled", "network", "gate_revoked"])
def test_remote_qr_materials_clear_without_automatic_reissue(browser, live_portal, outcome):
    context = browser.new_context(viewport={"width": 375, "height": 812})
    page = context.new_page()
    value = mock_remote(page)
    value["binding"].update(state="unbound", stale=False)
    value["capabilities"]["qr_binding"] = True
    issued, cancelled = [], []
    qr = {
        "binding_id": "synthetic-lifecycle", "generation": 1, "state": "waiting",
        "expires_at": "2099-01-01T00:00:00Z",
        "image_path": "/api/remote/peanut/bindings/synthetic-lifecycle/image?generation=1",
    }

    def protocol(route):
        path = urlsplit(route.request.url).path
        if path.endswith("/image"):
            route.fulfill(content_type="image/svg+xml",
                          body='<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')
        elif path.endswith("/cancel"):
            cancelled.append(True)
            route.fulfill(json={})
        elif route.request.method == "POST":
            issued.append(True)
            route.fulfill(json=qr)
        elif outcome == "network":
            route.abort()
        else:
            qr["state"] = "expired"
            route.fulfill(json=qr)

    page.route("**/api/remote/peanut/bindings**", protocol)
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "remote")
        page.wait_for_function("document.querySelector('#peanut-next-action').textContent === 'Get official QR'")
        page.locator("#peanut-next-action").click()
        page.wait_for_function("document.querySelector('#peanut-qr-image').naturalWidth > 0")
        if outcome == "gate_revoked":
            value["capabilities"]["qr_binding"] = False
            page.locator("#refresh-remote").click()
        elif outcome == "cancelled":
            page.locator("#peanut-qr-cancel").click()
        page.wait_for_function("!document.querySelector('#peanut-qr-image').getAttribute('src')")
        assert page.locator("#peanut-qr-image").is_hidden()
        assert page.locator("#peanut-management-link").count() == 0
        change_language(page, "zh")
        assert page.locator("#peanut-qr-image").get_attribute("src") is None
        if outcome == "network":
            assert "network" in page.locator("#peanut-feedback").inner_text()
        if outcome == "gate_revoked":
            assert page.locator("#peanut-next-action").is_disabled()
        assert issued == [True]
        assert cancelled == ([True] if outcome == "cancelled" else [])
    finally:
        context.close()


def test_terminal_document_is_not_shell_and_is_unloaded(browser, live_portal, monkeypatch):
    monkeypatch.setattr(routes_pages, "_ttyd_available", lambda cfg: True)
    context = browser.new_context()
    page = context.new_page()
    page.route("**/term/", lambda route: route.fulfill(
        content_type="text/html", body="<h1>Mock terminal document, no shell</h1>"
    ))
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "term")
        page.frame_locator("#term-frame").get_by_role("heading", name="Mock terminal document, no shell").wait_for()
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        navigate_view(page, "ssh")
        assert page.locator("#term-frame").get_attribute("src") == "about:blank"
        page.go_back()
        page.wait_for_function("document.querySelector('.nav-tab[data-view=\"term\"]').getAttribute('aria-current') === 'page'")
        page.frame_locator("#term-frame").get_by_role("heading").wait_for()
        page.go_forward()
        page.wait_for_function("document.querySelector('.nav-tab[data-view=\"ssh\"]').getAttribute('aria-current') === 'page'")
        assert page.locator("#term-frame").get_attribute("src") == "about:blank"
        navigate_view(page, "term")
        page.frame_locator("#term-frame").get_by_role("heading").wait_for()
        # Expire this synthetic session; polling must return to sign-in and unload the document.
        context.clear_cookies()
        page.wait_for_selector("#login-form")
        assert "/login?redirect=" in page.url
        assert page.locator("#term-frame").count() == 0
    finally:
        context.close()


@pytest.mark.parametrize("size", VIEWPORTS, ids=lambda size: f"{size[0]}x{size[1]}")
def test_terminal_workspace_size_and_resize_keep_one_iframe(browser, live_portal, monkeypatch, size):
    monkeypatch.setattr(routes_pages, "_ttyd_available", lambda cfg: True)
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    requests = []

    def terminal(route):
        requests.append(route.request.url)
        route.fulfill(content_type="text/html", body="<p>Mock terminal view; no shell commands.</p>")

    page.route("**/term/", terminal)
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "term")
        page.frame_locator("#term-frame").get_by_text("Mock terminal view; no shell commands.").wait_for()
        frame = page.locator("#term-frame")
        before_src = frame.get_attribute("src")
        before_count = len(requests)
        box = frame.bounding_box()
        assert box["y"] + box["height"] <= size[1] + 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        if size[0] >= 1280:
            assert box["width"] <= 1280
            assert (size[0] - box["width"]) / 2 >= 12
        else:
            assert (size[0] - box["width"]) / 2 >= 12

        resized = {"width": 375, "height": 812} if size[0] >= 1280 else {"width": 1280, "height": 800}
        page.set_viewport_size(resized)
        page.wait_for_function(
            "document.querySelector('#term-frame').getBoundingClientRect().width !== 0"
        )
        page.wait_for_timeout(100)
        resized_box = frame.bounding_box()
        assert resized_box["y"] + resized_box["height"] <= resized["height"] + 1
        assert frame.get_attribute("src") == before_src
        assert len(requests) == before_count
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
    finally:
        context.close()


@pytest.mark.parametrize("width,height", [(1440, 900), (1280, 800), (375, 812), (390, 844)])
def test_footer_is_bottom_aligned_only_when_content_is_short(browser, live_portal, width, height):
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    try:
        sign_in(page, live_portal.base)
        page.add_style_tag(content="""
          #view-overview > *, #overview-services { display: none !important; }
        """)
        footer = page.locator(".pagefoot")
        page.wait_for_function(
            "Math.abs(document.querySelector('.pagefoot').getBoundingClientRect().bottom - innerHeight) < 1"
        )
        assert page.evaluate("getComputedStyle(document.querySelector('.pagefoot')).position") != "fixed"
        page.locator("style").last.evaluate("node => node.remove()")
        page.locator("#view-overview").evaluate(
            "node => { for (let i=0;i<24;i++) { const p=document.createElement('p'); p.textContent='Synthetic layout test content'; node.append(p); } }"
        )
        page.wait_for_function("document.documentElement.scrollHeight > innerHeight")
        content_bottom = page.evaluate(
            "document.querySelector('#view-overview').lastElementChild.getBoundingClientRect().bottom + scrollY"
        )
        footer_top = footer.evaluate("node => node.getBoundingClientRect().top + scrollY")
        assert footer_top >= content_bottom
        assert page.evaluate("getComputedStyle(document.querySelector('.pagefoot')).position") != "fixed"
    finally:
        context.close()


def test_preference_warning_does_not_clip_workspace(browser, live_portal):
    context = browser.new_context(viewport={"width": 375, "height": 812})
    context.add_init_script(
        "Object.defineProperty(window, 'localStorage', {get() {throw new DOMException('Blocked', 'SecurityError');}})"
    )
    page = context.new_page()
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "site")
        page.frame_locator("#site-frame").locator("#markdown-input").wait_for()
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        assert page.locator("body > [data-i18n-status]").is_visible()
        page.wait_for_timeout(100)
        geometry = page.evaluate("""() => {
          const frame = document.querySelector('#site-frame').getBoundingClientRect();
          const workspace = document.querySelector('#view-embed').getBoundingClientRect();
          const header = document.querySelector('.topbar').getBoundingClientRect();
          const notice = document.querySelector('[data-i18n-status]').getBoundingClientRect();
          return {frameBottom: frame.bottom, viewport: innerHeight, workspaceBottom: workspace.bottom,
            headerBottom: header.bottom, noticeBottom: notice.bottom,
            headerVariable: getComputedStyle(document.documentElement).getPropertyValue('--portal-header-height')};
        }""")
        assert geometry["frameBottom"] <= geometry["viewport"] + .5, geometry
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight")
        change_language(page, "zh")
        assert page.url.endswith("#site")
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
        assert page.evaluate(
            "document.querySelector('#site-frame').getBoundingClientRect().bottom <= innerHeight + .5"
        )
    finally:
        context.close()


def test_login_errors_limits_and_workspace_feedback(browser, live_portal, monkeypatch):
    monkeypatch.setattr(routes_pages, "_ttyd_available", lambda cfg: True)
    context = browser.new_context()
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.accept())
    page.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: text => {window.copiedText = text; return Promise.resolve();}}})")
    try:
        page.goto(live_portal.base + "/login")
        page.locator("#login-submit").click()
        assert "Enter a username and password" in page.locator("#login-error").inner_text()
        page.route("**/auth/login", lambda route: route.abort())
        page.locator("#password").fill("network-fixture")
        page.locator("#login-submit").click()
        page.wait_for_function("document.querySelector('#login-error').textContent.includes('Network error')")
        assert page.locator("#login-submit").is_enabled()
        page.unroute("**/auth/login")
        page.locator("#password").fill("wrong")
        page.locator("#login-submit").click()
        page.wait_for_function("!document.querySelector('#login-error').hidden")
        assert "Incorrect" in page.locator("#login-error").inner_text()
        for _ in range(5):
            response = context.request.post(live_portal.base + "/auth/login", form={"username": TEST_USER, "password": "wrong"})
        assert response.status == 429
        page.locator("#login-submit").click()
        page.wait_for_function("document.querySelector('#login-error').textContent.includes('Too many')")
        live_portal.app.state.limiter.reset_all()
        sign_in(page, live_portal.base)
        page.goto(live_portal.base + "/#files")
        page.wait_for_function("document.querySelector('#embed-hint').textContent.includes('gateway')")
        assert page.locator("#view-embed").is_visible()
        page.route("**/files/", lambda route: route.fulfill(status=502, body="Unavailable"))
        page.locator("#embed-refresh").click()
        page.wait_for_function("document.querySelector('#embed-hint').textContent.includes('502')")
        page.unroute("**/files/")
        page.route("**/files/", lambda route: route.fulfill(status=404, body="Backend path not found"))
        page.locator("#embed-refresh").click()
        page.wait_for_function("document.querySelector('#embed-hint').textContent.includes('404')")
        assert "gateway did not route" not in page.locator("#embed-hint").inner_text()
        page.unroute("**/files/")
        page.route("**/files/", lambda route: route.abort())
        page.locator("#embed-refresh").click()
        page.wait_for_function("document.querySelector('#embed-hint').textContent.includes('timed out')")
        page.unroute("**/files/")
        page.route("**/files/", lambda route: None)
        page.locator("#embed-refresh").click()
        page.wait_for_function("document.querySelector('#embed-hint').textContent.includes('Checking')")
        page.wait_for_function("document.querySelector('#embed-hint').textContent.includes('timed out')", timeout=20000)
        page.unroute("**/files/")
        page.route("**/files/", lambda route: route.fulfill(status=302, headers={"Location": "/login?redirect=/files/"}))
        page.locator("#embed-refresh").click()
        page.wait_for_function("document.querySelector('#embed-hint').textContent.includes('session expired')")
        page.unroute("**/files/")
        page.route("**/api/status", lambda route: route.fulfill(json={
            "cpu": {}, "mem": {}, "temp": {"cpu_c": None}, "disk": {},
            "net": {"rx_kbps": None, "tx_kbps": None}, "uptime_sec": None,
        }))
        page.goto(live_portal.base + "/")
        page.wait_for_function("document.querySelector('#uptime').textContent === '—'")
        assert page.locator("#net-rx").inner_text() == "—"
        page.unroute("**/api/status")
        page.locator("#services-title").click()
        page.locator("button[data-service]").first.click()
        assert live_portal.restarted
        page.locator('.nav-tab[data-view="ssh"]').click()
        assert page.locator("#view-remote").is_visible()
        assert page.locator("#remote-ssh-title").is_visible()
        assert page.locator('.nav-tab[data-view="ssh"]').get_attribute("aria-current") == "page"
        page.locator("#ssh-copy").click()
        page.wait_for_function("document.querySelector('#ssh-copy').textContent === 'Copied'")
        assert page.evaluate("window.copiedText") == page.locator("#ssh-command").inner_text()
        page.locator('.nav-tab[data-view="overview"]').click()
        page.route("**/api/services", lambda route: route.fulfill(status=503, body="Unavailable"))
        page.locator("#refresh-services").click()
        page.wait_for_function("document.querySelector('#service-hint').textContent.includes('could not be refreshed')")
        page.unroute("**/api/services")
        page.route("**/api/services", lambda route: route.fulfill(json={"services": [
            {"name": "caddy", "state": "starting", "enabled": True},
            {"name": "cloudhost-files", "state": "failed", "enabled": True},
        ]}))
        page.locator("#refresh-services").click()
        page.wait_for_function("document.querySelector('#service-rows').textContent.includes('Starting')")
        assert page.locator("#service-rows").get_by_text("Failed", exact=True).is_visible()
        page.unroute("**/api/services")
        page.route("**/api/status", lambda route: route.fulfill(status=503, body="Unavailable"))
        page.wait_for_function("document.querySelector('#status-hint').textContent === 'Connection lost'")
    finally:
        context.close()


@pytest.fixture
def official_stack(live_portal, tmp_path, monkeypatch):
    binaries = {name: os.getenv("CLOUDHOST_" + name.upper()) for name in ("caddy", "filebrowser", "ttyd")}
    if not all(binaries.values()):
        pytest.skip("Set CLOUDHOST_CADDY, CLOUDHOST_FILEBROWSER and CLOUDHOST_TTYD for actual vendor integration")
    binaries = {key: str(Path(value).resolve()) for key, value in binaries.items()}
    config = live_portal.config
    monkeypatch.setattr(routes_pages, "_TTYD_BIN", Path(binaries["ttyd"]))
    fb_port, term_port, gateway_port = free_port(), free_port(), free_port()
    monkeypatch.setattr(live_portal.app.state, "config", replace(config, ttyd_port=term_port))
    fb_config = tmp_path / "filebrowser.json"
    db = tmp_path / "filebrowser.db"
    fb_config.write_text(json.dumps({
        "port": fb_port, "address": "127.0.0.1", "baseURL": "/files",
        "root": str(config.files_root), "database": str(db), "noauth": True,
        "hideLoginButton": True, "defaults": {"locale": "en"},
    }), encoding="utf-8")
    command = [binaries["filebrowser"], "-c", str(fb_config), "-d", str(db)]
    subprocess.run(command + ["config", "init", "--auth.method", "noauth"], check=True, capture_output=True)
    subprocess.run(command + ["users", "add", "fixture", "unused-isolated-fixture-password", "--locale", "en"], check=True, capture_output=True)
    filebrowser_branding.apply_branding(Path(binaries["filebrowser"]), fb_config, db, PROJECT / "deploy" / "filebrowser")
    (config.files_root / "browser-example.txt").write_text("A real isolated File Browser file\n", encoding="utf-8")
    (config.files_root / "empty").mkdir()
    shutil.copyfile(PROJECT / "deploy" / "site-index.html", config.site_dir / "index.html")
    settings = dict(render_config.DEFAULT_CONFIG, **{
        "listen_port": gateway_port, "portal_port": int(live_portal.base.rsplit(":", 1)[1]),
        "filebrowser_port": fb_port, "ttyd_port": term_port,
        "cloud_root": str(config.cloud_root), "site_dir": str(config.site_dir),
        "system_home": str(config.files_root),
    })
    values = render_config._placeholders(settings, SimpleNamespace(run_user="isolated-test"))
    caddy_config = tmp_path / "Caddyfile"
    render_config.render_template(PROJECT / "deploy" / "Caddyfile.tmpl", caddy_config, values)
    text = caddy_config.read_text(encoding="utf-8").replace(
        f"http://:{gateway_port}", f"http://127.0.0.1:{gateway_port}"
    ).replace("/var/log/cloudhost/caddy-access.log", str(tmp_path / "caddy-access.log"))
    caddy_config.write_text(text, encoding="utf-8")
    caddy_env = dict(os.environ, HOME=str(tmp_path), XDG_DATA_HOME=str(tmp_path / "xdg-data"), XDG_CONFIG_HOME=str(tmp_path / "xdg-config"))
    subprocess.run([binaries["caddy"], "validate", "--config", str(caddy_config), "--adapter", "caddyfile"], env=caddy_env, check=True, capture_output=True)
    term_template = (PROJECT / "deploy" / "cloudhost-ttyd.service.tmpl").read_text(encoding="utf-8")
    term_command = term_template.split("ExecStart=", 1)[1].split("\nRestart=", 1)[0].replace("\\\n", " ")
    term_command = term_command.replace("/opt/cloudhost/bin/ttyd", binaries["ttyd"]).replace("{{TTYD_PORT}}", str(term_port))
    # No login shell: the test must not read or execute the host user's shell configuration.
    term_command = term_command.replace("/bin/bash -l", "/bin/bash --noprofile --norc")
    processes, logs = [], []
    terminal_project = tmp_path / "terminal-project"
    terminal_project.mkdir()
    (terminal_project / "README.md").write_text("# Isolated terminal project\n", encoding="utf-8")
    (terminal_project / "src").mkdir()
    (terminal_project / "hello.py").write_text('def greet():\n    return "Readable terminal editor"\n', encoding="utf-8")
    (terminal_project / "edit-note.txt").write_text("Original isolated note\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(terminal_project), "init", "-q", "--initial-branch=main"],
                   env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null"),
                   check=True, capture_output=True)
    try:
        for name, args, env in [
            ("filebrowser", command, os.environ),
            ("ttyd", shlex.split(term_command), dict(os.environ, HOME=str(tmp_path), HISTFILE="/dev/null",
                                                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")),
            ("caddy", [binaries["caddy"], "run", "--config", str(caddy_config), "--adapter", "caddyfile"], caddy_env),
        ]:
            log = (tmp_path / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            processes.append(subprocess.Popen(args, cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT))
        base = f"http://127.0.0.1:{gateway_port}"
        wait_http(base + "/login", processes)
        wait_http(f"http://127.0.0.1:{fb_port}/files/", processes)
        yield SimpleNamespace(base=base, processes=processes, directory=tmp_path)
    finally:
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log in logs:
            log.close()


@pytest.mark.parametrize("size", VIEWPORTS, ids=lambda size: f"{size[0]}x{size[1]}")
def test_official_filebrowser_and_terminal(browser, official_stack, size):
    base = official_stack.base
    assert httpx.get(base + "/files/", follow_redirects=False).status_code == 302
    assert httpx.get(base + "/term/", follow_redirects=False).status_code == 302
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, accept_downloads=True)
    page = context.new_page()
    diagnostics = diagnostics_for(page)
    terminal_output = []
    def socket_opened(ws):
        ws.on("framereceived", lambda payload: terminal_output.append(
            payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
        ))
    page.on("websocket", socket_opened)
    evidence = Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) if os.getenv("CLOUDHOST_EVIDENCE_DIR") else None
    try:
        sign_in(page, base)
        page.goto(base + "/files/")
        page.get_by_text("browser-example.txt", exact=True).wait_for()
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(7, 20, 38)"
        assert page.locator('link[href*="custom.css"]').count() == 1
        assert context.request.get(base + "/files/static/custom.css").headers["cache-control"] == "no-cache"
        assert page.locator("html").get_attribute("lang") == "en"
        assert not page.locator('nav [aria-label="Settings"]').is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if evidence:
            page.screenshot(path=str(evidence / f"filebrowser-official-{size[0]}x{size[1]}.png"), full_page=True)
        page.get_by_text("browser-example.txt", exact=True).click()
        assert page.locator('#listing [aria-selected="true"]').count() == 1
        if size[0] >= 1366:
            page.get_by_role("button", name="Rename", exact=True).click()
            if evidence:
                page.screenshot(path=str(evidence / f"filebrowser-rename-{size[0]}x{size[1]}.png"), full_page=True)
            page.locator("#focus-prompt").fill("renamed-example.txt")
            page.locator(".card-action").get_by_role("button", name="Rename", exact=True).click()
            page.get_by_text("renamed-example.txt", exact=True).wait_for()
            assert (official_stack.directory / "cloud" / "renamed-example.txt").is_file()
            page.get_by_text("renamed-example.txt", exact=True).dblclick()
            page.get_by_text("A real isolated File Browser file", exact=False).last.wait_for()
            assert page.locator("#editor").evaluate("node => getComputedStyle(node).backgroundColor") == "rgb(7, 20, 38)"
            if evidence:
                page.screenshot(path=str(evidence / f"filebrowser-preview-{size[0]}x{size[1]}.png"), full_page=True)
            page.goto(base + "/files/")
            page.get_by_role("button", name="Upload", exact=True).click()
            with page.expect_file_chooser() as chooser:
                page.locator("#focus-prompt").click()
            chooser.value.set_files({"name": "uploaded-example.txt", "mimeType": "text/plain", "buffer": b"real isolated upload\n"})
            page.get_by_text("uploaded-example.txt", exact=True).wait_for()
            assert (official_stack.directory / "cloud" / "uploaded-example.txt").read_bytes() == b"real isolated upload\n"
            page.get_by_text("uploaded-example.txt", exact=True).click()
            with page.expect_download() as download:
                page.get_by_role("button", name="Download", exact=True).click()
            assert Path(download.value.path()).read_bytes() == b"real isolated upload\n"
            page.get_by_role("button", name="Delete", exact=True).click()
            page.locator(".card-action").get_by_role("button", name="Delete", exact=True).click()
            page.get_by_text("uploaded-example.txt", exact=True).wait_for(state="detached")
            assert not (official_stack.directory / "cloud" / "uploaded-example.txt").exists()
            page.get_by_text("empty", exact=True).dblclick()
            page.locator(".message").wait_for()
            if evidence:
                page.screenshot(path=str(evidence / f"filebrowser-empty-{size[0]}x{size[1]}.png"), full_page=True)
        assert context.request.get(base + "/files/api/resources/.ssh").status == 403
        # In-page and direct paths both use the actual official client.
        page.goto(base + "/#files")
        page.frame_locator("#embed-frame").get_by_text(
            "renamed-example.txt" if size[0] >= 1366 else "browser-example.txt", exact=True
        ).wait_for()
        page.goto(base + "/term/")
        page.locator(".xterm-helper-textarea").wait_for()
        terminal = page.locator(".xterm-helper-textarea")
        terminal.press_sequentially("printf 'REAL_%s_OK\\n' TERMINAL\n", delay=20)
        for _ in range(100):
            if "REAL_TERMINAL_OK" in "".join(terminal_output):
                break
            page.wait_for_timeout(50)
        assert "REAL_TERMINAL_OK" in "".join(terminal_output)
        assert page.evaluate("getComputedStyle(document.querySelector('.xterm-viewport')).backgroundColor") == "rgb(7, 20, 38)"
        page.wait_for_timeout(1200)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if evidence:
            page.screenshot(path=str(evidence / f"ttyd-connected-{size[0]}x{size[1]}.png"))
        terminal.press_sequentially(
            "cd terminal-project; clear; LS_COLORS='di=01;34:*.py=01;32' ls --color=always -la; "
            "git -c color.status=always --no-pager status --short --branch; "
            "printf 'TOOLS_%s_OK\\n' REAL\n", delay=5
        )
        for _ in range(100):
            if "TOOLS_REAL_OK" in "".join(terminal_output):
                break
            page.wait_for_timeout(50)
        assert "TOOLS_REAL_OK" in "".join(terminal_output)
        plain_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", "".join(terminal_output))
        assert "No commits yet on main" in plain_output
        assert "\x1b[31m??" in "".join(terminal_output)
        assert "\x1b[01;34msrc" in "".join(terminal_output)
        assert "README.md" in "".join(terminal_output)
        if evidence:
            page.screenshot(path=str(evidence / f"ttyd-ls-git-{size[0]}x{size[1]}.png"))
        terminal.press_sequentially(
            "vim -Nu NONE -U NONE -i NONE -n -c 'syntax on' -c 'set background=dark number' hello.py\n", delay=10
        )
        page.wait_for_timeout(400)
        if evidence:
            page.screenshot(path=str(evidence / f"ttyd-vim-syntax-{size[0]}x{size[1]}.png"))
        terminal.press("Escape")
        terminal.press_sequentially(":q!\n", delay=20)
        page.wait_for_timeout(150)
        terminal.press_sequentially("vim -Nu NONE -U NONE -i NONE -n -c 'set number' edit-note.txt\n", delay=10)
        page.wait_for_timeout(400)
        terminal.press("Home")
        terminal.press("i")
        terminal.press_sequentially("Edited through real ttyd: readable text. ", delay=10)
        page.wait_for_timeout(200)
        if evidence:
            page.screenshot(path=str(evidence / f"ttyd-vim-{size[0]}x{size[1]}.png"))
        terminal.press("Escape")
        terminal.press_sequentially(":wq\n", delay=20)
        note = official_stack.directory / "terminal-project" / "edit-note.txt"
        for _ in range(100):
            if note.read_text().startswith("Edited through real ttyd: readable text. "):
                break
            page.wait_for_timeout(50)
        assert note.read_text() == "Edited through real ttyd: readable text. Original isolated note\n"
        terminal.press_sequentially("printf 'line %s\\n' {1..150}\n", delay=5)
        page.wait_for_timeout(300)
        assert page.locator(".xterm-viewport").evaluate("node => node.scrollHeight > node.clientHeight")
        page.set_viewport_size({"width": max(360, size[0] - 100), "height": size[1]})
        page.wait_for_timeout(200)
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(1200)
        if evidence:
            page.screenshot(path=str(evidence / f"ttyd-official-{size[0]}x{size[1]}.png"))
        assert not diagnostics["errors"], diagnostics
        assert not diagnostics["missing"], diagnostics
        if evidence:
            (evidence / f"vendor-diagnostics-{size[0]}x{size[1]}.json").write_text(
                json.dumps(diagnostics, indent=2), encoding="utf-8"
            )
        terminal.press_sequentially("exit\n", delay=15)
        page.get_by_text("Press ⏎ to Reconnect", exact=False).wait_for()
        terminal.press("Enter")
        page.wait_for_timeout(500)
        terminal.press_sequentially("printf 'RECONNECT_%s_OK\\n' REAL\n", delay=10)
        for _ in range(100):
            if "RECONNECT_REAL_OK" in "".join(terminal_output):
                break
            page.wait_for_timeout(50)
        assert "RECONNECT_REAL_OK" in "".join(terminal_output)
        # ttyd 1.7.7 may emit this upstream protocol diagnostic on intentional
        # shell exit (close code 1005); do not suppress unrelated console errors.
        expected_exit = [message for message in diagnostics["errors"] if
                         "Received a broken close frame containing a reserved status code" in message]
        assert diagnostics["errors"] == expected_exit, diagnostics
        assert not diagnostics["missing"], diagnostics
        if evidence:
            (evidence / f"vendor-runtime-{size[0]}x{size[1]}.json").write_text(json.dumps({
                "filebrowser": "2.31.2", "ttyd": "1.7.7", "caddy": "2.8.4",
                "terminalOutput": terminal_output,
                "intentionalShellExitDiagnostics": expected_exit,
                "unexpectedErrors": [], "missingResources": diagnostics["missing"],
                "externalRequests": diagnostics["externalRequests"],
                "offlineCheckPassed": not diagnostics["externalRequests"],
                "terminalTools": {"ls": "real ls -la output", "git": "isolated repo status, no commit",
                                  "editor": "vim -Nu NONE -U NONE -i NONE -n; typed, saved, byte-verified",
                                  "savedNote": note.read_text()},
                "environment": "isolated x86_64 development host; not board acceptance",
            }, indent=2), encoding="utf-8")
    finally:
        context.close()


@pytest.mark.parametrize("size", [VIEWPORTS[0], VIEWPORTS[1]], ids=lambda size: f"{size[0]}x{size[1]}")
def test_official_filebrowser_editor_has_no_runtime_external_dependencies(browser, official_stack, size):
    """Strict independent offline contract; never masks file/terminal capability tests."""
    context = browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    diagnostics = diagnostics_for(page)
    try:
        sign_in(page, official_stack.base)
        page.goto(official_stack.base + "/files/")
        page.get_by_text("browser-example.txt", exact=True).dblclick()
        page.locator("#editor").wait_for()
        page.wait_for_timeout(500)
        assert "A real isolated File Browser file" in page.locator("#editor").inner_text()
        if os.getenv("CLOUDHOST_EVIDENCE_DIR"):
            path = Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) / f"filebrowser-offline-{size[0]}x{size[1]}.json"
            path.write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
        assert not diagnostics["externalRequests"], diagnostics
        assert not diagnostics["errors"], diagnostics
        assert not diagnostics["missing"], diagnostics
    finally:
        context.close()


def test_official_filebrowser_search_views_focus_and_site_publication(browser, official_stack):
    """Operate only on the real stack's private www fixture, never a user site."""
    context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
    page = context.new_page()
    evidence = Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) if os.getenv("CLOUDHOST_EVIDENCE_DIR") else None
    base = official_stack.base
    results = {"environment": "isolated real Caddy/FB; private pytest www; not board acceptance"}

    def capture(name):
        if evidence:
            page.screenshot(path=str(evidence / f"filebrowser-{name}-1366x768.png"))

    def upload(name, body):
        page.get_by_role("button", name="Upload", exact=True).click()
        with page.expect_file_chooser() as chooser:
            page.locator("#focus-prompt").click()
        chooser.value.set_files({"name": name, "mimeType": "text/html", "buffer": body})

    try:
        sign_in(page, base)
        page.goto(base + "/site/")
        assert page.locator('h1[data-i18n="site.heroTitle"]').is_visible()
        original_site = (official_stack.directory / "cloud/www/index.html").read_bytes()
        page.goto(base + "/files/")
        search = page.get_by_role("textbox", name="Search")
        search.click()
        search.fill("browser-example")
        search.press("Enter")
        page.locator("#search li a").filter(has_text="browser-example.txt").wait_for()
        assert page.locator("#search li a").count() == 1
        capture("search")
        results["search"] = "Real server search returns only matching fixture file"
        page.goto(base + "/files/")
        modes = []
        for _ in range(3):
            modes.append(page.locator("#listing").get_attribute("class"))
            capture("view-" + str(len(modes)))
            page.get_by_role("button", name="Switch view", exact=True).click()
        assert len(set(modes)) == 3, modes
        results["views"] = modes
        page.goto(base + "/files/")
        page.get_by_text("browser-example.txt", exact=True).wait_for()
        page.wait_for_function("getComputedStyle(document.body).backgroundColor === 'rgb(7, 20, 38)'")
        page.keyboard.press("Tab")
        page.wait_for_function("parseFloat(getComputedStyle(document.activeElement).outlineWidth) >= 3")
        focused = page.locator(":focus")
        assert focused.count() == 1
        focus_style = focused.evaluate("""node => {
            const s = getComputedStyle(node);
            return {label: node.getAttribute('aria-label'), tag: node.tagName, html: node.outerHTML.slice(0, 300), outline: s.outlineStyle,
                    width: s.outlineWidth, visible: node.getBoundingClientRect().width > 0};
        }""")
        assert focus_style["visible"] and focus_style["outline"] != "none"
        assert float(focus_style["width"].removesuffix("px")) >= 3, focus_style
        capture("keyboard-focus")
        results["keyboardFocus"] = focus_style
        page.get_by_text("www", exact=True).dblclick()
        page.get_by_text("index.html", exact=True).wait_for()
        # Replacing a file through the actual official upload dialog exercises
        # the user publication workflow, not a direct test write to site_dir.
        published = b"<!doctype html><html lang=en><meta charset=utf-8><title>Isolated publication</title><body><h1>Published through real File Browser</h1><p>PRIVATE_FIXTURE_PUBLICATION_OK</p></body></html>"
        upload("index.html", published)
        page.locator(".card-action").get_by_role("button", name=re.compile("Replace|Overwrite", re.I)).click()
        for _ in range(100):
            if (official_stack.directory / "cloud/www/index.html").read_bytes() == published:
                break
            page.wait_for_timeout(100)
        assert (official_stack.directory / "cloud/www/index.html").read_bytes() == published
        capture("published-www")
        page.goto(base + "/site/")
        page.reload()
        page.get_by_role("heading", name="Published through real File Browser", exact=True).wait_for()
        assert "PRIVATE_FIXTURE_PUBLICATION_OK" in page.locator("body").inner_text()
        assert context.request.get(base + "/site/").body() == published
        capture("published-site")
        results["publication"] = {
            "path": "private pytest cloud/www/index.html", "originalBytes": len(original_site),
            "publishedBytes": len(published), "actualFileBytesVerified": True,
            "actualCaddySiteResponseBytesVerified": True,
            "workflow": "Official FB overwrite upload -> disk -> Caddy /site/ reload",
        }
        if evidence:
            (evidence / "filebrowser-publication-and-controls.json").write_text(
                json.dumps(results, indent=2), encoding="utf-8"
            )
    finally:
        context.close()


def test_official_filebrowser_upload_progress(browser, official_stack):
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    base = official_stack.base
    evidence = Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) if os.getenv("CLOUDHOST_EVIDENCE_DIR") else None
    try:
        sign_in(page, base)
        page.goto(base + "/files/")
        page.get_by_text("browser-example.txt", exact=True).wait_for()
        session = context.new_cdp_session(page)
        session.send("Network.enable")
        session.send("Network.emulateNetworkConditions", {
            "offline": False, "latency": 100, "downloadThroughput": 1024 * 1024,
            "uploadThroughput": 64 * 1024,
        })
        body = b"Real isolated upload progress\n" * 20000
        page.locator("#upload-input").set_input_files(
            {"name": "progress-example.txt", "mimeType": "text/plain", "buffer": body})
        page.locator(".upload-files .card.floating").wait_for()
        page.wait_for_function("""() => {
            const node = document.querySelector('.upload-percentage');
            return node && parseFloat(node.textContent) > 0 && parseFloat(node.textContent) < 100;
        }""", timeout=20000)
        progress = page.locator(".upload-info").inner_text()
        if evidence:
            page.screenshot(path=str(evidence / "filebrowser-upload-progress-1366x768.png"))
        page.get_by_text("progress-example.txt", exact=True).wait_for(timeout=30000)
        assert (official_stack.directory / "cloud/progress-example.txt").read_bytes() == body
        if evidence:
            (evidence / "filebrowser-upload-progress.json").write_text(json.dumps({
                "environment": "Real official FB/Caddy with browser upload throughput limited to 64KiB/s",
                "progress": progress, "uploadedBytesVerified": len(body),
                "actualFileBytesVerified": True,
            }, indent=2), encoding="utf-8")
    finally:
        context.close()


def test_official_filebrowser_upload_permission_failure_is_visible(browser, official_stack):
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    base = official_stack.base
    evidence = Path(os.environ["CLOUDHOST_EVIDENCE_DIR"]) if os.getenv("CLOUDHOST_EVIDENCE_DIR") else None
    denied = official_stack.directory / "cloud" / "upload-denied"
    denied.mkdir()
    responses = []
    page.on("response", lambda response: responses.append(
        {"url": response.url, "status": response.status}) if "/api/" in response.url and response.status >= 400 else None)
    try:
        sign_in(page, base)
        page.goto(base + "/files/upload-denied/")
        page.get_by_role("button", name="Upload", exact=True).wait_for()
        denied.chmod(0o555)
        page.locator("#upload-input").set_input_files(
            {"name": "must-not-exist.txt", "mimeType": "text/plain", "buffer": b"permission failure fixture\n"})
        for _ in range(200):
            if responses:
                break
            page.wait_for_timeout(50)
        assert responses, "The actual read-only directory must reject the upload"
        page.locator(".upload-files .card.floating").wait_for(state="hidden", timeout=90000)
        assert not (denied / "must-not-exist.txt").exists()
        visible_errors = page.locator(".Vue-Toastification__toast, .notyf__toast, .toast, [role=alert]").filter(visible=True).all_inner_texts()
        if evidence:
            page.screenshot(path=str(evidence / "filebrowser-upload-permission-error-1366x768.png"))
            (evidence / "filebrowser-upload-feedback.json").write_text(json.dumps({
                "environment": "Real official FB/Caddy, private pytest directory chmod0555 restored in finally",
                "errorResponses": responses, "visibleErrors": visible_errors,
                "failedFileAbsent": True,
                "uploadQueueFinished": True,
                "officialSource": "v2.31.2 frontend/src/stores/upload.ts setError only records error; processUploads finish resets state",
            }, indent=2), encoding="utf-8")
        assert visible_errors, "Official client must visibly report the real upload permission failure"
    finally:
        denied.chmod(0o755)
        context.close()
