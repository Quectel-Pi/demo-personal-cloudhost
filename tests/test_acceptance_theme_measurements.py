"""Raw independent theme measurements, gated by the coordinator freeze.

Successful evidence capture is not itself proof of visual review, real-device
compatibility, Pi performance, first-frame correctness or screen-reader output.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from test_acceptance_theme_browser import (
    ALIASES, EVIDENCE, LANGUAGES, PROJECT, THEMES,
    acceptance_browser, acceptance_freeze_guard, acceptance_portal,
    assert_layout, choose, install_fixtures, language, open_selector,
    option, record, sign_in, theme_button,
)


CONTRAST = r"""() => {
  const rgba=s=>{
    const m=s.match(/[\d.]+/g);
    return m?[+m[0],+m[1],+m[2],m[3]===undefined?1:+m[3]]:[0,0,0,0];
  };
  const blend=(f,b)=>[0,1,2].map(i=>f[i]*f[3]+b[i]*(1-f[3])).concat(1);
  const luminance=c=>c.slice(0,3).map(v=>{
    v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);
  }).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
  const ratio=(a,b)=>{
    const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);
  };
  const background=n=>{
    let nodes=[],unresolved=[];
    for(let p=n;p;p=p.parentElement) {
      const s=getComputedStyle(p);nodes.push(rgba(s.backgroundColor));
      if(s.backgroundImage!=='none')unresolved.push({tag:p.tagName,image:s.backgroundImage});
      if(+s.opacity!==1)unresolved.push({tag:p.tagName,opacityGroup:s.opacity});
    }
    return {color:nodes.reverse().reduce((b,f)=>blend(f,b),[255,255,255,1]),unresolved};
  };
  let samples=[];
  for(const n of document.querySelectorAll('body *')) {
    const s=getComputedStyle(n),r=n.getBoundingClientRect();
    if(!r.width||!r.height||s.visibility!=='visible'||n.closest('[hidden],script,style'))continue;
    const text=Array.from(n.childNodes).filter(c=>c.nodeType===Node.TEXT_NODE)
      .map(c=>c.textContent).join('').trim();
    const value=n.matches('input,textarea')?n.value:'';
    if(!text&&!value)continue;
    const bg=background(n);
    let opacity=1;
    for(let p=n;p;p=p.parentElement) opacity*=+getComputedStyle(p).opacity;
    const fg=rgba(s.color);fg[3]*=opacity;
    samples.push({tag:n.tagName,id:n.id,text:(text||value).slice(0,120),
      foreground:fg,background:bg.color,ratio:ratio(blend(fg,bg.color),bg.color),
      opacity,unresolvedBackground:bg.unresolved,fontSize:s.fontSize,
      rect:{x:r.x,y:r.y,width:r.width,height:r.height}});
  }
  return samples;
}"""


@pytest.mark.parametrize("theme,locale", itertools.product(THEMES, LANGUAGES))
def test_acceptance_theme_actual_contrast_samples(
        acceptance_browser, acceptance_portal, theme, locale):
    context = acceptance_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    install_fixtures(page, "warning")
    samples = {}
    try:
        page.goto(acceptance_portal.base + "/login")
        language(page, locale)
        choose(page, theme)
        samples["login"] = page.evaluate(CONTRAST)
        open_selector(page)
        samples["login-selector"] = page.evaluate(CONTRAST)
        page.keyboard.press("Escape")
        sign_in(page, acceptance_portal.base)
        page.locator("#overview-services > details > summary").click()
        samples["overview-services"] = page.evaluate(CONTRAST)
        open_selector(page)
        samples["portal-selector"] = page.evaluate(CONTRAST)
        focus_samples = []
        for target in THEMES:
            item = option(page, target)
            item.evaluate("n=>n.blur()")
            unfocused = item.evaluate("""n=>{
              const s=getComputedStyle(n);return {
                outline:s.outline,borderColor:s.borderColor,boxShadow:s.boxShadow,
                background:s.backgroundColor
              };
            }""")
            item.focus()
            page.keyboard.press("ArrowRight")
            page.keyboard.press("ArrowLeft")
            focused = item.evaluate("""n=>{
              const s=getComputedStyle(n),r=n.getBoundingClientRect();
              const rgba=v=>{
                const m=v.match(/[\\d.]+/g);
                return m?[+m[0],+m[1],+m[2],m[3]===undefined?1:+m[3]]:[0,0,0,0];
              };
              const blend=(f,b)=>[0,1,2].map(i=>f[i]*f[3]+b[i]*(1-f[3])).concat(1);
              const lum=c=>c.slice(0,3).map(v=>{
                v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);
              }).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
              const surfaces=[];let needsManual=false;
              for(let p=n.parentElement;p;p=p.parentElement){
                const ps=getComputedStyle(p);surfaces.push(rgba(ps.backgroundColor));
                if(ps.backgroundImage!=='none'||+ps.opacity!==1)needsManual=true;
              }
              const adjacent=surfaces.reverse().reduce((b,f)=>blend(f,b),[255,255,255,1]);
              const stroke=blend(rgba(s.outlineColor),adjacent);
              const a=lum(adjacent),b=lum(stroke);
              return {text:n.textContent,outline:s.outline,outlineColor:s.outlineColor,
                outlineWidth:s.outlineWidth,outlineOffset:s.outlineOffset,
                borderColor:s.borderColor,boxShadow:s.boxShadow,background:s.backgroundColor,
                parentBackground:getComputedStyle(n.parentElement).backgroundColor,
                adjacentSolid:adjacent,outlineRatio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05),
                needsManualBackground:needsManual,
                rect:{x:r.x,y:r.y,width:r.width,height:r.height}};
            }""")
            focused["unfocused"] = unfocused
            focus_samples.append(focused)
        evidence = EVIDENCE / "candidate-browser"
        evidence.mkdir(parents=True, exist_ok=True)
        payload = {
            "theme": theme, "language": locale, "browser": acceptance_browser.version,
            "method": "Computed foreground/ancestor alpha-composited solid backgrounds",
            "limitation": "Gradients/images require pixel-level/manual follow-up; focus outlines "
            "and essential boundary adjacencies require explicit 3:1 pair evaluation",
            "samples": samples, "focus_samples": focus_samples,
            "manual_background_sample_count": sum(
                bool(s["unresolvedBackground"]) for rows in samples.values() for s in rows
            ),
        }
        (evidence / f"{theme}-{locale}-contrast-raw.json").write_text(json.dumps(
            payload, ensure_ascii=False, indent=2) + "\n")
        failures = [
            {"view": view, **s} for view, rows in samples.items() for s in rows
            if not s["unresolvedBackground"] and s["ratio"] < 4.5
        ]
        assert not failures, ("PRD text contrast <4.5:1 on resolved solid backgrounds", failures)
        assert all(
            float(s["outlineWidth"].removesuffix("px")) > 0 or any(
                s[key] != s["unfocused"][key]
                for key in ("outline", "borderColor", "boxShadow", "background")
            ) for s in focus_samples
        ), ("No computed focus-indicator change; visible 3:1 review still required", focus_samples)
        failed_outlines = [
            s for s in focus_samples if not s["needsManualBackground"]
            and float(s["outlineWidth"].removesuffix("px")) > 0 and s["outlineRatio"] < 3
        ]
        assert not failed_outlines, ("Focus outline below 3:1 on resolved adjacent background",
                                     failed_outlines)
        record(page, f"{theme}-{locale}-focus-outline-samples")
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("kind", ("no-javascript", "block-theme-module"))
def test_acceptance_theme_resource_degradation(
        acceptance_browser, acceptance_portal, theme, kind):
    context = acceptance_browser.new_context(java_script_enabled=kind != "no-javascript")
    if kind != "no-javascript":
        context.add_init_script("localStorage.setItem('cloudhost.theme'," + json.dumps(theme) + ")")
    page = context.new_page()
    blocked = []
    if kind == "block-theme-module":
        def block(route):
            blocked.append(route.request.url)
            route.abort()
        page.route(re.compile(r".*/static/[^/?]*theme[^/?]*\.js(?:\?.*)?$"), block)
    try:
        page.goto(acceptance_portal.base + "/login", wait_until="load")
        assert page.locator("body").is_visible(), "Theme/i18n failure must not hide body forever"
        assert page.locator("#username").is_visible()
        assert page.locator("#password").is_visible()
        assert page.locator("#login-submit").is_visible()
        explanation = page.locator("body").inner_text()
        assert re.search(r"unavailable|disabled|enable JavaScript|不可用|禁用|启用.*JavaScript",
                         explanation, re.I), "Visible unavailable/degradation explanation required"
        if kind == "block-theme-module":
            assert blocked, "Fault injection did not hit an actual theme resource; adapt injector, not expectation"
        record(page, f"{theme}-{kind}", {"blocked_resources": blocked})
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_stylesheet_failure_is_not_saved_as_success(
        acceptance_browser, acceptance_portal, theme):
    context = acceptance_browser.new_context()
    context.add_init_script("localStorage.setItem('cloudhost.theme'," + json.dumps(theme) + ")")
    page = context.new_page()
    blocked = []

    def block(route):
        blocked.append(route.request.url)
        route.abort()

    page.route("**/static/themes.css?*", block)
    try:
        page.goto(acceptance_portal.base + "/login", wait_until="load")
        assert blocked, "Stylesheet fault must hit the delivered resource"
        assert page.locator("body").is_visible()
        assert page.locator("#username").is_visible()
        assert page.locator("#login-submit").is_visible()
        open_selector(page)
        option(page, "bento" if theme != "bento" else "swiss").click()
        assert page.locator("html").get_attribute("data-theme") == "classic"
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == theme
        assert re.search(r"could not|fail|无法|失败", page.locator("[data-theme-status]").inner_text(), re.I)
        record(page, theme + "-theme-stylesheet-blocked", {"blocked": blocked})
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_offline_dependency_boundary(
        acceptance_browser, acceptance_portal, theme):
    context = acceptance_browser.new_context()
    external = []

    def local_only(route):
        url = route.request.url
        if urlsplit(url).scheme in ("http", "https") and urlsplit(url).hostname not in (
                "127.0.0.1", "localhost"):
            external.append(url)
            route.abort()
        else:
            route.continue_()

    context.route("**/*", local_only)
    page = context.new_page()
    install_fixtures(page)
    try:
        page.goto(acceptance_portal.base + "/login")
        choose(page, theme)
        assert_layout(page)
        sign_in(page, acceptance_portal.base)
        choose(page, theme)
        assert_layout(page)
        open_selector(page)
        record(page, f"{theme}-internet-blocked-portal",
               {"external_requests": external, "boundary": "Portal only; iframe fixtures excluded"})
        assert not external, "Portal themes must not require/request remote resources"
    finally:
        context.close()


@pytest.mark.parametrize("theme,path", itertools.product(THEMES, ("/files/", "/site/", "/term/")))
def test_acceptance_theme_portal_gateway_error_not_disguised(
        acceptance_browser, acceptance_portal, theme, path):
    context = acceptance_browser.new_context()
    page = context.new_page()
    try:
        page.goto(acceptance_portal.base + "/login")
        choose(page, theme)
        response = page.goto(acceptance_portal.base + path)
        assert response.status == 404, "Portal fallback is still an error, never success"
        assert response.headers.get("x-cloudhost-gateway-fallback") == "true"
        assert page.evaluate("localStorage.getItem('cloudhost.theme')") == theme
        assert page.locator("html").evaluate(
            "n=>getComputedStyle(n).colorScheme") == ("light" if theme == "swiss" else "dark")
        assert page.locator("body").is_visible()
        assert re.search(r"unavailable|missing|not.*route|不可用|未.*转发|网关",
                         page.locator("body").inner_text(), re.I)
        stylesheet = page.locator('link[href*="/static/themes.css"]')
        assert stylesheet.get_attribute("href") == "/static/themes.css?ui=37"
        css = context.request.get(acceptance_portal.base + stylesheet.get_attribute("href"))
        actual_sha = hashlib.sha256(css.body()).hexdigest()
        assert css.status == 200
        assert actual_sha == hashlib.sha256(
            (PROJECT / "portal/static/themes.css").read_bytes()).hexdigest()
        record(page, f"{theme}-portal-fallback-{path.strip('/')}", {
            "actual_stylesheet_url": css.url, "actual_stylesheet_sha256": actual_sha,
        })
    finally:
        context.close()


def test_acceptance_theme_resource_increment_uncompressed():
    baseline = "d5a7c80d4974e65172bfa17304a3be849347890a"
    prefix = "demos/35-cloudhost/portal/static/"
    previous_names = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", baseline, "--", prefix],
        cwd=PROJECT.parents[1], text=True,
    ).splitlines()
    old_sizes = {}
    for path in previous_names:
        contents = subprocess.check_output(
            ["git", "show", baseline + ":" + path], cwd=PROJECT.parents[1]
        )
        old_sizes[path.removeprefix(prefix)] = len(contents)
    new_sizes = {
        str(p.relative_to(PROJECT / "portal/static")): p.stat().st_size
        for p in (PROJECT / "portal/static").rglob("*") if p.is_file()
    }
    # CSS/JS and newly added dedicated non-HTML assets; shared files counted once.
    names = {
        name for name in new_sizes.keys() | old_sizes.keys()
        if name.endswith((".css", ".js")) or (
            name not in old_sizes and not name.endswith(".html")
        )
    }
    deltas = {
        name: {"before_bytes": old_sizes.get(name, 0), "after_bytes": new_sizes.get(name, 0),
               "delta_bytes": new_sizes.get(name, 0) - old_sizes.get(name, 0)}
        for name in sorted(names)
    }
    increment = sum(v["delta_bytes"] for v in deltas.values())
    fonts = [
        name for name in new_sizes if name not in old_sizes
        and name.lower().endswith((".woff", ".woff2", ".ttf", ".otf"))
    ]
    payload = {"baseline": baseline, "unit": "uncompressed bytes", "files": deltas,
               "increment_bytes": increment, "limit_bytes": 100 * 1024, "new_fonts": fonts}
    (EVIDENCE / "theme-resource-byte-deltas.json").write_text(json.dumps(payload, indent=2) + "\n")
    assert not fonts, ("No new font packages allowed", fonts)
    assert increment <= 100 * 1024, payload


def test_acceptance_theme_36_raw_paint_samples(acceptance_browser, acceptance_portal):
    if acceptance_browser.browser_type.name != "chromium":
        pytest.skip("BLOCKED: specified desktop Chromium measurement requires Chromium")
    context = acceptance_browser.new_context(viewport={"width": 1280, "height": 800})
    context.add_init_script("""if(window===top) {
      window.addEventListener('click',event=>{
        if(!window.__acceptanceMeasureNext ||
            !event.target.closest('[role=radio],input[type=radio]'))return;
        window.__acceptanceMeasureNext=false;
        const start=performance.now();
        requestAnimationFrame(()=>requestAnimationFrame(()=>{
          const s=getComputedStyle(document.documentElement);
          window.__acceptancePaintSample={
            start,end:performance.now(),background:s.backgroundColor,
            colorScheme:s.colorScheme,
            selected:document.querySelector('[role=radio][aria-checked=true]')?.textContent
          };
        }));
      },true);
    }""")
    page = context.new_page()
    install_fixtures(page)
    try:
        sign_in(page, acceptance_portal.base)
        raw = []
        for iteration in range(3):
            for source, target in itertools.permutations(THEMES, 2):
                choose(page, source)
                open_selector(page)
                item = option(page, target)
                page.evaluate("""() => {
                  window.__acceptancePaintSample=null;window.__acceptanceMeasureNext=true;
                }""")
                item.click()
                page.wait_for_function("!!window.__acceptancePaintSample")
                sample = page.evaluate("window.__acceptancePaintSample")
                assert page.evaluate("localStorage.getItem('cloudhost.theme')") == target
                sample.update({"iteration": iteration, "from": source, "to": target,
                               "elapsed_ms": sample["end"] - sample["start"]})
                assert sample["colorScheme"] == ("light" if target == "swiss" else "dark")
                raw.append(sample)
        ordered = sorted(s["elapsed_ms"] for s in raw)
        p95 = ordered[math.ceil(.95 * len(ordered)) - 1]
        (EVIDENCE / "theme-paint-performance-raw.json").write_text(json.dumps({
            "browser": acceptance_browser.version,
            "viewport": {"width": 1280, "height": 800},
            "client": {"cpu_logical": os.cpu_count()},
            "method": "Capture click timestamp; second rAF after applied styles. "
            "Presentation bound, not hardware display scanout; correctness needs visual matrix review.",
            "samples": raw, "count": len(raw), "p95_ms": p95, "threshold_ms": 300,
        }, indent=2) + "\n")
        assert len(raw) >= 30
        assert p95 <= 300
    finally:
        context.close()


@pytest.mark.parametrize("theme,page_kind", itertools.product(THEMES, ("login", "portal", "logout")))
def test_acceptance_theme_cold_hot_slow_filmstrip(
        acceptance_browser, acceptance_portal, theme, page_kind):
    if acceptance_browser.browser_type.name != "chromium":
        pytest.skip("BLOCKED: Chromium CDP raw filmstrip unavailable on this engine")
    context = acceptance_browser.new_context(viewport={"width": 1280, "height": 800})
    context.add_init_script("""
      localStorage.setItem('cloudhost.theme', THEME);
      if(window===top) {
        window.__acceptanceFirstVisibleSamples=[];
        const inspect=()=>{
          const root=document.documentElement,body=document.body;
          if(root && body && body.innerText.trim() && getComputedStyle(body).visibility==='visible'
              && getComputedStyle(body).display!=='none' && +getComputedStyle(body).opacity>0) {
            const s=getComputedStyle(root);
            window.__acceptanceFirstVisibleSamples.push({
              time:performance.now(),background:s.backgroundColor,colorScheme:s.colorScheme,
              bodyBackground:getComputedStyle(body).backgroundColor,
              bodyBackgroundImage:getComputedStyle(body).backgroundImage,
              nativeControlScheme:getComputedStyle(document.querySelector('input')||body).colorScheme,
              bodyVisible:true,readyState:document.readyState
            });
          }
          if(document.readyState!=='complete') requestAnimationFrame(inspect);
        };
        requestAnimationFrame(inspect);
      }
    """.replace("THEME", json.dumps(theme)))
    page = context.new_page()
    install_fixtures(page)
    target = EVIDENCE / "filmstrip" / f"{theme}-{page_kind}"
    target.mkdir(parents=True, exist_ok=True)
    cdp = context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.send("Network.emulateNetworkConditions", {
        "offline": False, "latency": 150,
        "downloadThroughput": 64 * 1024, "uploadThroughput": 32 * 1024,
        "connectionType": "cellular3g",
    })
    frames = []
    cache_mode = "setup"

    def frame(event):
        index = len(frames)
        filename = f"{cache_mode}-{index:04d}.png"
        (target / filename).write_bytes(base64.b64decode(event["data"]))
        frames.append({"index": index, "cache": cache_mode, "file": filename,
                       "metadata": event["metadata"]})
        cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})

    cdp.on("Page.screencastFrame", frame)
    try:
        if page_kind in ("portal", "logout"):
            sign_in(page, acceptance_portal.base)
        cdp.send("Page.startScreencast", {"format": "png", "everyNthFrame": 1})
        captures = []
        for mode in ("cold", "hot"):
            cache_mode = mode
            cdp.send("Network.setCacheDisabled", {"cacheDisabled": mode == "cold"})
            if mode == "cold":
                cdp.send("Network.clearBrowserCache")
            if page_kind == "logout":
                sign_in(page, acceptance_portal.base)
                page.locator("#tools-toggle").click()
                page.locator("#logout-btn").click()
                page.wait_for_url("**/login", wait_until="load")
            else:
                page.goto(acceptance_portal.base + ("/login" if page_kind == "login" else "/"),
                          wait_until="load")
            page.evaluate("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
            samples = page.evaluate("window.__acceptanceFirstVisibleSamples")
            final = page.evaluate("""() => ({
              background:getComputedStyle(document.documentElement).backgroundColor,
              colorScheme:getComputedStyle(document.documentElement).colorScheme,
              bodyBackground:getComputedStyle(document.body).backgroundColor,
              bodyBackgroundImage:getComputedStyle(document.body).backgroundImage,
              nativeControlScheme:getComputedStyle(document.querySelector('input')||document.body).colorScheme
            })""")
            captures.append({"cache": mode, "first_visible_samples": samples, "final": final})
            assert samples, "No first-visible observations captured; cannot claim first-frame correctness"
            for key in final:
                assert all(s[key] == final[key] for s in samples), \
                    ("Wrong initial visible styling", key, captures)
        cdp.send("Page.stopScreencast")
        assert any(f["cache"] == "cold" for f in frames)
        assert any(f["cache"] == "hot" for f in frames)
        (target / "manifest.json").write_text(json.dumps({
            "browser": acceptance_browser.version,
            "method": "CDP PNG screencast + first-visible rAF computed-style observations",
            "network": {"latency_ms": 150, "download_bytes_sec": 64 * 1024},
            "frames": frames, "captures": captures,
            "review_status": "MANUAL FRAME REVIEW REQUIRED; final screenshot alone is insufficient",
            "boundary": "Local loopback fixture server; not real desktop hardware screen recording",
        }, indent=2) + "\n")
    finally:
        context.close()
