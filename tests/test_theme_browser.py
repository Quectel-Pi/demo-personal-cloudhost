"""Theme behavior on an isolated local portal; iframe/API data are test fixtures."""
import json
import os
from pathlib import Path
import socket
import time

import pytest

import test_redesign_browser as harness
from test_redesign_browser import browser, live_portal, sign_in, navigate_view, mock_remote

THEMES = ("classic", "bento", "swiss", "cyber")
SIZES = ((360, 800), (390, 844), (768, 1024), (1280, 800), (1920, 1080))


@pytest.fixture(autouse=True)
def theme_ports(monkeypatch):
    def port():
        for number in range(int(os.getenv("CLOUDHOST_THEME_TEST_PORT", "18351")), int(os.getenv("CLOUDHOST_THEME_TEST_PORT", "18351")) + 9):
            with socket.socket() as handle:
                handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    handle.bind(("127.0.0.1", number))
                except OSError:
                    continue
                return number
        raise RuntimeError("Theme test ports busy")
    monkeypatch.setattr(harness, "free_port", port)


def pick(page, name):
    page.locator("#theme-toggle").click()
    page.locator(f'[data-theme-choice="{name}"]').click()
    assert page.locator("html").get_attribute("data-theme") == name
    assert page.locator(f'[data-theme-choice="{name}"]').get_attribute("aria-checked") == "true"
    assert page.locator("#theme-panel").is_hidden()


def evidence(page, label):
    folder = os.getenv("CLOUDHOST_THEME_EVIDENCE_DIR")
    if folder:
        directory = Path(folder)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"{label}.png"), full_page=True)


@pytest.mark.parametrize("name", THEMES)
@pytest.mark.parametrize("language", ("en", "zh"))
@pytest.mark.parametrize("motion", ("no-preference", "reduce"))
def test_theme_login_loading_transition_contrast(browser, live_portal, name, language, motion):
    context = browser.new_context(reduced_motion=motion)
    page = context.new_page()
    held = []
    page.route("**/auth/login", lambda route: held.append(route))
    try:
        page.goto(live_portal.base + "/login")
        page.locator(f'[data-language="{language}"]').click()
        pick(page, name)
        page.wait_for_function("!document.documentElement.hasAttribute('data-theme-changing')")
        page.locator("#password").fill("deliberately-invalid-test-value")
        page.evaluate("""() => {
          const button=document.querySelector('#login-submit');
          window.loadingContrast={enter:[],exit:[]};
          function sample() {
            const s=getComputedStyle(button);
            const rgb=v=>(v.match(/[\\d.]+/g)||[]).map(Number).slice(0,3);
            const fg=rgb(s.color);
            const bg=rgb(s.backgroundImage==='none'?s.backgroundColor:s.backgroundImage.match(/rgba?\\([^)]+\\)/)[0]);
            const lum=c=>c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;})
              .reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
            const a=lum(fg),b=lum(bg);
            return {time:performance.now(),foreground:s.color,background:s.backgroundColor,
              ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),disabled:button.disabled,
              text:button.textContent,opacity:s.opacity};
          }
          new MutationObserver(records=>{
            if(!records.some(r=>r.attributeName==='disabled'))return;
            const phase=button.disabled?'enter':'exit';
            loadingContrast[phase].push(sample());
            function frame() {
              loadingContrast[phase].push(sample());
              const values=loadingContrast[phase];
              if(values[values.length-1].time-values[0].time<350)requestAnimationFrame(frame);
            }
            requestAnimationFrame(frame);
          }).observe(button,{attributes:true,attributeFilter:['disabled']});
        }""")
        page.locator("#login-submit").click()
        page.wait_for_function("window.loadingContrast.enter.length >= 2")
        page.wait_for_timeout(400)
        assert len(held) == 1
        assert page.locator("#login-submit").is_disabled()
        assert page.locator("#login-submit").inner_text() == ("Signing in…" if language == "en" else "登录中…")
        held[0].fulfill(status=401, json={"error": {"code": "E1001"}})
        page.wait_for_function("window.loadingContrast.exit.length >= 2")
        page.wait_for_timeout(400)
        samples = page.evaluate("window.loadingContrast")
        folder = os.getenv("CLOUDHOST_THEME_EVIDENCE_DIR")
        if folder:
            directory = Path(folder) / "loading-transition"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{browser.browser_type.name}-{name}-{language}-{motion}.json").write_text(
                json.dumps({"browser": browser.version, "theme": name, "language": language,
                            "motion": motion, "samples": samples}, indent=2))
        for phase, values in samples.items():
            assert values[-1]["time"] - values[0]["time"] >= 350
            assert all(v["ratio"] >= 4.5 for v in values), (name, language, motion, phase, values)
            assert all(v["disabled"] == (phase == "enter") for v in values)
        assert page.locator("#login-error").is_visible()
        assert page.locator("#login-submit").is_enabled()
    finally:
        context.close()


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("language", ("en", "zh"))
def test_theme_visual_matrix(browser, live_portal, size, language):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    page = context.new_page()
    page.add_init_script(f"localStorage.setItem('cloudhost.language', '{language}')")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(live_portal.base + "/login")
        for view in ("login", "overview"):
            if view == "overview":
                sign_in(page, live_portal.base)
                page.wait_for_function("document.querySelector('#cpu-percent').textContent !== '—'")
            for name in THEMES:
                pick(page, name)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (name, view, page.evaluate("""() => ({
                    width:innerWidth,scroll:document.documentElement.scrollWidth,
                    nodes:[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth).slice(0,12).map(n=>[n.tagName,n.className,n.getBoundingClientRect().right])
                })"""))
                assert not errors
                assert page.locator("#theme-toggle").bounding_box()["height"] >= 44
                page.locator("#theme-toggle").click()
                box = page.locator("#theme-panel").bounding_box()
                assert box["x"] >= 0 and box["x"] + box["width"] <= size[0]
                assert box["y"] >= 0 and box["y"] + box["height"] <= size[1]
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.keyboard.press("Escape")
                assert page.locator("html").evaluate("n => getComputedStyle(n).colorScheme") == ("light" if name == "swiss" else "dark")
                if size[0] >= 1280 and view == "overview":
                    assert page.locator(".topbar").bounding_box()["height"] <= 80
                evidence(page, f"{name}-{view}-{language}-{size[0]}x{size[1]}")
        mock_remote(page)
        for view in ("remote", "ssh"):
            navigate_view(page, view)
            for name in THEMES:
                pick(page, name)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                if size in (SIZES[1], SIZES[3]):
                    evidence(page, f"{name}-{view}-{language}-{size[0]}x{size[1]}-fixture")
        navigate_view(page, "overview")
        page.locator("#services-title").click()
        for name in THEMES:
            pick(page, name)
            assert page.locator("#overview-services > details").evaluate("n => n.open")
            if size in (SIZES[1], SIZES[3]):
                evidence(page, f"{name}-services-{language}-{size[0]}x{size[1]}")
    finally:
        context.close()


@pytest.mark.parametrize("value", ["", "deleted", '<script>alert("bad")</script>'])
def test_theme_invalid_storage_is_not_absent(browser, live_portal, value):
    context = browser.new_context()
    context.add_init_script("localStorage.setItem('cloudhost.theme'," + json.dumps(value) + ")")
    page = context.new_page()
    page.goto(live_portal.base + "/login")
    assert page.locator("html").get_attribute("data-theme") == "classic"
    assert "saved theme is unavailable" in page.locator("[data-theme-status]").inner_text()
    pick(page, "bento")
    assert page.evaluate("localStorage.getItem('cloudhost.theme')") == "bento"
    context.close()


@pytest.mark.parametrize("kind", ("object", "read", "write"))
def test_theme_storage_failure_retry_and_language(browser, live_portal, kind):
    context = browser.new_context()
    context.add_init_script("""
      localStorage.setItem('cloudhost.theme','swiss');
      window.themeDeny = true; window.themeWrites = 0;
      const get = Storage.prototype.getItem, set = Storage.prototype.setItem;
      const storage = localStorage;
      if (KIND === 'object') Object.defineProperty(window,'localStorage',{get(){
        if(window.themeDeny) throw new Error('denied'); return storage;
      }});
      Storage.prototype.getItem = function(k) {
        if(KIND === 'read' && window.themeDeny && k === 'cloudhost.theme') throw new Error('denied');
        return get.call(this,k);
      };
      Storage.prototype.setItem = function(k,v) {
        if(k === 'cloudhost.theme') {window.themeWrites++; if(window.themeDeny) throw new Error('quota');}
        return set.call(this,k,v);
      };
    """.replace("KIND", json.dumps(kind)))
    page = context.new_page()
    page.goto(live_portal.base + "/login")
    assert page.locator("html").get_attribute("data-theme") == ("swiss" if kind == "write" else "classic")
    if kind != "write":
        assert "Cannot read" in page.locator("[data-theme-status]").inner_text()
        assert page.evaluate("CloudhostThemeBoot.failure") == ("storage_object" if kind == "object" else "getItem")
    pick(page, "bento")
    assert "could not save" in page.locator("[data-theme-status]").inner_text()
    if kind == "write":
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == "swiss"
        other = context.new_page()
        other.goto(live_portal.base + "/login")
        assert other.locator("html").get_attribute("data-theme") == "swiss"
        assert page.locator("html").get_attribute("data-theme") == "bento"
        other.close()
    page.locator('[data-language="zh"]').click()
    assert "无法保存" in page.locator("[data-theme-status]").inner_text()
    assert page.locator("[data-theme-status]").is_visible()
    page.evaluate("window.themeDeny = false")
    pick(page, "bento")
    assert page.evaluate("localStorage.getItem('cloudhost.theme')") == "bento"
    writes = page.evaluate("window.themeWrites")
    text = page.locator("[data-theme-status]").text_content()
    pick(page, "bento")
    assert page.evaluate("window.themeWrites") == writes
    assert page.locator("[data-theme-status]").text_content() == text
    context.close()


def test_theme_keyboard_failure_recovery_and_login_draft(browser, live_portal):
    context = browser.new_context()
    page = context.new_page()
    page.goto(live_portal.base + "/login?redirect=%2F%23files")
    page.locator("#password").fill("not-a-secret-fixture")
    page.locator("#password").evaluate("n => n.setSelectionRange(2,8)")
    page.locator("#theme-toggle").click()
    assert page.locator('[data-theme-choice="classic"]').evaluate("n => n === document.activeElement")
    page.keyboard.press("ArrowRight")
    assert page.locator("html").get_attribute("data-theme") == "classic"
    page.keyboard.press("Escape")
    assert page.locator("#theme-toggle").evaluate("n => n === document.activeElement")
    page.locator("#theme-toggle").click()
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    assert page.locator("html").get_attribute("data-theme") == "bento"
    page.locator("#theme-toggle").click()
    page.keyboard.press("ArrowRight")
    page.locator("#login-title").click()
    assert page.locator("html").get_attribute("data-theme") == "bento"
    assert page.locator("#theme-toggle").evaluate("n => n === document.activeElement")
    page.evaluate("""() => {window.realComputed = window.getComputedStyle;
      window.getComputedStyle = function(n) {
        if(n === document.documentElement) throw new Error('application fixture');
        return window.realComputed(n);
      };}""")
    page.locator("#theme-toggle").click()
    page.locator('[data-theme-choice="cyber"]').click()
    assert page.locator("html").get_attribute("data-theme") == "bento"
    assert page.evaluate("localStorage.getItem('cloudhost.theme')") == "bento"
    assert page.locator('[data-theme-choice="bento"]').get_attribute("aria-checked") == "true"
    assert "could not be applied" in page.locator("[data-theme-status]").inner_text()
    page.evaluate("() => {window.getComputedStyle = window.realComputed}")
    pick(page, "cyber")
    assert page.locator("#password").input_value() == "not-a-secret-fixture"
    assert page.locator("#password").evaluate("n => [n.selectionStart,n.selectionEnd]") == [2, 8]
    context.close()


@pytest.mark.parametrize("frame_view", ("files", "site", "term"))
@pytest.mark.parametrize("size", ((390, 844), (1280, 800)))
@pytest.mark.parametrize("language", ("en", "zh"))
def test_theme_keeps_business_identity_geometry_requests(browser, live_portal, monkeypatch, frame_view, size, language):
    monkeypatch.setattr(harness.routes_pages, "_ttyd_available", lambda cfg: True)
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    context.add_init_script(f"localStorage.setItem('cloudhost.language','{language}')")
    page = context.new_page()
    sign_in(page, live_portal.base)
    page.route("**/" + frame_view + "/", lambda route: route.fulfill(
        content_type="text/html", body="<title>Fixture app</title><input id=draft value=unchanged>"))
    navigate_view(page, frame_view)
    frame_id = {"files": "embed-frame", "site": "site-frame", "term": "term-frame"}[frame_view]
    page.frame_locator("#" + frame_id).locator("#draft").wait_for()
    page.frame_locator("#" + frame_id).locator("#draft").evaluate("n => n.setSelectionRange(1,5)")
    page.wait_for_function("document.querySelector('#embed-hint').hidden")
    page.evaluate("""id => {
      window.themeFrame = document.getElementById(id);window.themeDoc = themeFrame.contentDocument;
      window.themeHistory = history.length; window.themeUrl = location.href;
      window.themeTimers = 0;window.themeLoads = 0;window.themeViewchanges = 0;
      themeFrame.addEventListener('load',()=>themeLoads++);
      addEventListener('cloudhost:viewchange',()=>themeViewchanges++);
      window.realInterval = window.setInterval;window.setInterval = function(...args){themeTimers++;return realInterval(...args);};
    }""", frame_id)
    calls = []
    page.on("request", lambda request: calls.append((request.method, request.url)))
    baseline = page.locator("#" + frame_id).bounding_box()
    captured = set()
    transitions = [(source, target) for source in THEMES for target in THEMES if source != target]
    for source, name in transitions:
        pick(page, source)
        pick(page, name)
        now = page.locator("#" + frame_id).bounding_box()
        assert all(abs(now[key] - baseline[key]) <= 1 for key in ("x", "y", "width", "height")), (name, baseline, now)
        assert page.evaluate("""() => themeFrame === document.getElementById(themeFrame.id) &&
          themeDoc === themeFrame.contentDocument && history.length === themeHistory &&
          location.href === themeUrl && themeLoads === 0 && themeTimers === 0 && themeViewchanges === 0""")
        assert page.frame_locator("#" + frame_id).locator("#draft").input_value() == "unchanged"
        assert page.frame_locator("#" + frame_id).locator("#draft").evaluate("n => [n.selectionStart,n.selectionEnd]") == [1, 5]
        if name not in captured:
            evidence(page, f"{name}-{frame_view}-{language}-{size[0]}x{size[1]}-normal-fixture")
            captured.add(name)
    assert not [(m, u) for m, u in calls if m != "GET" or any(p in u for p in ("/files/", "/site/", "/term/"))]
    # Same frame remains after a real theme warning changes the available height.
    pick(page, "bento")
    page.evaluate("""Storage.prototype.setItem = function(k,v) { if(k === 'cloudhost.theme') throw new Error('quota'); };""")
    warning_geometry = []
    for name in THEMES:
        pick(page, name)
        page.wait_for_timeout(50)
        geometry = page.locator("#" + frame_id).bounding_box()
        warning_geometry.append(geometry)
        assert geometry["y"] + geometry["height"] <= size[1]
        assert page.locator("[data-theme-status]").is_visible()
        assert page.evaluate("themeDoc === themeFrame.contentDocument && themeLoads === 0")
        evidence(page, f"{name}-{frame_view}-{language}-{size[0]}x{size[1]}-write-warning-fixture")
    for geometry in warning_geometry[1:]:
        assert all(abs(geometry[key] - warning_geometry[0][key]) <= 1 for key in ("x", "y", "width", "height")), warning_geometry
    context.close()


@pytest.mark.parametrize("failure", ("theme.js", "theme-boot.js", "themes.css", "no-js"))
def test_theme_resource_failure_remains_readable(browser, live_portal, failure):
    context = browser.new_context(java_script_enabled=failure != "no-js")
    page = context.new_page()
    if failure != "no-js":
        page.route("**/static/" + failure + "*", lambda route: route.abort())
    page.goto(live_portal.base + "/login")
    assert page.locator("#login-title").is_visible()
    assert page.locator("#username").is_visible()
    assert page.locator("[data-theme-status]").is_visible()
    assert page.locator("#login-title").evaluate("n => getComputedStyle(n).visibility") == "visible"
    context.close()


def test_theme_scope_gateway_and_restore(browser, live_portal):
    context = browser.new_context()
    page = context.new_page()
    page.goto(live_portal.base + "/")
    assert page.locator("#login-form").is_visible()
    assert page.evaluate("localStorage.getItem('cloudhost.theme')") is None
    other = context.new_page()
    other.goto(live_portal.base + "/login")
    pick(page, "swiss")
    assert other.locator("html").get_attribute("data-theme") == "classic"
    other.reload()
    assert other.locator("html").get_attribute("data-theme") == "swiss"
    for path in ("/files/", "/site", "/term/"):
        page.goto(live_portal.base + path)
        assert page.locator("html").get_attribute("data-theme") == "swiss"
        assert page.locator("#theme-toggle").is_visible()
        assert page.locator("#gateway-message").inner_text()
    sign_in(page, live_portal.base)
    assert page.locator("html").get_attribute("data-theme") == "swiss"
    page.locator("#tools-toggle").click()
    page.locator("#logout-btn").click()
    page.wait_for_selector("#login-form")
    assert page.locator("html").get_attribute("data-theme") == "swiss"
    context.close()


def test_theme_all_directed_switches_remote_draft_and_scroll(browser, live_portal):
    context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
    page = context.new_page()
    sign_in(page, live_portal.base)
    page.locator("#services-title").click()
    failures = []
    for source in THEMES:
        for target in THEMES:
            if source == target:
                continue
            pick(page, source)
            # A visible content anchor at mid-page, with explicit bottom clamping.
            page.locator(".metric").nth(2).scroll_into_view_if_needed()
            page.evaluate("scrollBy(0,85)")
            point = page.evaluate("""() => {
              const edge=document.querySelector('.topbar').getBoundingClientRect().bottom;
              const nodes=[...document.querySelectorAll('#view-overview .product-hero,#view-overview .link-card,#view-overview .panel-head,#view-overview .metric,#view-overview .submetrics,#overview-services')];
              const n=nodes.find(n=>n.getBoundingClientRect().bottom>edge);
              window.anchorNode=n;return {top:n.getBoundingClientRect().top,scroll:scrollY};
            }""")
            pick(page, target)
            page.wait_for_timeout(50)
            measured = page.evaluate("""() => ({top:anchorNode.getBoundingClientRect().top,scroll:scrollY,max:Math.max(0,document.documentElement.scrollHeight-innerHeight)})""")
            error = abs(measured["top"] - point["top"])
            if error > 8 and measured["scroll"] not in (0, measured["max"]):
                failures.append((source, target, point, measured))
    assert not failures, failures
    mock_remote(page)
    navigate_view(page, "remote")
    page.locator("#peanut-address-entry").evaluate("n => n.open=true")
    page.locator("#peanut-address").fill("https://draft.example.invalid")
    page.locator("#peanut-ssh-address").fill("draft.example.invalid:2222")
    page.locator("#peanut-ssh-address").evaluate("n => n.setSelectionRange(2,7)")
    original = page.evaluate("""() => { window.draftNode=document.querySelector('#peanut-address-form');
      return {url:location.href,history:history.length};}""")
    writes = []
    page.on("request", lambda r: writes.append(r.method) if r.method != "GET" else None)
    for name in THEMES * 3:
        pick(page, name)
        assert page.locator("#peanut-address").input_value() == "https://draft.example.invalid"
        assert page.locator("#peanut-ssh-address").input_value() == "draft.example.invalid:2222"
        assert page.locator("#peanut-ssh-address").evaluate("n => [n.selectionStart,n.selectionEnd]") == [2, 7]
        assert page.evaluate("draftNode === document.querySelector('#peanut-address-form')")
        assert page.locator("#peanut-address-entry").evaluate("n => n.open")
        assert page.url == original["url"]
        assert page.evaluate("history.length") == original["history"]
    assert not writes
    context.close()


def test_theme_first_render_and_timing_samples(browser, live_portal):
    context = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
    page = context.new_page()
    sign_in(page, live_portal.base)
    samples = []
    for name in THEMES:
        pick(page, name)
        for path in ("/login?redirect=%2F", "/"):
            page.goto(live_portal.base + path)
            assert page.locator("html").get_attribute("data-theme") == name
            assert page.locator("html").evaluate("n => getComputedStyle(n).getPropertyValue('--theme-ready').trim()") == name
    for index in range(36):
        name = THEMES[(index + 1) % 4]
        page.locator("#theme-toggle").click()
        # Timing starts in the input event and ends after the first presentation opportunity.
        value = page.evaluate("""name => new Promise(resolve => {
          const button=document.querySelector('[data-theme-choice="'+name+'"]');
          button.addEventListener('click',function(){
            const start=performance.now();
            requestAnimationFrame(()=>requestAnimationFrame(()=>resolve(performance.now()-start)));
          },{once:true,capture:true});
          button.click();
        })""", name)
        samples.append(value)
    p95 = sorted(samples)[int(len(samples) * .95)]
    assert p95 <= 300, (p95, samples)
    folder = os.getenv("CLOUDHOST_THEME_EVIDENCE_DIR")
    if folder:
        (Path(folder) / "theme-paint-timing.json").write_text(json.dumps({
            "browser": browser.version, "samples_ms": samples, "p95_ms": p95,
            "viewport": [1280, 800], "method": "input capture to two requestAnimationFrame callbacks",
            "limitation": "Synthetic click/headless local test; not a filmstrip or demonstration-device claim",
            "cpu_logical": os.cpu_count()
        }, indent=2))
    context.close()


@pytest.mark.parametrize("language", ("en", "zh"))
def test_theme_actual_text_and_control_contrast(browser, live_portal, language):
    context = browser.new_context(viewport={"width": 1280, "height": 800}, reduced_motion="reduce")
    page = context.new_page()
    page.add_init_script(f"localStorage.setItem('cloudhost.language','{language}')")
    page.route("**/api/services", lambda route: route.fulfill(json={"services": [
        {"name": "fixture-" + state, "state": state, "enabled": True, "detail": "Test state"}
        for state in ("running", "stopped", "failed", "starting", "unknown")
    ]}))
    records = []
    try:
        page.goto(live_portal.base + "/login")
        for view in ("login", "overview", "remote"):
            if view == "overview":
                sign_in(page, live_portal.base)
                page.locator("#services-title").click()
            elif view == "remote":
                mock_remote(page)
                navigate_view(page, "remote")
                page.locator(".remote-diagnostics").evaluate("n => n.open=true")
                page.locator("#peanut-address-entry").evaluate("n => n.open=true")
            for name in THEMES:
                pick(page, name)
                values = page.evaluate("""() => {
                  function rgba(s) {return (s.match(/[\\d.]+/g)||[]).map(Number);}
                  function mix(a,b){const x=a.length>3?a[3]:1;return [0,1,2].map(i=>a[i]*x+b[i]*(1-x));}
                  function bg(n) {
                    const chain=[];while(n){chain.unshift(n);n=n.parentElement;}
                    let c=[255,255,255];for(const e of chain){
                      const s=getComputedStyle(e);c=mix(rgba(s.backgroundColor),c);
                      if(e.matches('.btn-primary') && s.backgroundImage !== 'none') c=rgba(s.backgroundImage.match(/rgba?\\([^)]+\\)/)[0]);
                    }return c;
                  }
                  function lum(c){return c.map(x=>{x/=255;return x<=.04045?x/12.92:((x+.055)/1.055)**2.4;}).reduce((v,x,i)=>v+x*[.2126,.7152,.0722][i],0);}
                  function ratio(a,b){const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}
                  const items=[...document.querySelectorAll('body *')].filter(n=>n.getClientRects().length &&
                    getComputedStyle(n).visibility==='visible' && !n.closest('[aria-hidden="true"]') &&
                    [...n.childNodes].some(c=>c.nodeType===3&&c.textContent.trim()));
                  const results=items.map(n=>{
                    const s=getComputedStyle(n), background=bg(n), fg=mix(rgba(s.color),background);
                    return {text:n.textContent.trim().slice(0,90),selector:n.id||n.className,ratio:ratio(fg,background)};
                  });
                  const controls=[...document.querySelectorAll('input:enabled,.btn-outline:enabled')].filter(n=>n.getClientRects().length);
                  return {text:results,controls:controls.map(n=>({selector:n.id||n.className,
                    ratio:ratio(rgba(getComputedStyle(n).borderTopColor),bg(n))}))};
                }""")
                bad_text = [v for v in values["text"] if v["ratio"] < 4.5]
                bad_controls = [v for v in values["controls"] if v["ratio"] < 3]
                assert not bad_text, (name, view, bad_text)
                assert not bad_controls, (name, view, bad_controls)
                records.append({"theme": name, "view": view, "language": language, "samples": values})
        folder = os.getenv("CLOUDHOST_THEME_EVIDENCE_DIR")
        if folder:
            (Path(folder) / f"contrast-{language}.json").write_text(json.dumps(records, indent=2))
    finally:
        context.close()


@pytest.mark.parametrize("size", ((860, 800), (861, 800), (640, 400)))
def test_theme_breakpoint_zoom_long_content_and_offline_boundary(browser, live_portal, size):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    page = context.new_page()
    external = []
    def network(route):
        if not route.request.url.startswith(live_portal.base):
            external.append(route.request.url)
            route.abort()
        else:
            route.continue_()
    page.route("**/*", network)
    sign_in(page, live_portal.base)
    for name in THEMES:
        pick(page, name)
        page.locator(".brand-sub").evaluate("n => n.textContent='Long_account_name_'.repeat(12)")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # Desktop page zoom at 200% halves the CSS viewport; CSS zoom alone
        # would not exercise the media-query reflow of real browser page zoom.
        page.set_viewport_size({"width": max(320, size[0] // 2), "height": max(320, size[1] // 2)})
        assert page.locator("#theme-toggle").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#theme-toggle").click()
        assert page.locator("#theme-panel").is_visible()
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": size[0], "height": size[1]})
    assert not external
    context.close()


def test_theme_chromium_cold_warm_first_visible_frames(browser, live_portal):
    if browser.browser_type.name != "chromium":
        pytest.skip("Chromium CDP filmstrip requires Chromium; Firefox final-state checks are separate")
    import base64
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.add_init_script("""window.themeVisibleFrames=[];
      function themeFrameSample() {
        const body=document.body;
        if(body && body.querySelector('h1') && getComputedStyle(body).visibility === 'visible') {
          themeVisibleFrames.push({theme:document.documentElement.dataset.theme,
            ready:getComputedStyle(document.documentElement).getPropertyValue('--theme-ready').trim(),
            scheme:getComputedStyle(document.documentElement).colorScheme,
            bodyBackground:getComputedStyle(body).backgroundColor,time:performance.now()});
        }
        if(themeVisibleFrames.length<12)requestAnimationFrame(themeFrameSample);
      }requestAnimationFrame(themeFrameSample);""")
    page = context.new_page()
    sign_in(page, live_portal.base)
    records = []
    directory = Path(os.environ["CLOUDHOST_THEME_EVIDENCE_DIR"]) / "filmstrip" if os.getenv("CLOUDHOST_THEME_EVIDENCE_DIR") else None
    if directory:
        directory.mkdir(parents=True, exist_ok=True)
    for name in THEMES:
        page.evaluate("name => localStorage.setItem('cloudhost.theme',name)", name)
        for warm in (False, True):
            for path, view in (("/login?redirect=%2F", "login"), ("/", "portal")):
                page.goto("about:blank")
                client = context.new_cdp_session(page)
                frames = []
                client.on("Page.screencastFrame", lambda frame: (
                    frames.append(frame["data"]),
                    client.send("Page.screencastFrameAck", {"sessionId": frame["sessionId"]})
                ))
                client.send("Network.enable")
                client.send("Network.setCacheDisabled", {"cacheDisabled": not warm})
                if warm:
                    page.goto(live_portal.base + path)
                    page.goto("about:blank")
                client.send("Network.emulateNetworkConditions", {
                    "offline": False, "latency": 100, "downloadThroughput": 256000, "uploadThroughput": 256000
                })
                client.send("Page.startScreencast", {"format": "png", "everyNthFrame": 1})
                page.goto(live_portal.base + path)
                page.wait_for_function("window.themeVisibleFrames.length >= 12")
                page.wait_for_timeout(50)
                client.send("Page.stopScreencast")
                samples = page.evaluate("window.themeVisibleFrames")
                assert all(s["theme"] == name and s["ready"] == name for s in samples), (name, samples)
                assert all(s["scheme"] == ("light" if name == "swiss" else "dark") for s in samples)
                assert frames
                label = f"{name}-{view}-{'warm' if warm else 'cold'}"
                if directory:
                    for index, data in enumerate(frames):
                        (directory / f"{label}-{index:02d}.png").write_bytes(base64.b64decode(data))
                records.append({"label": label, "frames": len(frames), "visible_style_samples": samples})
                client.detach()
    if directory:
        (directory / "visible-frames.json").write_text(json.dumps({
            "browser": browser.version, "viewport": [1280, 800],
            "network": "100ms latency, 256000 bytes/s", "samples": records,
            "limitation": "Headless Chromium local portal; blank navigation frames are retained in filmstrip"
        }, indent=2))
    context.close()
