"""Small static contracts supplement, rather than replace, browser assertions."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "portal/static"


def test_theme_shared_wiring_cache_and_default():
    sources = [(STATIC / name).read_text() for name in ("index.html", "login.html")]
    sources.append((ROOT / "portal/app/routes_pages.py").read_text())
    for source in sources:
        for asset in ("theme-boot.js", "theme.js", "themes.css"):
            assert f"/static/{asset}" in source
        assert source.index("theme-boot.js") < source.index("i18n.js") < source.index("styles.css")
        assert "data-theme-control" in source
        assert "data-theme-status" in source
        assert 'type="button"' in source
        assert re.search(r"/static/themes\.css\?[^\" >]*ui=37", source)
    boot = (STATIC / "theme-boot.js").read_text()
    assert 'theme: "classic"' in boot
    assert 'value !== null' in boot
    assert "cloudhost.theme" in boot
    assert '["classic", "bento", "swiss", "cyber"]' in boot


def test_theme_module_is_not_a_business_lifecycle_or_remote_dependency():
    source = (STATIC / "theme.js").read_text() + (STATIC / "theme-boot.js").read_text()
    for forbidden in ("fetch(", "setView(", "setInterval(", "setTimeout(", ".innerHTML",
                      "iframe", "location.", "history.", "cloudhost:viewchange", "cloudhost.language"):
        assert forbidden not in source
    css = (STATIC / "themes.css").read_text()
    assert not re.search(r"@import|https?://|url\(", css)
    assert "filter:" not in css.replace("filter: none", "")
    assert "--theme-ready: swiss" in css
    assert "prefers-color-scheme" not in css
    assert "animation:" not in css


def test_theme_static_increment_fits_budget():
    # All newly introduced runtime assets plus growth of pre-existing resources.
    import subprocess
    growth = 0
    for path in STATIC.iterdir():
        if not path.is_file():
            continue
        result = subprocess.run(["git", "show", f"d5a7c80d4974e65172bfa17304a3be849347890a:{path.relative_to(ROOT.parents[1])}"],
                                capture_output=True)
        previous = len(result.stdout) if result.returncode == 0 else 0
        growth += max(0, path.stat().st_size - previous)
    assert growth <= 100 * 1024, growth
