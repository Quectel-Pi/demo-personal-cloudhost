"""Optional browser checks: pip install playwright; playwright install chromium.

On Windows the installed Edge browser is used instead.
"""

import os
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def site_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        engine = os.environ.get("CLOUDHOST_BROWSER", "chromium")
        browser = getattr(playwright, engine).launch(
            channel="msedge" if os.name == "nt" and engine == "chromium" else None,
            executable_path=os.environ.get("CLOUDHOST_" + engine.upper()) or None,
        )
        context = browser.new_context(accept_downloads=True)
        site = (PROJECT_ROOT / "deploy" / "site-index.html").read_text(encoding="utf-8")
        assets = {
            "/static/" + name: (PROJECT_ROOT / "portal" / "static" / name).read_text(encoding="utf-8")
            for name in ("i18n.js", "theme-boot.js", "themes.css")
        }
        requests = []
        errors = []

        def serve(route):
            path = route.request.url.split("cloudhost.test", 1)[-1].split("?", 1)[0]
            requests.append(path)
            if path == "/site/":
                route.fulfill(body=site, content_type="text/html; charset=utf-8")
            elif path in assets:
                route.fulfill(
                    body=assets[path], content_type=(
                        "text/css; charset=utf-8" if path.endswith(".css")
                        else "application/javascript; charset=utf-8"
                    )
                )
            elif path == "/":
                route.fulfill(
                    body='<title>Portal shell</title><iframe src="/site/"></iframe>',
                    content_type="text/html",
                )
            else:
                route.fulfill(status=404, body="Not found")

        context.route("**/*", serve)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://cloudhost.test/site/")
        yield page, requests, errors
        context.close()
        browser.close()


def test_markdown_blocks_links_and_empty_state(site_page):
    page, requests, errors = site_page
    editor = page.locator("#markdown-input")
    editor.fill(
        "# Heading\n\nA paragraph\ncontinued.\n\n## Subheading\n\n"
        "- Alpha\n- Beta\n\n1. One\n2. Two\n\n> Quote\n\n"
        "```text\n# Literal heading\n[Literal](https://example.com)\n```\n\n"
        "[Local](#publish)\n[External](https://example.com)"
    )
    preview = page.locator("#markdown-preview")
    assert preview.locator("h1").inner_text() == "Heading"
    assert preview.locator("h2").inner_text() == "Subheading"
    assert preview.locator("p").first.inner_text() == "A paragraph continued."
    assert preview.locator("ul li").all_text_contents() == ["Alpha", "Beta"]
    assert preview.locator("ol li").all_text_contents() == ["One", "Two"]
    assert preview.locator("blockquote").inner_text() == "Quote"
    assert preview.locator("pre code").inner_text().startswith("# Literal heading\n")
    assert preview.locator("pre a, pre h1").count() == 0
    assert preview.locator("a").count() == 2
    assert preview.locator("a").first.get_attribute("href").endswith("/site/#publish")
    editor.fill("```\nUnclosed fence")
    assert preview.locator("pre code").inner_text() == "Unclosed fence"
    editor.fill("")
    assert preview.inner_text() == "Type some Markdown to see a preview."
    assert requests == ["/site/", "/static/theme-boot.js", "/static/i18n.js", "/static/themes.css"]
    assert errors == []


def test_markdown_does_not_execute_html_or_dangerous_links(site_page):
    page, _, errors = site_page
    payload = (
        '<script>window.siteInjected=true</script>\n\n'
        '<img src=x onerror="window.siteInjected=true">\n\n'
        "[Bad](javascript:alert)\n[Data](data:text/html,hello)\n"
        "[File](file:///etc/passwd)\n[Network](//example.com)\n"
        "[Malformed](http://[)\n[Good](./help.html)"
    )
    page.locator("#markdown-input").fill(payload)
    preview = page.locator("#markdown-preview")
    assert preview.locator("script, img, iframe").count() == 0
    assert "<script>" in preview.inner_text()
    assert preview.locator("a").count() == 1
    assert preview.locator("a").inner_text() == "Good"
    assert page.evaluate("window.siteInjected === undefined")
    assert errors == []


def test_language_changes_preserve_input_and_download_utf8(site_page, tmp_path):
    page, _, errors = site_page
    editor = page.locator("#markdown-input")
    editor.fill("# User content")
    assert page.locator("[data-language]").count() == 0
    language_control = page.context.new_page()
    language_control.goto("http://cloudhost.test/")
    language_control.evaluate("localStorage.setItem('cloudhost.language', 'zh')")
    page.wait_for_function("document.documentElement.lang === 'zh'")
    assert editor.input_value() == "# User content"
    assert page.locator("html").get_attribute("lang") == "zh"
    assert page.locator("#markdown-title").inner_text() == "Markdown 体验区"
    for kind, filename in [
        ("intro", "pi-m2-introduction.txt"),
        ("markdown", "pi-m2-example.md"),
    ]:
        with page.expect_download() as download_info:
            page.locator(f'[data-download="{kind}"]').click()
        download = download_info.value
        assert download.suggested_filename == filename
        destination = tmp_path / filename
        download.save_as(destination)
        content = destination.read_text(encoding="utf-8")
        assert "User content" not in content
        if kind == "intro":
            assert "个人云主机" in content
            assert "浏览器生成" in content
        else:
            assert content.startswith("# 我的 Pi M2 项目\n")
    page.locator("#reset-example").click()
    assert editor.input_value().startswith("# 我的 Pi M2 项目")
    editor.fill("")
    assert page.locator("#markdown-preview").inner_text() == "输入 Markdown，即可查看预览。"
    language_control.evaluate("localStorage.setItem('cloudhost.language', 'en')")
    page.wait_for_function("document.documentElement.lang === 'en'")
    assert page.locator("#markdown-preview").inner_text().startswith("Type some Markdown")
    page.reload()
    assert page.locator("html").get_attribute("lang") == "en"
    assert editor.input_value().startswith("# My Pi M2 project")
    language_control.close()
    assert errors == []


def test_download_failure_is_visible(site_page):
    page, _, errors = site_page
    page.evaluate("() => { URL.createObjectURL = () => { throw new Error('unavailable'); }; }")
    page.locator('[data-download="intro"]').click()
    assert "Could not start" in page.locator("#download-status").inner_text()
    assert errors == []


def test_mobile_navigation_keyboard_and_embedded_return(site_page):
    page, _, errors = site_page
    page.set_viewport_size({"width": 375, "height": 812})
    assert page.evaluate(
        "document.documentElement.scrollWidth <= window.innerWidth"
    )
    page.locator('nav a[href="#markdown"]').click()
    assert page.url.endswith("#markdown")
    page.locator("#reset-example").focus()
    page.keyboard.press("Enter")
    assert page.locator("#markdown-input").input_value().startswith("# My Pi M2")
    page.goto("http://cloudhost.test/")
    frame = page.frame_locator("iframe")
    frame.locator('nav a[href="#downloads"]').click()
    assert page.url == "http://cloudhost.test/"
    frame.locator('a[target="_top"]').click()
    page.wait_for_load_state()
    assert page.title() == "Portal shell"
    assert page.locator("iframe").count() == 1
    assert errors == []
