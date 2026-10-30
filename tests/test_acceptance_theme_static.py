"""Independent wiring/resource checks; these never stand in for browser evidence."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from test_acceptance_theme_browser import (
    EVIDENCE, PROJECT, acceptance_freeze_guard,
)


@pytest.mark.parametrize("template", ("index.html", "login.html"))
def test_acceptance_theme_critical_assets_are_local(template):
    html = (PROJECT / "portal/static" / template).read_text()
    resources = re.findall(
        r"<(?:script|link)\b[^>]*(?:src|href)=[\"']([^\"']+)", html, re.I
    )
    assert resources, "Expected local CSS/JS wiring"
    assert all(resource.startswith("/static/") for resource in resources), resources
    for resource in resources:
        path = PROJECT / "portal" / resource.split("?", 1)[0].lstrip("/")
        assert path.is_file(), (template, resource)
    assert 'name="viewport"' in html
    # Exact file names/layout are implementation choices. Verify stylesheet coverage
    # using resource references, leaving four visual differences to real review.
    assert any(".css" in item for item in resources)
    assert any(".js" in item for item in resources)


def test_acceptance_theme_no_new_font_or_external_stylesheet_import():
    suspicious = []
    for path in (PROJECT / "portal/static").rglob("*.css"):
        text = path.read_text()
        if re.search(r"@import\s+(?:url\()?['\"]?(?:https?:)?//", text, re.I):
            suspicious.append(str(path.relative_to(PROJECT)))
    assert not suspicious, ("Portal CSS must not require remote stylesheet imports", suspicious)


def test_acceptance_theme_repair_cache_references():
    references = {}
    for name in ("portal/static/index.html", "portal/static/login.html",
                 "portal/app/routes_pages.py"):
        references[name] = re.findall(
            r"/static/themes\.css\?[^\"'\s<>]+", (PROJECT / name).read_text())
        assert len(references[name]) == 1, (name, references[name])
        assert parse_qs(urlsplit(unescape(references[name][0])).query).get("ui") == ["37"], \
            (name, references[name])
    (EVIDENCE / "repair-cache-references.json").write_text(
        json.dumps(references, indent=2) + "\n")


def test_acceptance_theme_implementation_source_inventory():
    paths = sorted(
        p for p in (PROJECT / "portal/static").rglob("*")
        if p.is_file() and p.suffix in (".js", ".css", ".html")
    )
    inventory = []
    for path in paths:
        content = path.read_text()
        inventory.append({
            "path": str(path.relative_to(PROJECT)),
            "bytes": len(path.read_bytes()),
            "external_references": re.findall(r"https?://[^\s\"'<>`]+", content),
            "setInterval_count": len(re.findall(r"\bsetInterval\s*\(", content)),
            "setTimeout_count": len(re.findall(r"\bsetTimeout\s*\(", content)),
            "requestAnimationFrame_count": len(re.findall(r"\brequestAnimationFrame\s*\(", content)),
            "iframe_references": len(re.findall(r"\biframe\b", content, re.I)),
        })
    (EVIDENCE / "theme-static-review-inventory.json").write_text(json.dumps({
        "purpose": "Manual source review index, NOT a behavioral/visual pass",
        "files": inventory,
    }, ensure_ascii=False, indent=2) + "\n")
    assert inventory
