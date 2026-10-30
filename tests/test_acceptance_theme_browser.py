"""Independent PRD acceptance; run only after coordinator content-hash freeze.

All /api and embedded-app samples below are explicitly browser fixtures. They
prove presentation/lifecycle only, never actual Caddy/File Browser/ttyd or Pi.
Actual device, first-paint filmstrip, third-party transfer and shell evidence
are separate acceptance gates, not replaced by this suite.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from app import routes_pages
from app.main import create_app
from app.services import ServiceManager
from conftest import TEST_PASSWORD, TEST_USER


PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(os.getenv(
    "CLOUDHOST_ACCEPTANCE_EVIDENCE",
    "/home/tris/.copilot/session-state/08a6968e-45c9-48ea-ad13-ae189c1742da"
    "/files/theme-acceptance",
))
THEMES = ("classic", "bento", "swiss", "cyber")
LANGUAGES = ("en", "zh")
VIEWPORTS = ((360, 800), (390, 844), (768, 1024), (1280, 800), (1920, 1080))
ALIASES = {
    "classic": r"classic|经典|深蓝",
    "bento": r"bento|硬件.*展台",
    "swiss": r"swiss|瑞士",
    "cyber": r"cyber|赛博",
}
PORTS = range(18361, 18370)


def source_manifest():
    paths = []
    for folder in ("portal", "deploy", "tools", "scripts"):
        paths.extend(
            p for p in (PROJECT / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        )
    paths.append(PROJECT / "install.sh")
    values = {
        str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }
    digest = hashlib.sha256(json.dumps(
        values, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return digest, values


@pytest.fixture(autouse=True)
def acceptance_freeze_guard():
    expected = os.getenv("CLOUDHOST_ACCEPTANCE_SOURCE_SHA256")
    if not expected:
        pytest.skip("NOT RUN: coordinator candidate freeze/hash has not been supplied")
    before, manifest = source_manifest()
    if before != expected:
        pytest.exit("STOP: product source differs from coordinator-frozen hash", returncode=3)
    delivery = Path(os.getenv(
        "CLOUDHOST_ACCEPTANCE_FREEZE_RECORD",
        str(EVIDENCE / "evidence/freeze-verified.json"),
    ))
    frozen_delivery = json.loads(delivery.read_text())["verified_delivery_files"] if delivery.exists() else {}

    def check_delivery():
        for name, digest in frozen_delivery.items():
            if hashlib.sha256((PROJECT.parents[1] / name).read_bytes()).hexdigest() != digest:
                pytest.exit("STOP: frozen delivery/document/test changed: " + name, returncode=3)

    check_delivery()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "candidate-source-manifest.json").write_text(json.dumps({
        "sha256": before, "files": manifest,
        "scope": "portal/deploy/tools/scripts/install.sh; excludes acceptance tests",
    }, indent=2) + "\n")
    yield
    check_delivery()
    if source_manifest()[0] != expected:
        pytest.exit("STOP: product source changed during acceptance; rerun affected evidence",
                    returncode=3)


@pytest.fixture(scope="module")
def acceptance_browser():
    if not os.getenv("CLOUDHOST_ACCEPTANCE_SOURCE_SHA256"):
        pytest.skip("NOT RUN: no coordinator freeze, browser acceptance not started")
    api = pytest.importorskip("playwright.sync_api")
    engine = os.getenv("CLOUDHOST_ACCEPTANCE_BROWSER", "firefox")
    executable = os.getenv(
        "CLOUDHOST_ACCEPTANCE_EXECUTABLE",
        "/home/tris/.cache/ms-playwright/firefox-1555/firefox/firefox",
    )
    with api.sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(executable_path=executable)
        yield browser
        browser.close()


@pytest.fixture
def acceptance_portal(config, fake_auth, monkeypatch):
    monkeypatch.setattr(routes_pages, "_ttyd_available", lambda cfg: True)
    application = create_app(config, authenticator=fake_auth)
    application.state.service_manager = ServiceManager(
        runner=lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, "ActiveState=active\nUnitFileState=enabled\n", ""
        )
    )
    reservation = socket.socket()
    reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for port in PORTS:
        try:
            reservation.bind(("127.0.0.1", port))
            break
        except OSError:
            continue
    else:
        reservation.close()
        pytest.fail("No free acceptance-only loopback port in 18361–18369")
    server = uvicorn.Server(uvicorn.Config(
        application, host="127.0.0.1", port=port, log_level="error"
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [reservation]}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/api/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(.05)
        else:
            pytest.fail("Acceptance loopback server not responsive")
        yield SimpleNamespace(base=base, config=config, application=application)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        reservation.close()
        assert not thread.is_alive(), "Acceptance server did not stop"


STATUS = {
    "time": "2026-10-26T12:00:00",
    "cpu": {"percent": 25, "load1": .1, "load5": .2, "load15": .3},
    "mem": {"percent": 35, "used_mb": 1400, "total_mb": 4000},
    "temp": {"cpu_c": 45},
    "disk": {"root_percent": 40, "root_used_gb": 40, "root_total_gb": 100},
    "net": {"iface": "acceptance_fixture", "rx_kbps": 12, "tx_kbps": 8},
    "uptime_sec": 12345,
}
REMOTE = {
    "observed_at": None, "generated_at": "2026-10-26T12:00:00",
    "stale": False,
    "summary": {"enabled": False, "active_provider": "none", "connection": "unknown",
                "https": "off", "reachability": "unverified"},
}
FRAME = (
    "<!doctype html><html><head><title>Acceptance lifecycle fixture, not real service</title>"
    "</head><body><h1>Fixture only</h1><label>Draft<input id='draft'></label>"
    "<details open><summary>Fixture details</summary>Retain this state</details>"
    "<script>window.fixtureIdentity=crypto.randomUUID();</script></body></html>"
)


def install_fixtures(page, state="normal"):
    status = json.loads(json.dumps(STATUS))
    services = [
        {"name": "fixture-running", "state": "running", "enabled": True, "detail": "Fixture"},
        {"name": "fixture-failed", "state": "failed", "enabled": False, "detail": "Fixture"},
        {"name": "fixture-unknown", "state": "unknown", "enabled": False, "detail": "Fixture"},
        {"name": "fixture-starting", "state": "starting", "enabled": True, "detail": "Fixture"},
    ]
    if state == "warning":
        status["cpu"]["percent"] = 80
        status["mem"]["percent"] = 95
    elif state == "unknown":
        status["temp"]["cpu_c"] = None
        status["net"]["rx_kbps"] = None
    elif state == "empty":
        services = []
    page.route("**/api/status", lambda route: route.fulfill(
        status=503 if state == "failure" else 200,
        json={"error": {"code": "FIXTURE_FAILURE"}} if state == "failure" else status,
    ))
    page.route("**/api/services", lambda route: route.fulfill(json={"services": services}))
    remote = json.loads(json.dumps(REMOTE))
    remote["stale"] = state == "stale"
    page.route("**/api/remote/status", lambda route: route.fulfill(json=remote))
    page.route("**/api/remote/peanut", lambda route: route.fulfill(json={
        key: {"state": value, "source": "acceptance_fixture", "observed_at": None,
              "stale": state == "stale", "reason": "fixture_only"}
        for key, value in (
            ("app_config", "missing"), ("installation", "not_installed"),
            ("daemon", "stopped"), ("device", "unknown"), ("binding", "unknown"),
            ("https", "off"),
        )
    } | {
        "intent": {"enabled": False, "paused_this_boot": False}, "revision": 1,
        "capabilities": {"qr_binding": False, "management": False, "unbind": False,
                         "unavailable_reason": "fixture_only"},
        "address": {"https_url": ""},
    }))
    for path in ("files", "site", "term"):
        page.route(f"**/{path}/", lambda route: route.fulfill(
            content_type="text/html", body=FRAME,
        ))


def theme_button(page):
    return page.get_by_role("button", name=re.compile(r"(?:theme|主题)", re.I)).first


def theme_notice(page, pattern):
    return page.locator(
        '[role="status"]:not([data-i18n-status]), [aria-live="polite"]:not([data-i18n-status]):not([role="status"])'
    ).filter(has_text=re.compile(pattern, re.I))


def option(page, theme):
    return page.get_by_role("radio", name=re.compile(ALIASES[theme], re.I))


def open_selector(page):
    button = theme_button(page)
    assert button.is_visible(), "FR-02: visible text theme entry required"
    button.click()
    assert button.get_attribute("aria-expanded") == "true"
    assert page.get_by_role("radio").count() == 4, "Exactly four named radio options"
    return button


def selected(page):
    radios = page.get_by_role("radio")
    return [
        radios.nth(i).evaluate("""n=>n.getAttribute('aria-label') ||
          (n.labels && Array.from(n.labels).map(l=>l.textContent).join(' ')) ||
          n.textContent""")
        for i in range(radios.count())
        if radios.nth(i).get_attribute("aria-checked") == "true"
        or radios.nth(i).evaluate("n => n.matches('input:checked')")
    ]


def choose(page, theme):
    button = open_selector(page)
    item = option(page, theme)
    assert item.count() == 1, f"Theme needs unique localized accessible name: {theme}"
    item.click()
    assert button.get_attribute("aria-expanded") == "false"
    assert button.evaluate("n => n === document.activeElement"), "Focus must return to entry"
    saved = page.evaluate("localStorage.getItem('cloudhost.theme')")
    assert saved == theme, f"Persist only fixed theme identifier: {saved!r}"


def language(page, locale):
    control = page.locator(f'[data-language="{locale}"]')
    if not control.is_visible():
        page.locator("#tools-toggle").click()
    control.click()
    if page.locator("#tools-toggle").count() and page.locator("#tools-toggle").get_attribute(
            "aria-expanded") == "true":
        page.locator("#tools-toggle").click()
    assert page.locator("html").get_attribute("lang") == locale


def sign_in(page, base):
    page.goto(base + "/login")
    if not page.locator("#username").count():
        page.locator("#view-overview").wait_for(state="visible")
        return
    page.locator("#username").fill(TEST_USER)
    page.locator("#password").fill(TEST_PASSWORD)
    page.locator("#login-submit").click()
    page.locator("#view-overview").wait_for(state="visible")


def navigate(page, view):
    if page.locator("#nav-toggle").is_visible():
        page.locator("#nav-toggle").click()
    page.locator(f'.nav-tab[data-view="{view}"]').click()
    page.wait_for_function("(view) => location.hash === '#' + view", arg=view)


def record(page, name, extra=None):
    target = EVIDENCE / "candidate-browser"
    target.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target / f"{name}.png"), full_page=True)
    if name.endswith(("-login-normal", "-overview-normal")):
        page.screenshot(
            path=str(target / f"{name}-blind.png"), full_page=True,
            mask=[theme_button(page)],
        )
    payload = page.evaluate("""() => ({
      url: location.href, viewport: {width: innerWidth, height: innerHeight},
      documentWidth: document.documentElement.scrollWidth,
      colorScheme: getComputedStyle(document.documentElement).colorScheme,
      rootAttributes: Object.fromEntries(Array.from(document.documentElement.attributes)
        .filter(a => !/csrf/i.test(a.name)).map(a => [a.name,a.value]))
    })""")
    payload.update({
        "browser": page.context.browser.version,
        "fixture_boundary": "Controlled API/embedded documents; NOT real board/service",
        **(extra or {}),
    })
    (target / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def assert_layout(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Page overflow"
    bad_targets = page.locator(
        "button:visible, a.btn:visible, summary:visible, [role=radio]:visible"
    ).evaluate_all("""nodes => nodes.map(n => {
      let r=n.getBoundingClientRect(); return {name:n.textContent.trim(),w:r.width,h:r.height};
    }).filter(r=>r.w<44 || r.h<44)""")
    assert not bad_targets, ("Touch targets below 44 CSS px", bad_targets)
    assert theme_button(page).is_visible()
    assert "{{" not in page.locator("body").inner_text()


@pytest.mark.parametrize("theme,locale,size", itertools.product(THEMES, LANGUAGES, VIEWPORTS))
def test_acceptance_theme_login_overview_matrix(
        acceptance_browser, acceptance_portal, theme, locale, size):
    context = acceptance_browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    install_fixtures(page)
    try:
        page.goto(acceptance_portal.base + "/login")
        language(page, locale)
        choose(page, theme)
        assert_layout(page)
        record(page, f"{theme}-{locale}-{size[0]}x{size[1]}-login-normal")
        sign_in(page, acceptance_portal.base)
        assert page.locator("html").get_attribute("lang") == locale
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == theme
        assert_layout(page)
        assert page.locator(".purpose-panel").bounding_box()["y"] < \
            page.locator(".metrics").bounding_box()["y"]
        assert page.locator("#overview-site-link").get_attribute("target") == "_blank"
        assert page.locator("#overview-services > details").evaluate("n=>!n.open")
        record(page, f"{theme}-{locale}-{size[0]}x{size[1]}-overview-normal")
    finally:
        context.close()


@pytest.mark.parametrize("theme,locale,size", itertools.product(
    THEMES, LANGUAGES, ((390, 844), (1280, 800))))
def test_acceptance_theme_regions(
        acceptance_browser, acceptance_portal, theme, locale, size):
    context = acceptance_browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    install_fixtures(page)
    try:
        sign_in(page, acceptance_portal.base)
        language(page, locale)
        choose(page, theme)
        page.locator("#overview-services > details > summary").click()
        record(page, f"{theme}-{locale}-{size[0]}x{size[1]}-services-fixture")
        for view in ("remote", "ssh", "files", "site", "term"):
            navigate(page, view)
            if view in ("files", "site", "term"):
                frame_id = {"files": "embed-frame", "site": "site-frame", "term": "term-frame"}[view]
                page.frame_locator("#" + frame_id).locator("#draft").wait_for()
            assert_layout(page)
            record(page, f"{theme}-{locale}-{size[0]}x{size[1]}-{view}-fixture")
    finally:
        context.close()


@pytest.mark.parametrize("location", ("login", "portal"))
@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("locale", LANGUAGES)
def test_acceptance_theme_keyboard(
        acceptance_browser, acceptance_portal, location, theme, locale):
    context = acceptance_browser.new_context()
    page = context.new_page()
    install_fixtures(page)
    try:
        if location == "portal":
            sign_in(page, acceptance_portal.base)
        else:
            page.goto(acceptance_portal.base + "/login")
        language(page, locale)
        choose(page, theme)
        button = open_selector(page)
        original = option(page, theme)
        assert original.evaluate("n=>n===document.activeElement")
        before = page.evaluate("localStorage.getItem('cloudhost.theme')")
        page.keyboard.press("ArrowRight")
        candidate = page.locator(":focus")
        assert candidate.get_attribute("role") == "radio" or \
            candidate.get_attribute("type") == "radio"
        assert not original.evaluate("n=>n===document.activeElement")
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == before
        page.keyboard.press("Escape")
        assert button.evaluate("n=>n===document.activeElement")
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == before
        open_selector(page)
        page.keyboard.press("ArrowRight")
        page.locator("h1").first.click()
        assert button.evaluate("n=>n===document.activeElement")
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == before
        for key in ("Space", "Enter"):
            open_selector(page)
            page.keyboard.press("ArrowRight")
            focused_name = page.locator(":focus").evaluate("""n=>n.getAttribute('aria-label') ||
              (n.labels && Array.from(n.labels).map(l=>l.textContent).join(' ')) || n.textContent""")
            page.keyboard.press(key)
            assert button.get_attribute("aria-expanded") == "false"
            assert button.evaluate("n=>n===document.activeElement")
            open_selector(page)
            assert len(selected(page)) == 1
            assert any(re.search(ALIASES[t], focused_name, re.I)
                       and re.search(ALIASES[t], selected(page)[0], re.I) for t in THEMES)
            page.keyboard.press("Escape")
        record(page, f"{theme}-{locale}-{location}-keyboard")
    finally:
        context.close()


INSTRUMENT = r"""(() => {
  if (window!==top) return;
  window.__acceptanceLedger = {fetches:[], intervals:[], timeouts:[], viewchanges:0};
  const ledger=window.__acceptanceLedger;
  const oldFetch=window.fetch;
  window.fetch=function(...args) {
    ledger.fetches.push({url:String(args[0]),method:(args[1]||{}).method||'GET',
      stack:new Error().stack,time:performance.now()});
    return oldFetch.apply(this,args);
  };
  for (const [name,list] of [['setInterval','intervals'],['setTimeout','timeouts']]) {
    const old=window[name];
    window[name]=function(cb,delay,...args) {
      ledger[list].push({delay,stack:new Error().stack,time:performance.now()});
      return old.call(this,cb,delay,...args);
    };
  }
  window.addEventListener('cloudhost:viewchange',()=>ledger.viewchanges++);
})();"""


def remember_business(page, frame_id=None):
    return page.evaluate("""frameId => {
      const nodes = Array.from(document.querySelectorAll(
        '#login-form,input,textarea,details,#view-overview,#view-remote,#view-embed,iframe'));
      window.__acceptanceNodes=nodes;
      window.__acceptanceState=nodes.map(n=>({
        node:n,value:n.value,selectionStart:n.selectionStart,selectionEnd:n.selectionEnd,
        open:n.open,disabled:n.disabled,hidden:n.hidden
      }));
      const frame=frameId && document.getElementById(frameId);
      window.__acceptanceFrame=frame ? {
        node:frame,doc:frame.contentDocument,src:frame.getAttribute('src'),
        identity:frame.contentWindow.fixtureIdentity,loads:0,
        draft:frame.contentDocument.querySelector('#draft').value
      }:null;
      if(frame) frame.addEventListener('load',()=>window.__acceptanceFrame.loads++);
      const ledger=window.__acceptanceLedger;
      return {url:location.href,history:history.length,viewchanges:ledger.viewchanges,
        intervalCount:ledger.intervals.length,timeoutCount:ledger.timeouts.length,
        fetchCount:ledger.fetches.length};
    }""", frame_id)


def assert_business_unchanged(page, before):
    result = page.evaluate("""before => {
      const ledger=window.__acceptanceLedger;
      const bad=window.__acceptanceState.filter(s=>!s.node.isConnected ||
        s.node.value!==s.value || s.node.open!==s.open ||
        s.node.disabled!==s.disabled || s.node.hidden!==s.hidden ||
        s.node.selectionStart!==s.selectionStart || s.node.selectionEnd!==s.selectionEnd);
      const f=window.__acceptanceFrame;
      const frameOK=!f || (f.node.isConnected && f.node.contentDocument===f.doc &&
        f.node.getAttribute('src')===f.src && f.loads===0 &&
        f.node.contentWindow.fixtureIdentity===f.identity &&
        f.doc.querySelector('#draft').value===f.draft);
      const requests=ledger.fetches.slice(before.fetchCount);
      return {bad:bad.map(s=>s.node.id),frameOK,url:location.href,history:history.length,
        viewchanges:ledger.viewchanges,intervalCount:ledger.intervals.length,
        themeTimeouts:ledger.timeouts.slice(before.timeoutCount).filter(t=>/theme/i.test(t.stack)),
        forbiddenRequests:requests.filter(r=>r.method!=='GET' ||
          !['/api/status','/api/services','/api/remote/peanut','/api/remote/status'].includes(r.url)
          || /theme/i.test(r.stack))};
    }""", before)
    assert result["bad"] == [], result
    assert result["frameOK"], result
    for key in ("url", "history", "viewchanges", "intervalCount"):
        assert result[key] == before[key], (key, before, result)
    assert not result["forbiddenRequests"], result
    assert not result["themeTimeouts"], result
    return result


@pytest.mark.parametrize("view", ("login", "overview", "remote", "files", "site", "term"))
def test_acceptance_theme_all_directed_continuity(
        acceptance_browser, acceptance_portal, view):
    context = acceptance_browser.new_context(viewport={"width": 1280, "height": 800})
    context.add_init_script(INSTRUMENT)
    page = context.new_page()
    install_fixtures(page)
    samples = []
    try:
        if view == "login":
            page.goto(acceptance_portal.base + "/login")
            page.locator("#username").fill("Independent draft username")
            page.locator("#password").fill("independent-test-only-password")
            page.locator("#username").evaluate("n=>n.setSelectionRange(2,8)")
        else:
            sign_in(page, acceptance_portal.base)
            if view != "overview":
                navigate(page, view)
                if view == "remote":
                    if not page.locator("#peanut-address-entry").evaluate("n=>n.open"):
                        page.locator("#peanut-address-entry > summary").click()
                    page.locator("#peanut-address").fill("https://acceptance-draft.example.invalid")
                    page.locator("#peanut-ssh-address").fill("[2001:db8::1]:2222")
                    page.locator("#peanut-ssh-address").evaluate("n=>n.setSelectionRange(2,8)")
                    page.locator(".remote-diagnostics").evaluate("n=>n.open=true")
            else:
                page.locator("#overview-services > details > summary").click()
        frame_id = {"files": "embed-frame", "site": "site-frame", "term": "term-frame"}.get(view)
        if frame_id:
            page.frame_locator("#" + frame_id).locator("#draft").fill("Persist fixture draft")
            page.wait_for_function("document.querySelector('#embed-hint').hidden")
        for source, target in itertools.permutations(THEMES, 2):
            choose(page, source)
            before = remember_business(page, frame_id)
            choose(page, target)
            samples.append({"from": source, "to": target,
                            "checks": assert_business_unchanged(page, before)})
            before = remember_business(page, frame_id)
            page.evaluate("""() => {
              window.__acceptanceWrites=0; const old=Storage.prototype.setItem;
              window.__acceptanceOldSet=old;
              Storage.prototype.setItem=function(...args) {
                if(args[0]==='cloudhost.theme') window.__acceptanceWrites++;
                return old.apply(this,args);
              };
            }""")
            choose(page, target)
            assert page.evaluate("window.__acceptanceWrites") == 0, \
                "Repeated successfully saved selection must not write again"
            page.evaluate("() => { Storage.prototype.setItem=window.__acceptanceOldSet; }")
            assert_business_unchanged(page, before)
        record(page, f"{view}-12-directed-transitions", {"samples": samples})
    finally:
        context.close()


@pytest.mark.parametrize("invalid", ("", "removed-theme", '{"theme":"swiss"}',
                                    '"><script>window.__themePayloadExecuted=true</script>'))
@pytest.mark.parametrize("path", ("/", "/login"))
def test_acceptance_theme_invalid_storage(
        acceptance_browser, acceptance_portal, invalid, path):
    context = acceptance_browser.new_context()
    context.add_init_script(
        "localStorage.setItem('cloudhost.theme'," + json.dumps(invalid) + ");"
        "localStorage.setItem('acceptance.unrelated','retain');"
    )
    page = context.new_page()
    try:
        page.goto(acceptance_portal.base + path)
        assert page.evaluate("!window.__themePayloadExecuted")
        assert page.evaluate("localStorage.getItem('acceptance.unrelated')") == "retain"
        open_selector(page)
        assert len(selected(page)) == 1
        assert re.search(ALIASES["classic"], selected(page)[0], re.I)
        page.keyboard.press("Escape")
        status = theme_notice(page, r"unavailable|invalid|无效|不可用")
        assert status.is_visible(), "Invalid saved theme requires visible explanation"
        choose(page, "swiss")
        assert page.evaluate("localStorage.getItem('acceptance.unrelated')") == "retain"
        record(page, f"invalid-{hashlib.sha256(invalid.encode()).hexdigest()[:8]}-{path=='/'}")
    finally:
        context.close()


@pytest.mark.parametrize("failure", ("object", "getItem", "setItem"))
def test_acceptance_theme_storage_failure_recovery(
        acceptance_browser, acceptance_portal, failure):
    context = acceptance_browser.new_context()
    injection = """
      localStorage.setItem('cloudhost.theme','classic');
      window.__acceptanceFailStorage=true;
      window.__acceptanceStored=window.localStorage;
      window.__acceptanceGet=Storage.prototype.getItem;
      window.__acceptanceSet=Storage.prototype.setItem;
    """
    if failure == "object":
        injection += """Object.defineProperty(window,'localStorage',{
          configurable:true,get(){if(window.__acceptanceFailStorage)
          throw new DOMException('Acceptance fixture','SecurityError');
          return window.__acceptanceStored;}});"""
    else:
        injection += f"""
          Storage.prototype.{failure}=function(...args){{
            if(window.__acceptanceFailStorage && args[0]==='cloudhost.theme')
              throw new DOMException('Acceptance fixture','QuotaExceededError');
            return window.__acceptance{'Get' if failure=='getItem' else 'Set'}.apply(this,args);
          }};
        """
    context.add_init_script(injection)
    page = context.new_page()
    try:
        page.goto(acceptance_portal.base + "/login")
        if failure in ("object", "getItem"):
            assert theme_notice(page, r"read|读取").is_visible()
        button = open_selector(page)
        option(page, "swiss").click()
        assert button.get_attribute("aria-expanded") == "false"
        assert page.locator("body").is_visible()
        if failure in ("object", "setItem"):
            assert theme_notice(page, r"cannot|unable|could not|无法|未保存|not saved").is_visible()
            language(page, "zh")
            assert theme_notice(page, r"无法|未保存").is_visible()
            language(page, "en")
            assert theme_notice(page, r"cannot|unable|could not|not saved").is_visible()
        if failure == "setItem":
            assert page.evaluate("localStorage.getItem('cloudhost.theme')") == "classic"
            page.reload()
            open_selector(page)
            assert re.search(ALIASES["classic"], selected(page)[0], re.I)
            page.keyboard.press("Escape")
            open_selector(page)
            option(page, "swiss").click()
        page.evaluate("window.__acceptanceFailStorage=false")
        choose(page, "swiss")
        assert not theme_notice(page, r"cannot|unable|could not|not saved").is_visible()
        record(page, f"storage-{failure}-recovery")
    finally:
        context.close()


@pytest.mark.parametrize("size", ((859, 800), (860, 800), (861, 800), (844, 390)))
@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_breakpoint_motion(
        acceptance_browser, acceptance_portal, size, theme):
    context = acceptance_browser.new_context(
        viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce"
    )
    page = context.new_page()
    install_fixtures(page)
    try:
        sign_in(page, acceptance_portal.base)
        choose(page, theme)
        assert_layout(page)
        open_selector(page)
        assert_layout(page)
        running = page.evaluate("""() => document.getAnimations().filter(a=>{
          const t=a.effect.getComputedTiming();return t.activeDuration>0;
        }).map(a=>({name:a.animationName,timing:a.effect.getTiming()}))""")
        assert not running, ("Nonessential animation under reduced-motion", running)
        record(page, f"{theme}-{size[0]}x{size[1]}-reduce-selector")
    finally:
        context.close()


@pytest.mark.parametrize("theme,locale,state,size", itertools.product(
    THEMES, LANGUAGES, ("normal", "warning", "failure", "unknown", "stale", "empty"),
    ((390, 844), (1280, 800))))
def test_acceptance_theme_state_evidence(
        acceptance_browser, acceptance_portal, theme, locale, state, size):
    context = acceptance_browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    install_fixtures(page, state=state)
    try:
        sign_in(page, acceptance_portal.base)
        language(page, locale)
        choose(page, theme)
        page.locator("#overview-services > details > summary").click()
        if state == "warning":
            assert page.locator("#cpu-bar").get_attribute("class") == "warn"
            assert page.locator("#mem-bar").get_attribute("class") == "crit"
        elif state == "failure":
            page.wait_for_function(
                "document.querySelector('#status-dot').classList.contains('dead')")
        elif state == "unknown":
            assert page.locator("#temp-value").inner_text() == "—"
        elif state == "empty":
            assert page.locator("#service-rows [data-service]").count() == 0
            assert page.locator("#service-rows").inner_text().strip()
        assert_layout(page)
        record(page, f"{theme}-{locale}-{size[0]}x{size[1]}-{state}-fixture")
        navigate(page, "remote")
        if state == "stale":
            page.locator(".remote-diagnostics > summary").click()
            assert page.locator("#remote-stale").is_visible()
        record(page, f"{theme}-{locale}-{size[0]}x{size[1]}-remote-{state}-fixture")
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_login_error_loading_preserved(
        acceptance_browser, acceptance_portal, theme):
    context = acceptance_browser.new_context()
    page = context.new_page()
    posts = []
    pending = []
    page.route("**/auth/login", lambda route: (
        posts.append(route.request.post_data), pending.append(route)
    ))
    try:
        page.goto(acceptance_portal.base + "/login?redirect=%2F%23files")
        choose(page, theme)
        page.locator("#username").fill("")
        page.locator("#password").fill("")
        page.locator("#login-submit").click()
        error_before = page.locator("#login-error").inner_text()
        assert error_before
        page.locator("#username").fill("Fixture account")
        page.locator("#password").fill("fixture-password-not-a-credential")
        page.locator("#username").evaluate("n=>n.setSelectionRange(1,6)")
        for target in THEMES:
            choose(page, target)
            assert page.locator("#login-error").inner_text() == error_before
            assert page.locator("#username").input_value() == "Fixture account"
            assert page.locator("#username").evaluate("n=>[n.selectionStart,n.selectionEnd]") == [1, 6]
        assert not posts, "Theme choices must not POST credentials"
        page.locator("#login-submit").click()
        page.wait_for_function("document.querySelector('#login-submit').disabled")
        for target in THEMES:
            choose(page, target)
            assert page.locator("#login-submit").is_disabled()
            assert page.locator("#password").input_value() == "fixture-password-not-a-credential"
            assert len(posts) == 1
        record(page, f"{theme}-login-loading-fixture")
        pending.pop().fulfill(status=401, json={"error": {"code": "E1001"}})
        page.wait_for_function("!document.querySelector('#login-submit').disabled")
        assert page.locator("#login-error").is_visible()
        assert page.url.endswith("redirect=%2F%23files")
    finally:
        for route in pending:
            route.abort()
        context.close()


@pytest.mark.parametrize("view,frame_id", (
    ("files", "embed-frame"), ("site", "site-frame"), ("term", "term-frame")))
@pytest.mark.parametrize("size", ((390, 844), (1280, 800)))
def test_acceptance_theme_workspace_geometry(
        acceptance_browser, acceptance_portal, view, frame_id, size):
    context = acceptance_browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    install_fixtures(page)
    geometries = []
    try:
        sign_in(page, acceptance_portal.base)
        navigate(page, view)
        page.frame_locator("#" + frame_id).locator("#draft").wait_for()
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        for theme in THEMES:
            choose(page, theme)
            geometry = {
                selector: page.locator(selector).bounding_box()
                for selector in (".topbar", ".workspace-bar", "#" + frame_id)
            }
            for selector, box in geometry.items():
                assert box is not None, (theme, selector)
                if geometries:
                    for dimension in ("x", "y", "width", "height"):
                        assert abs(box[dimension] - geometries[0]["geometry"][selector][dimension]) <= 1, \
                            (theme, selector, dimension, box, geometries)
            assert geometry["#" + frame_id]["y"] + geometry["#" + frame_id]["height"] <= size[1] + 1
            geometries.append({"theme": theme, "geometry": geometry})
        # A theme write warning is real page UI, but the storage failure is a fixture.
        page.evaluate("""() => {
          const old=Storage.prototype.setItem; window.__acceptanceRestoreStorage=old;
          Storage.prototype.setItem=function(k,v) {
            if(k==='cloudhost.theme')throw new DOMException('fixture','QuotaExceededError');
            return old.call(this,k,v);
          };
        }""")
        button = open_selector(page)
        option(page, "classic").click()
        assert button.get_attribute("aria-expanded") == "false"
        theme_notice(page, r"cannot|unable|could not|not saved|无法|未保存").wait_for()
        page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
        frame = page.locator("#" + frame_id).bounding_box()
        assert frame["y"] + frame["height"] <= size[1] + 1
        assert page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1")
        record(page, f"{view}-{size[0]}x{size[1]}-geometry-warning",
               {"geometry_samples": geometries, "warning_frame": frame})
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_native_history_and_terminal_leave(
        acceptance_browser, acceptance_portal, theme):
    context = acceptance_browser.new_context()
    context.add_init_script(INSTRUMENT)
    page = context.new_page()
    install_fixtures(page)
    try:
        sign_in(page, acceptance_portal.base)
        navigate(page, "files")
        page.frame_locator("#embed-frame").locator("#draft").fill("File state")
        navigate(page, "term")
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        page.wait_for_function("""() => {
          const f=document.querySelector('#term-frame');
          return f.contentDocument.readyState==='complete'&&!!f.contentWindow.fixtureIdentity;
        }""")
        page.evaluate("() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        page.frame_locator("#term-frame").locator("#draft").fill("Terminal fixture state")
        assert page.frame_locator("#term-frame").locator("#draft").input_value() == \
            "Terminal fixture state", "Fixture draft must exist before theme interaction"
        before = remember_business(page, "term-frame")
        choose(page, theme)
        checks = assert_business_unchanged(page, before)
        record(page, theme + "-native-history-before-leave", {"continuity": checks})
        assert page.frame_locator("#term-frame").locator("#draft").input_value() == \
            "Terminal fixture state"
        page.go_back()
        page.wait_for_function("location.hash==='#files'")
        assert page.locator("#term-frame").get_attribute("src") == "about:blank"
        assert page.frame_locator("#embed-frame").locator("#draft").input_value() == "File state"
        page.go_forward()
        page.wait_for_function("location.hash==='#term'")
        page.frame_locator("#term-frame").locator("#draft").wait_for()
        assert page.frame_locator("#term-frame").locator("#draft").input_value() == ""
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_401_and_terminal_disabled(
        acceptance_browser, acceptance_portal, theme, monkeypatch):
    context = acceptance_browser.new_context()
    page = context.new_page()
    install_fixtures(page)
    try:
        sign_in(page, acceptance_portal.base)
        navigate(page, "term")
        page.frame_locator("#term-frame").locator("#draft").wait_for()
        choose(page, theme)
        page.route("**/api/status", lambda route: route.fulfill(
            status=401, json={"error": {"code": "E1001"}}))
        page.wait_for_url("**/login?redirect=**")
        assert page.locator("#login-form").is_visible()
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == theme
        page.route("**/api/status", lambda route: route.fulfill(json=STATUS))
        monkeypatch.setattr(routes_pages, "_ttyd_available", lambda cfg: False)
        sign_in(page, acceptance_portal.base)
        assert page.locator("#term-tab").is_hidden()
        assert page.locator("#term-card").is_hidden()
        assert page.locator("#shell-fallback").is_visible()
        choose(page, theme)
        assert page.locator("#term-tab").is_hidden()
        assert page.locator("#term-card").is_hidden()
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("position", ("middle", "bottom"))
def test_acceptance_theme_overview_anchor(
        acceptance_browser, acceptance_portal, theme, position):
    context = acceptance_browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    install_fixtures(page)
    samples = []
    try:
        sign_in(page, acceptance_portal.base)
        page.locator("#overview-services > details > summary").click()
        choose(page, theme)
        page.evaluate("""position => scrollTo(0, position==='bottom'
          ?document.documentElement.scrollHeight:document.documentElement.scrollHeight/2)""",
                      position)
        page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
        for target in THEMES:
            # Opening an offscreen entry with the pointer can itself scroll the page.
            # Trigger keyboard-style activation with preventScroll, then measure the
            # business anchor before application; do not charge normal entry scrolling.
            theme_button(page).evaluate("n=>{n.focus({preventScroll:true});n.click()}")
            anchor = page.evaluate("""() => {
              const edge=document.querySelector('.topbar').getBoundingClientRect().bottom;
              const candidates=[...document.querySelectorAll(
                '#view-overview .product-hero,#view-overview .link-card,'
                +'#view-overview .panel-head,#view-overview .metric,'
                +'#view-overview .submetrics,#overview-services')];
              const inventory=candidates.map(n=>{
                const b=n.getBoundingClientRect();
                return {id:n.id,className:n.className,text:n.textContent.trim().slice(0,120),
                  top:b.top,bottom:b.bottom,width:b.width,height:b.height};
              });
              const index=inventory.findIndex(b=>b.width>0&&b.height>0
                &&b.bottom>edge&&b.top<innerHeight);
              if(index<0)throw Error('No visible business content anchor');
              const n=candidates[index];
              window.__acceptanceAnchor=n;
              return {top:n.getBoundingClientRect().top,scroll:scrollY,
                max:document.documentElement.scrollHeight-innerHeight,
                selected:inventory[index],candidateInventory:inventory,topbarBottom:edge};
            }""")
            option(page, target).evaluate("n=>n.click()")
            page.evaluate("() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(resolve))))")
            after = page.evaluate("""() => ({top:window.__acceptanceAnchor.getBoundingClientRect().top,
              scroll:scrollY,max:document.documentElement.scrollHeight-innerHeight})""")
            # If the desired scroll is beyond the new document end, clamp is permitted.
            desired = after["scroll"] + after["top"] - anchor["top"]
            clamped = max(0, min(desired, after["max"]))
            samples.append({"target": target, "before": anchor, "after": after,
                            "clamped_scroll": clamped,
                            "actual_offset_error_px": abs(after["scroll"] - clamped)})
            (EVIDENCE / f"{theme}-{position}-overview-anchor.json").write_text(
                json.dumps(samples, ensure_ascii=False, indent=2) + "\n")
            assert abs(after["scroll"] - clamped) <= 8, (theme, target, anchor, after)
            assert theme_button(page).evaluate("n=>n===document.activeElement")
    finally:
        context.close()


@pytest.mark.parametrize("position", ("middle", "bottom"))
def test_acceptance_theme_remote_long_form_anchor(
        acceptance_browser, acceptance_portal, position):
    context = acceptance_browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    install_fixtures(page, "stale")
    samples = []
    try:
        sign_in(page, acceptance_portal.base)
        navigate(page, "remote")
        page.locator("#peanut-address-entry").evaluate("n=>n.open=true")
        page.locator(".remote-diagnostics").evaluate("n=>n.open=true")
        page.locator("#peanut-ssh-address").fill("[2001:db8::1234]:2222")
        page.evaluate("""p=>scrollTo(0,p==='bottom'
          ?document.documentElement.scrollHeight:document.documentElement.scrollHeight/2)""",
                      position)
        page.evaluate("() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        for theme in THEMES:
            theme_button(page).evaluate("n=>{n.focus({preventScroll:true});n.click()}")
            before = page.evaluate("""()=>{
              const edge=document.querySelector('.topbar').getBoundingClientRect().bottom;
              const nodes=[...document.querySelectorAll('#view-remote p,#view-remote label,#view-remote summary,#view-remote h3')];
              const n=nodes.find(n=>n.getBoundingClientRect().width&&n.getBoundingClientRect().bottom>edge);
              window.__acceptanceRemoteAnchor=n;
              return {text:n.textContent,top:n.getBoundingClientRect().top,scroll:scrollY};
            }""")
            option(page, theme).evaluate("n=>n.click()")
            page.evaluate("() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(r))))")
            after = page.evaluate("""()=>({
              top:window.__acceptanceRemoteAnchor.getBoundingClientRect().top,scroll:scrollY,
              max:document.documentElement.scrollHeight-innerHeight})""")
            desired = after["scroll"] + after["top"] - before["top"]
            error = abs(after["scroll"] - max(0, min(desired, after["max"])))
            samples.append({"theme": theme, "before": before, "after": after, "error_px": error})
        record(page, "remote-long-form-anchor-" + position, {"samples": samples})
        assert all(s["error_px"] <= 8 for s in samples), samples
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_storage_scope_and_logout(
        acceptance_browser, acceptance_portal, theme):
    context = acceptance_browser.new_context()
    first, second = context.new_page(), context.new_page()
    try:
        first.goto(acceptance_portal.base + "/login")
        second.goto(acceptance_portal.base + "/login")
        choose(first, theme)
        # Existing second page is allowed to retain its own appearance until reload.
        second.reload()
        open_selector(second)
        assert len(selected(second)) == 1
        assert re.search(ALIASES[theme], selected(second)[0], re.I)
        second.keyboard.press("Escape")
        language(first, "zh")
        second.wait_for_function("document.documentElement.lang==='zh'")
        assert first.evaluate("localStorage.getItem('cloudhost.theme')") == theme
        sign_in(first, acceptance_portal.base)
        first.locator("#tools-toggle").click()
        first.locator("#logout-btn").click()
        first.wait_for_url("**/login")
        assert first.evaluate("localStorage.getItem('cloudhost.theme')") == theme
        other = context.new_page()
        other.goto(acceptance_portal.base.replace("127.0.0.1", "localhost") + "/login")
        assert other.evaluate("localStorage.getItem('cloudhost.theme')") is None
    finally:
        context.close()


@pytest.mark.parametrize("scheme,location", itertools.product(
    ("light", "dark"), ("login", "portal")))
def test_acceptance_theme_default_is_classic_without_implicit_save(
        acceptance_browser, acceptance_portal, scheme, location):
    context = acceptance_browser.new_context(color_scheme=scheme)
    page = context.new_page()
    install_fixtures(page)
    try:
        if location == "portal":
            sign_in(page, acceptance_portal.base)
        else:
            page.goto(acceptance_portal.base + "/login")
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") is None
        assert page.locator("html").evaluate(
            "n=>getComputedStyle(n).colorScheme") == "dark"
        open_selector(page)
        assert len(selected(page)) == 1
        assert re.search(ALIASES["classic"], selected(page)[0], re.I)
        page.keyboard.press("Escape")
        assert not theme_notice(page, r"saved|已保存").is_visible()
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_saved_preference_restores_new_document(
        acceptance_browser, acceptance_portal, theme):
    context = acceptance_browser.new_context()
    page = context.new_page()
    try:
        page.goto(acceptance_portal.base + "/login")
        choose(page, theme)
        page.reload()
        open_selector(page)
        assert len(selected(page)) == 1
        assert re.search(ALIASES[theme], selected(page)[0], re.I)
        page.close()
        fresh = context.new_page()
        fresh.goto(acceptance_portal.base + "/")
        open_selector(fresh)
        assert len(selected(fresh)) == 1
        assert re.search(ALIASES[theme], selected(fresh)[0], re.I)
    finally:
        context.close()


@pytest.mark.parametrize("theme,locale", itertools.product(THEMES, LANGUAGES))
def test_acceptance_theme_long_text_fixture(
        acceptance_browser, acceptance_portal, theme, locale):
    context = acceptance_browser.new_context(viewport={"width": 360, "height": 800})
    page = context.new_page()
    install_fixtures(page)
    try:
        sign_in(page, acceptance_portal.base)
        language(page, locale)
        choose(page, theme)
        # Explicit text fixture, never a mutation of a real account/service.
        page.locator(".brand-sub").evaluate(
            "(n,text)=>n.textContent=text",
            "Quectel Pi M2 · acceptance_fixture_very_long_account_" * 4)
        navigate(page, "remote")
        if not page.locator("#peanut-address-entry").evaluate("n=>n.open"):
            page.locator("#peanut-address-entry > summary").click()
        long_url = "https://" + ("long-fixture-segment." * 8) + "example.invalid"
        long_ssh = "ssh -p 65535 fixture_user@[2001:db8:1234:5678:9012:abcd:ef01:2345]"
        page.locator("#peanut-address").fill(long_url)
        page.locator("#peanut-ssh-address").fill("[2001:db8:1234:5678:9012:abcd:ef01:2345]:65535")
        page.locator("#ssh-command").evaluate("(n,text)=>n.textContent=text", long_ssh)
        assert_layout(page)
        for selector in ("#peanut-address", "#peanut-ssh-address"):
            box = page.locator(selector).bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 360
        record(page, f"{theme}-{locale}-360x800-long-text-fixture")
    finally:
        context.close()


def test_acceptance_theme_apply_failure_does_not_persist_false_success(
        acceptance_browser, acceptance_portal):
    context = acceptance_browser.new_context()
    page = context.new_page()
    try:
        page.goto(acceptance_portal.base + "/login")
        choose(page, "classic")
        appearance = page.locator("html").evaluate("""n=>({
          colorScheme:getComputedStyle(n).colorScheme,
          background:getComputedStyle(document.body).backgroundColor})""")
        page.evaluate("""() => {
          const root=document.documentElement;
          window.__acceptanceApplyFaultHit=false;
          const fail=v=>{
            if(String(v).includes('swiss')){
              window.__acceptanceApplyFaultHit=true;
              throw new Error('Independent acceptance visual-application fault');
            }
          };
          const oldAttribute=Element.prototype.setAttribute;
          Element.prototype.setAttribute=function(k,v) {
            if(this===root) fail(v);
            return oldAttribute.call(this,k,v);
          };
          const descriptor=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'dataset');
          if(descriptor && descriptor.configurable) {
            let proxy;
            Object.defineProperty(HTMLElement.prototype,'dataset',{
              configurable:true,enumerable:descriptor.enumerable,
              get:function() {
                const value=descriptor.get.call(this);
                if(this!==root)return value;
                if(!proxy) proxy=new Proxy(value,{
                  set(target,k,v){fail(v);target[k]=v;return true;}
                });
                return proxy;
              }
            });
          }
          for(const method of ['add','replace','toggle']) {
            const old=DOMTokenList.prototype[method];
            DOMTokenList.prototype[method]=function(...args) {
              if(this===root.classList)args.forEach(fail);
              return old.apply(this,args);
            };
          }
        }""")
        open_selector(page)
        option(page, "swiss").click()
        if not page.evaluate("window.__acceptanceApplyFaultHit"):
            pytest.skip("NOT RUN: apply-failure injector did not intercept candidate's actual visual path")
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == "classic"
        after = page.locator("html").evaluate("""n=>({
          colorScheme:getComputedStyle(n).colorScheme,
          background:getComputedStyle(document.body).backgroundColor})""")
        assert after == appearance, "Failed application must keep last working appearance"
        assert theme_notice(page, r"fail|unable|could not|未能|失败|无法").is_visible()
        if theme_button(page).get_attribute("aria-expanded") != "true":
            open_selector(page)
        assert len(selected(page)) == 1
        assert re.search(ALIASES["classic"], selected(page)[0], re.I)
        record(page, "application-failure-fixture-retains-classic")
    finally:
        context.close()
