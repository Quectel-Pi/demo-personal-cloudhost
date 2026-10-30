import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE_SOURCE = (PROJECT_ROOT / "deploy" / "site-index.html").read_text(encoding="utf-8")


def test_site_navigation_and_cards_have_real_targets():
    assert "data-language=" not in SITE_SOURCE
    ids = set(re.findall(r'\bid="([^"]+)"', SITE_SOURCE))
    targets = re.findall(r'href="#([^"]+)"', SITE_SOURCE)
    assert set(targets) <= ids
    assert {"about", "projects", "markdown", "publish", "downloads"} <= ids
    assert SITE_SOURCE.count('class="card"') == 3
    assert 'href="/" target="_top"' in SITE_SOURCE
    assert 'data-i18n="site.transport"' in SITE_SOURCE
    assert 'data-i18n="experience.boundary"' not in SITE_SOURCE
    assert "over HTTP on a trusted local network" not in SITE_SOURCE


def test_site_uses_only_local_resources_and_safe_dom_rendering():
    sources = re.findall(r'<script[^>]+src="([^"]+)"', SITE_SOURCE)
    assert sources == ["/static/theme-boot.js?ui=36", "/static/i18n.js?ui=12"]
    assert 'data-cloudhost-site' in SITE_SOURCE
    assert '/static/themes.css?ui=37' in SITE_SOURCE
    assert "innerHTML" not in SITE_SOURCE
    assert "createTextNode" in SITE_SOURCE
    assert "textContent" in SITE_SOURCE
    assert "fetch(" not in SITE_SOURCE
    assert "localStorage" not in SITE_SOURCE
    assert "cloudhost:languagechange" in SITE_SOURCE


def test_public_gateway_uses_same_site_read_group_as_lan_caddy():
    unit = (PROJECT_ROOT / "deploy" / "cloudhost-remote-gateway.service.tmpl").read_text()
    installer = (PROJECT_ROOT / "install.sh").read_text()
    assert "SupplementaryGroups=caddy" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "g:caddy:--x" in installer
    assert "g:caddy:rX" in installer


def test_site_downloads_are_named_utf8_samples():
    assert 'data-download="intro"' in SITE_SOURCE
    assert 'data-download="markdown"' in SITE_SOURCE
    assert "pi-m2-introduction.txt" in SITE_SOURCE
    assert "pi-m2-example.md" in SITE_SOURCE
    assert "text/plain;charset=utf-8" in SITE_SOURCE
    assert "URL.revokeObjectURL" in SITE_SOURCE


@pytest.fixture
def deploy_site(tmp_path):
    bash = shutil.which("bash")
    if os.name == "nt":
        candidate = (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Git" / "bin" / "bash.exe"
        )
        bash = str(candidate) if candidate.is_file() else None
    if not bash:
        pytest.skip("requires Bash to execute the installer's site deployment")
    source = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
    start = source.index('if [[ ${KEEP_SITE} -eq 1 ]]')
    block = source[start:source.index("\nfi", start) + len("\nfi")]
    site_dir = tmp_path / "site"
    deploy_dir = tmp_path / "deploy"
    site_dir.mkdir()
    deploy_dir.mkdir()
    template = deploy_dir / "site-index.html"
    template.write_text("demo template", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        SITE_DIR=site_dir.as_posix(), DEPLOY_SRC=deploy_dir.as_posix(),
        SITE_BACKUP_ROOT=(tmp_path / "private-backups").as_posix(),
        RUN_USER="unused-test-owner",
        SITE_TEST_WINDOWS="1" if os.name == "nt" else "0",
    )

    def run(keep=False, fail_backup=False):
        environment["KEEP_SITE"] = "1" if keep else "0"
        # Exercise real filesystem operations without root or POSIX ACLs on Windows.
        prelude = """
set -e
log() { printf '%s\\n' "$*"; }
ok() { log "$@"; }
die() { log "$@"; exit 1; }
install() {
    if [[ "$SITE_TEST_WINDOWS" == 1 ]]; then
        if [[ "$1" == -d ]]; then
            command mkdir -p "${@: -1}"
        else
            command cp "${@: -2}"
        fi
        return
    fi
    local args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -o|-g) shift 2 ;;
            *) args+=("$1"); shift ;;
        esac
    done
    command install "${args[@]}"
}
"""
        if fail_backup:
            prelude += 'cp() { return 1; }\n'
        return subprocess.run(
            [bash, "-c", prelude + block],
            env=environment, capture_output=True, text=True, check=True,
        )

    return run, site_dir, template, tmp_path / "private-backups"


def test_default_site_replaces_old_homepage_and_preserves_backup(deploy_site):
    run, site_dir, template, backups = deploy_site
    homepage = site_dir / "index.html"
    homepage.write_text("hello site", encoding="utf-8")
    resource = site_dir / "custom.css"
    resource.write_text("body {}", encoding="utf-8")
    result = run()
    assert homepage.read_bytes() == template.read_bytes()
    saved = list(backups.glob("site.*/index.html"))
    assert len(saved) == 1
    assert saved[0].read_text(encoding="utf-8") == "hello site"
    assert "Backed up" in result.stdout
    assert resource.read_text(encoding="utf-8") == "body {}"
    run()
    assert list(backups.glob("site.*/index.html")) == saved
    homepage.write_text("second custom page", encoding="utf-8")
    run()
    assert len(list(backups.glob("site.*/index.html"))) == 2
    assert saved[0].read_text(encoding="utf-8") == "hello site"


@pytest.mark.parametrize("with_resource", [False, True])
def test_default_site_installs_when_homepage_missing(deploy_site, with_resource):
    run, site_dir, template, backups = deploy_site
    if with_resource:
        (site_dir / ".keep").write_text("keep", encoding="utf-8")
    run()
    assert (site_dir / "index.html").read_bytes() == template.read_bytes()
    assert not backups.exists()


@pytest.mark.parametrize("with_homepage", [False, True])
def test_keep_site_leaves_directory_unchanged(deploy_site, with_homepage):
    run, site_dir, _, backups = deploy_site
    homepage = site_dir / "index.html"
    if with_homepage:
        homepage.write_text("custom", encoding="utf-8")
    run(keep=True)
    assert homepage.exists() == with_homepage
    if with_homepage:
        assert homepage.read_text(encoding="utf-8") == "custom"
    assert not backups.exists()


def test_missing_template_fails_without_changing_homepage(deploy_site):
    run, site_dir, template, _ = deploy_site
    homepage = site_dir / "index.html"
    homepage.write_text("custom", encoding="utf-8")
    template.unlink()
    with pytest.raises(subprocess.CalledProcessError, match="non-zero"):
        run()
    assert homepage.read_text(encoding="utf-8") == "custom"


def test_backup_failure_aborts_before_replacement(deploy_site):
    run, site_dir, _, _ = deploy_site
    homepage = site_dir / "index.html"
    homepage.write_text("custom", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError, match="non-zero"):
        run(fail_backup=True)
    assert homepage.read_text(encoding="utf-8") == "custom"
