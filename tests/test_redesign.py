"""Static contracts complement, but do not replace, real-client browser checks."""

import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))
import filebrowser_branding


def luminance(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))


def contrast(one, two):
    high, low = sorted((luminance(one), luminance(two)), reverse=True)
    return (high + .05) / (low + .05)


@pytest.mark.parametrize("foreground", ["#edf5fc", "#a6b8cb", "#66d9ef", "#ff8b88", "#34d399", "#f0b429"])
@pytest.mark.parametrize("background", ["#071426", "#10263e", "#152e48"])
def test_text_palette_exceeds_wcag_aa(foreground, background):
    assert contrast(foreground, background) >= 4.5


def test_terminal_palette_and_official_options():
    source = (PROJECT / "deploy" / "cloudhost-ttyd.service.tmpl").read_text(encoding="utf-8")
    theme = json.loads(re.search(r"'theme=(\{.*\})'", source).group(1))
    for name, color in theme.items():
        if name not in ("background", "cursorAccent", "selectionBackground"):
            assert contrast(color, theme["background"]) >= 4.5, name
    assert "cursorBlink=false" in source
    assert "lineHeight=1.25" in source
    assert "--interface 127.0.0.1" in source
    assert "/bin/bash -l" in source


def test_native_offline_views_and_official_extensions_only():
    source = (PROJECT / "portal" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'terminal.options.theme = ' in source
    assert 'hasAttribute("data-cloudhost-site")' in source
    assert 'terminalThemeObserver.disconnect()' in source
    assert 'createElement("script")' not in source
    assert 'name === "term" && !ttydOn' in source
    assert 'function releaseTerminal()' in source
    assert 'showEmbedHint(name === "term"' not in source
    css = (PROJECT / "deploy" / "filebrowser" / "custom.css").read_text(encoding="utf-8")
    for token in ("--surfacePrimary", "--surfaceSecondary", "--textPrimary", "--textSecondary", ":focus-visible", "prefers-reduced-motion"):
        assert token in css
    assert "url(" not in css
    portal = (PROJECT / "portal" / "static" / "index.html").read_text(encoding="utf-8")
    assert portal.index("product-hero") < portal.index("purpose-panel") < portal.index('aria-labelledby="status-title"')


def test_branding_is_version_gated_and_never_resets_database(monkeypatch, tmp_path):
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdout="File Browser v2.31.2/129a4fd3")
    monkeypatch.setattr(filebrowser_branding.subprocess, "run", run)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "custom.css").write_text(":root {}", encoding="utf-8")
    database = tmp_path / "existing.db"
    database.write_bytes(b"unchanged-user-database")
    for _ in range(2):
        filebrowser_branding.apply_branding(Path("filebrowser"), Path("config.json"), database, assets)
    assert commands[1] == commands[3]
    assert "init" not in commands[1]
    assert "users" not in commands[1]
    assert "--branding.files" in commands[1]
    assert database.read_bytes() == b"unchanged-user-database"
    monkeypatch.setattr(filebrowser_branding.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="File Browser v2.32.0"))
    with pytest.raises(ValueError, match="not verified"):
        filebrowser_branding.apply_branding(Path("filebrowser"), Path("config.json"), database, assets)


def test_login_product_message_has_no_translation_residue():
    login = (PROJECT / "portal" / "static" / "login.html").read_text(encoding="utf-8")
    headline = re.search(r'<h2[^>]+data-i18n="experience.headline"[^>]*>(.*?)</h2>', login).group(1)
    assert "<" not in headline
    assert 'data-auth-mode="{{AUTH_MODE}}"' in login
