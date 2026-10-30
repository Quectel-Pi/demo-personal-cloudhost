import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = PROJECT_ROOT / "portal" / "static"
I18N_SOURCE = (STATIC_ROOT / "i18n.js").read_text(encoding="utf-8")


def _dictionary_keys(locale):
    start = I18N_SOURCE.index(f"    {locale}: {{")
    end = I18N_SOURCE.index("\n    }", start)
    return set(re.findall(r'^\s{6}"([^"]+)":', I18N_SOURCE[start:end], re.MULTILINE))


def test_translation_dictionaries_have_matching_keys_and_parameters():
    english = _dictionary_keys("en")
    chinese = _dictionary_keys("zh")

    assert english
    assert english == chinese
    for key in english:
        english_value = re.search(
            rf'^\s{{6}}"{re.escape(key)}": "(.*?)",?$',
            I18N_SOURCE[I18N_SOURCE.index("    en: {"):I18N_SOURCE.index("\n    zh: {")],
            re.MULTILINE,
        ).group(1)
        chinese_value = re.search(
            rf'^\s{{6}}"{re.escape(key)}": "(.*?)",?$',
            I18N_SOURCE[I18N_SOURCE.index("\n    zh: {"):I18N_SOURCE.index("\n    }\n  };", I18N_SOURCE.index("\n    zh: {"))],
            re.MULTILINE,
        ).group(1)
        assert set(re.findall(r"\{([a-zA-Z0-9_]+)\}", english_value)) == set(
            re.findall(r"\{([a-zA-Z0-9_]+)\}", chinese_value)
        ), key


def test_pages_use_only_defined_translation_keys_and_load_shared_module():
    keys = _dictionary_keys("en")
    pages = [
        STATIC_ROOT / "login.html",
        STATIC_ROOT / "index.html",
        PROJECT_ROOT / "deploy" / "site-index.html",
    ]
    for page in pages:
        source = page.read_text(encoding="utf-8")
        assert re.search(r'<html\b[^>]*\blang="en"', source)
        references = re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', source)
        references.extend(
            re.findall(
                r'["\']((?:common|login|dashboard|services|ssh|embed|site|remote)\.[A-Za-z0-9_]+)["\']',
                source,
            )
        )
        assert set(references) <= keys, page.name
        assert "i18n.js" in source, page.name
    dashboard = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    assert dashboard.index("i18n.js") < dashboard.index("app.js")
    assert "i18n.js?v={{VERSION}}&amp;ui=39" in dashboard
    assert "app.js?v={{VERSION}}&ui=39" in dashboard
    assert "styles.css?v={{VERSION}}&amp;ui=39" in dashboard
    assert "peanut.js?v={{VERSION}}&ui=39" in dashboard
    login = (STATIC_ROOT / "login.html").read_text(encoding="utf-8")
    assert "i18n.js?v={{VERSION}}&amp;ui=36" in login
    assert "styles.css?v={{VERSION}}&amp;ui=36" in login
    assert "局域网版" not in login
    assert "LAN edition" not in login


def test_login_intro_does_not_limit_workspace_to_local_network():
    assert '"experience.intro": "Manage files, host a website and run commands in your own real workspace."' in I18N_SOURCE
    assert '"experience.intro": "管理文件、托管网站、运行命令，操作你自己的真实工作空间。"' in I18N_SOURCE
    assert "A real workspace on your local network" not in I18N_SOURCE
    assert "在同一局域网" not in I18N_SOURCE


def test_language_switch_uses_en_zh_and_localizes_login_hint():
    login = (STATIC_ROOT / "login.html").read_text(encoding="utf-8")
    assert 'id="auth-hint">{{AUTH_HINT}}' in login
    assert 'data-language="en"' in login
    assert 'data-language="zh"' in login
    assert 'data-language="zh-CN"' not in login
    assert '"login.authHint": "Sign in with the account and password configured for this portal."' in I18N_SOURCE
    assert 'hint.textContent = language === "zh" ? authHintOriginal : translate("login.authHint");' in I18N_SOURCE


def test_dashboard_runtime_translation_keys_are_defined():
    keys = _dictionary_keys("en")
    app_source = "\n".join((STATIC_ROOT / name).read_text(encoding="utf-8")
                           for name in ("app.js", "peanut.js"))
    references = set(
        re.findall(
            r'["\']((?:flow|peanut|workbench|common|login|dashboard|services|ssh|embed|site|remote)\.[A-Za-z0-9_]+)["\']',
            app_source,
        )
    )
    assert references <= keys


def test_language_module_uses_scoped_preference_and_reports_missing_keys():
    assert 'var STORAGE_KEY = "cloudhost.language";' in I18N_SOURCE
    assert 'var VALID_LANGUAGES = ["en", "zh"];' in I18N_SOURCE
    assert 'if (value === "zh-CN")' in I18N_SOURCE
    assert 'window.localStorage' in I18N_SOURCE
    assert 'cloudhost:languagechange' in I18N_SOURCE
    assert "Missing Cloudhost translation:" in I18N_SOURCE
    assert "localStorage" not in (PROJECT_ROOT / "deploy" / "site-index.html").read_text(
        encoding="utf-8"
    )
