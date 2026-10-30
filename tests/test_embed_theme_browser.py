"""Embedded colors and continuity, including the pinned official applications."""
import pytest

from test_redesign_browser import (
    browser, live_portal, official_stack, sign_in, navigate_view,
)
from test_theme_browser import THEMES, pick, evidence


FRAME_IDS = {"files": "embed-frame", "site": "site-frame", "term": "term-frame"}

def assert_file_branding(page):
    page.wait_for_function("""() => {
      const f = document.querySelector('#embed-frame'), d = f.contentDocument, w = f.contentWindow;
      const root = getComputedStyle(document.documentElement);
      const rgb = value => value.match(/[\\d.]+/g).slice(0,3).map(Number);
      const color = key => {
        const node = document.createElement('span');
        node.style.color = root.getPropertyValue('--' + key);
        document.body.appendChild(node);
        const result = rgb(getComputedStyle(node).color);
        node.remove(); return result;
      };
      const logo = d.querySelector('header > img');
      if (!logo?.complete || !logo.naturalWidth) return false;
      const canvas = d.createElement('canvas'); canvas.width=64; canvas.height=64;
      const ctx = canvas.getContext('2d'); ctx.drawImage(logo,0,0,64,64);
      const pixel = (x,y) => [...ctx.getImageData(x,y,1,1).data].slice(0,3);
      const same = (a,b) => a.every((v,i)=>v===b[i]);
      const bar = d.querySelector('.credits .vue-simple-progress-bar');
      const track = d.querySelector('.credits .vue-simple-progress');
      return same(pixel(32,10),color('bg')) && same(pixel(2,32),color('accent')) &&
        same(rgb(w.getComputedStyle(bar).backgroundColor),color('accent')) &&
        same(rgb(w.getComputedStyle(track).backgroundColor),color('panel-2')) &&
        same(rgb(w.getComputedStyle(d.querySelector('.credits')).color),color('muted'));
    }""", timeout=5000)


def assert_palette(page, view, hidden=False):
    frame_id = FRAME_IDS[view]
    page.wait_for_function("""({id, view, hidden}) => {
      const frame = document.getElementById(id);
      const root = frame.contentDocument.documentElement;
      const parent = getComputedStyle(document.documentElement);
      const style = hidden ? root.style : frame.contentWindow.getComputedStyle(root);
      const token = key => parent.getPropertyValue('--' + key).trim();
      if (root.dataset.theme !== document.documentElement.dataset.theme ||
          style.colorScheme !== (root.dataset.theme === 'swiss' ? 'light' : 'dark')) return false;
      if (view === 'term') {
        const term = frame.contentWindow.term;
        return term && term.options.theme.background === token('bg') &&
          term.options.theme.foreground === token('text') &&
          term.options.theme.cursor === token('accent');
      }
      return style.getPropertyValue(view === 'files' ? '--background' : '--bg').trim() === token('bg') &&
        style.getPropertyValue(view === 'files' ? '--blue' : '--accent').trim() === token('accent');
    }""", arg={"id": frame_id, "view": view, "hidden": hidden})
    if hidden:
        return
    colors = page.evaluate("""id => {
      const f = document.getElementById(id), doc = f.contentDocument;
      const style = getComputedStyle(document.body);
      const node = id === 'term-frame' ? doc.querySelector('.xterm-viewport') : doc.body;
      return [style.backgroundColor, f.contentWindow.getComputedStyle(node).backgroundColor];
    }""", frame_id)
    assert colors[0] == colors[1]


@pytest.mark.parametrize("size", ((390, 844), (1280, 800)))
@pytest.mark.parametrize("view", ("files", "site", "term"))
def test_official_embedded_theme_colors_and_continuity(browser, official_stack, size, view):
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, reduced_motion="reduce")
    context.add_init_script("localStorage.setItem('cloudhost.theme', 'swiss')")
    page = context.new_page()
    sockets, errors = [], []
    page.on("websocket", lambda socket: sockets.append(socket))
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        sign_in(page, official_stack.base)
        navigate_view(page, view)
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        frame_id = FRAME_IDS[view]
        frame = page.locator("#" + frame_id).content_frame
        if view == "files":
            frame.get_by_text("browser-example.txt", exact=True).wait_for()
            frame.get_by_text("browser-example.txt", exact=True).click()
            capacity = frame.locator(".credits .vue-simple-progress-bar").get_attribute("style")
            logo_box = frame.locator("header > img").bounding_box()
        elif view == "site":
            frame.locator("#markdown-preview h1").wait_for()
            frame.locator("#markdown-input").click()
            frame.locator("#markdown-input").fill("# Retained theme draft")
            assert frame.locator("#markdown-input").input_value() == "# Retained theme draft"
        else:
            frame.locator(".xterm-helper-textarea").wait_for()
            frame.locator(".xterm-helper-textarea").press_sequentially(
                "printf 'THEME_%s_OK\\n' BEFORE\n", delay=10)
            page.wait_for_function("""() => {
              const b = document.querySelector('#term-frame').contentWindow.term.buffer.active;
              return Array.from({length:b.length}, (_,i)=>b.getLine(i).translateToString()).join('\\n').includes('THEME_BEFORE_OK');
            }""")
            frame.locator(".xterm-helper-textarea").press_sequentially("printf theme_draft", delay=10)
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        assert_palette(page, view)
        if view == "files":
            assert_file_branding(page)
            frame.locator("header > img").evaluate("""logo => {
              const replacement = logo.cloneNode();
              replacement.src = '/files/static/img/logo.svg';
              logo.replaceWith(replacement);
            }""")
            assert_file_branding(page)
        page.evaluate("""id => {
          window.originalFrame = document.getElementById(id);
          window.originalDocument = originalFrame.contentDocument;
          window.originalTerminal = originalFrame.contentWindow.term;
          window.originalTerminalSize = originalTerminal && [originalTerminal.cols, originalTerminal.rows];
          window.themeLoads = 0;
          originalFrame.addEventListener('load', () => themeLoads++);
        }""", frame_id)
        connections = len(sockets)
        navigations = []
        page.on("request", lambda request: navigations.append(request.url)
                if request.is_navigation_request() else None)
        for name in THEMES:
            pick(page, name)
            assert_palette(page, view)
            evidence(page, f"embedded-{name}-{view}-{size[0]}-official")
            assert page.evaluate("originalDocument === originalFrame.contentDocument && themeLoads === 0")
            if view == "files":
                assert_file_branding(page)
                assert frame.locator(".credits .vue-simple-progress-bar").get_attribute("style") == capacity
                assert frame.locator("header > img").bounding_box() == logo_box
                assert frame.locator('#listing [aria-selected="true"]').count() == 1
            elif view == "site":
                assert frame.locator("#markdown-input").input_value() == "# Retained theme draft"
            else:
                assert len(sockets) == connections == 1
                assert page.evaluate("""() => {
                  const term = originalFrame.contentWindow.term;
                  return term === originalTerminal &&
                    term.cols === originalTerminalSize[0] && term.rows === originalTerminalSize[1];
                }""")
        if view == "term":
            frame.locator("body").evaluate("""() => {
              term.options.theme = {background:'#071426', foreground:'#edf5fc'};
            }""")
            assert_palette(page, "term")
            frame.locator(".xterm-helper-textarea").press("Control+c")
            frame.locator(".xterm-helper-textarea").press_sequentially(
                "printf 'THEME_%s_OK\\n' AFTER\n", delay=10)
            page.wait_for_function("""() => {
              const b = document.querySelector('#term-frame').contentWindow.term.buffer.active;
              const text = Array.from({length:b.length}, (_,i)=>b.getLine(i).translateToString()).join('\\n');
              return text.includes('THEME_AFTER_OK') && text.includes('printf theme_draft');
            }""")
            assert len(sockets) == 1
        assert not navigations
        assert not errors
        navigate_view(page, "overview")
        pick(page, "bento")
        if view != "term":
            assert_palette(page, view, hidden=True)
        navigate_view(page, view)
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        assert_palette(page, view)
        if view == "term":
            page.wait_for_function("""() => {
              const term = document.querySelector('#term-frame').contentWindow.term;
              return term && term !== originalTerminal;
            }""")
            assert len(sockets) == 2
        else:
            assert page.evaluate("originalDocument === originalFrame.contentDocument && themeLoads === 0")
            if view == "files":
                assert_file_branding(page)
    finally:
        context.close()


def test_demo_site_retained_hidden_and_standalone_theme(browser, live_portal):
    context = browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "site")
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        editor = page.frame_locator("#site-frame").locator("#markdown-input")
        page.frame_locator("#site-frame").locator("#markdown-preview h1").wait_for()
        editor.click()
        editor.fill("# Hidden draft")
        navigate_view(page, "overview")
        for name in THEMES:
            pick(page, name)
            assert_palette(page, "site", hidden=True)
            assert editor.input_value() == "# Hidden draft"
        navigate_view(page, "site")
        assert_palette(page, "site")
        assert editor.input_value() == "# Hidden draft"
        page.goto(live_portal.base + "/site/")
        assert page.locator("html").get_attribute("data-theme") == "cyber"
        assert page.locator("body").evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(8, 11, 18)"
    finally:
        context.close()


def test_unsaved_theme_applies_to_new_and_existing_demo_frame(browser, live_portal):
    context = browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    try:
        sign_in(page, live_portal.base)
        page.evaluate("""() => {
          Storage.prototype.setItem = function(key) {
            if (key === 'cloudhost.theme') throw new Error('quota fixture');
          };
        }""")
        pick(page, "swiss")
        navigate_view(page, "site")
        page.wait_for_function("document.querySelector('#embed-hint').hidden")
        assert_palette(page, "site")
        pick(page, "bento")
        assert_palette(page, "site")
        assert page.locator("[data-theme-status]").is_visible()
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") is None
    finally:
        context.close()


@pytest.mark.parametrize("name", THEMES)
def test_standalone_demo_restores_saved_theme(browser, live_portal, name):
    context = browser.new_context()
    context.add_init_script(f"localStorage.setItem('cloudhost.theme', '{name}')")
    page = context.new_page()
    try:
        page.goto(live_portal.base + "/site/")
        assert page.locator("html").get_attribute("data-theme") == name
        assert page.locator("html").evaluate("n => getComputedStyle(n).colorScheme") == (
            "light" if name == "swiss" else "dark")
        colors = {"classic": "rgb(7, 20, 38)", "bento": "rgb(21, 23, 25)",
                  "swiss": "rgb(245, 246, 248)", "cyber": "rgb(8, 11, 18)"}
        assert page.locator("body").evaluate("n => getComputedStyle(n).backgroundColor") == colors[name]
    finally:
        context.close()


def test_custom_site_does_not_receive_theme_overrides(browser, live_portal):
    context = browser.new_context()
    page = context.new_page()
    page.route("**/site/", lambda route: route.fulfill(
        content_type="text/html",
        body='<style>body{background:rgb(123,45,67)}</style><input value="My own page">'))
    try:
        sign_in(page, live_portal.base)
        navigate_view(page, "site")
        body = page.frame_locator("#site-frame").locator("body")
        body.wait_for()
        for name in THEMES:
            pick(page, name)
            assert body.evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(123, 45, 67)"
            assert page.locator("#site-frame").evaluate("f => !f.contentDocument.documentElement.hasAttribute('data-theme')")
    finally:
        context.close()
