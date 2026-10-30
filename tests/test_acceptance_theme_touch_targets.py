"""Independent full-matrix hit target audit including navigation links."""
import itertools
import json

import pytest

from test_acceptance_theme_browser import (
    EVIDENCE, LANGUAGES, THEMES, VIEWPORTS, acceptance_browser,
    acceptance_freeze_guard, acceptance_portal, choose, install_fixtures,
    language, open_selector, sign_in,
)


@pytest.mark.parametrize("theme,locale,size", itertools.product(THEMES, LANGUAGES, VIEWPORTS))
def test_acceptance_theme_all_interactive_hit_targets(
        acceptance_browser, acceptance_portal, theme, locale, size):
    context = acceptance_browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    install_fixtures(page)
    snapshots = {}
    failures = []

    def sample(name):
        rows = page.locator(
            "button:visible,a.nav-tab:visible,a.link-card:visible,a.btn:visible,"
            "summary:visible,[role=radio]:visible,input:visible"
        ).evaluate_all("""nodes=>nodes.map(n=>{
          const r=n.getBoundingClientRect();return {tag:n.tagName,id:n.id,
            name:n.getAttribute('aria-label')||n.textContent.trim().slice(0,80),
            x:r.x,y:r.y,width:r.width,height:r.height};
        })""")
        snapshots[name] = rows
        failures.extend({"state": name, **row} for row in rows
                        if row["width"] < 44 or row["height"] < 44)
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")

    try:
        page.goto(acceptance_portal.base + "/login")
        language(page, locale)
        choose(page, theme)
        sample("login")
        open_selector(page)
        sample("login-selector")
        page.keyboard.press("Escape")
        sign_in(page, acceptance_portal.base)
        sample("overview")
        if size[0] >= 1280:
            assert page.locator(".topbar").bounding_box()["height"] <= 80
        if page.locator("#nav-toggle").is_visible():
            page.locator("#nav-toggle").click()
            sample("navigation-expanded")
            page.locator("#nav-toggle").click()
        page.locator("#tools-toggle").click()
        sample("account-expanded")
        page.locator("#tools-toggle").click()
        open_selector(page)
        sample("portal-selector")
        path = EVIDENCE / "touch-targets"
        path.mkdir(exist_ok=True)
        (path / f"{theme}-{locale}-{size[0]}x{size[1]}.json").write_text(json.dumps({
            "browser": acceptance_browser.version, "viewport": size,
            "snapshots": snapshots, "failures": failures,
            "scope": "CSS-pixel target boxes in controlled browser, not actual mobile device",
        }, ensure_ascii=False, indent=2) + "\n")
        assert not failures, ("Interactive hit targets below 44x44 CSS px", failures)
    finally:
        context.close()
