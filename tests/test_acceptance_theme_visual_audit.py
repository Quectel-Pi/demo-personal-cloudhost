"""Independent non-colour signatures and rendered-background text measurements."""
import io
import itertools
import json
import math

import pytest
from PIL import Image

from test_acceptance_theme_browser import (
    EVIDENCE, LANGUAGES, THEMES, acceptance_browser, acceptance_freeze_guard,
    acceptance_portal, choose, install_fixtures, language, open_selector, sign_in,
)


TEXT_RECTS = r"""() => {
  const result=[];
  for(const n of document.querySelectorAll('body *')){
    const s=getComputedStyle(n),box=n.getBoundingClientRect();
    if(!box.width||!box.height||s.visibility!=='visible'||n.closest('[hidden],script,style'))continue;
    let opacity=1;
    for(let p=n;p;p=p.parentElement)opacity*=+getComputedStyle(p).opacity;
    for(const child of n.childNodes){
      if(child.nodeType!==Node.TEXT_NODE||!child.textContent.trim())continue;
      const range=document.createRange();range.selectNodeContents(child);
      for(const r of range.getClientRects()){
        if(r.width<2||r.height<2||r.bottom<0||r.top>=innerHeight||r.right<0||r.left>=innerWidth)continue;
        const points=[.15,.5,.85].map(f=>({
          x:Math.floor(r.x+r.width*f)+.5,y:Math.floor(r.y+r.height*.5)+.5}))
          .filter(p=>p.x>=0&&p.x<innerWidth&&p.y>=0&&p.y<innerHeight)
          .filter(p=>{const hit=document.elementFromPoint(p.x,p.y);
            return hit&&(hit===n||n.contains(hit)||hit.contains(n));});
        if(!points.length)continue;
        result.push({tag:n.tagName,id:n.id,text:child.textContent.trim().slice(0,120),points,
          color:s.color,opacity,fontSize:s.fontSize,
          disabled:!!n.closest(':disabled'),rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
      }
    }
  }
  return result;
}"""


def luminance(rgb):
    channels = [v / 255 for v in rgb[:3]]
    channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
                for v in channels]
    return sum(a * b for a, b in zip(channels, (.2126, .7152, .0722)))


def ratio(fg, bg):
    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + .05) / (min(a, b) + .05)


def rendered_contrast(page, path):
    import re
    rows = page.evaluate(TEXT_RECTS)
    style = page.add_style_tag(content=(
        "body,body * { -webkit-text-fill-color:transparent!important;text-shadow:none!important; }"
    ))
    try:
        data = page.screenshot()
        path.write_bytes(data)
        image = Image.open(io.BytesIO(data)).convert("RGB")
    finally:
        style.evaluate("n=>n.remove()")
    for row in rows:
        values = list(map(float, re.findall(r"[\d.]+", row["color"])))
        opacity = row["opacity"] * (values[3] if len(values) > 3 else 1)
        samples = []
        for point in row["points"]:
            x = max(0, min(image.width - 1, int(point["x"])))
            y = max(0, min(image.height - 1, int(point["y"])))
            bg = image.getpixel((x, y))
            fg = [values[i] * opacity + bg[i] * (1 - opacity) for i in range(3)]
            samples.append({"x": x, "y": y, "background_rgb": bg,
                            "contrast": ratio(fg, bg)})
        row["rendered_background_samples"] = samples
        row["minimum_ratio"] = min(s["contrast"] for s in samples)
    return rows


@pytest.mark.parametrize("theme,locale,size", itertools.product(
    THEMES, LANGUAGES, ((390, 844), (1280, 800))))
def test_acceptance_theme_rendered_state_contrast(
        acceptance_browser, acceptance_portal, theme, locale, size):
    context = acceptance_browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    target = EVIDENCE / "rendered-contrast"
    target.mkdir(exist_ok=True)
    prefix = f"{theme}-{locale}-{size[0]}x{size[1]}"
    result = {"theme": theme, "locale": locale, "viewport": size,
              "browser": acceptance_browser.version,
              "method": "Foreground computed RGB versus actual screenshot background pixels after "
              "WebKit text-fill transparent (not color, preserving currentColor surfaces); "
              "3 samples per visible direct-text line. Captures are text-hidden measurement masks, "
              "not final visual screenshots. Group opacity is an approximation requiring review.",
              "states": {}}
    failures = []
    try:
        page.goto(acceptance_portal.base + "/login")
        language(page, locale)
        choose(page, theme)
        for state in ("login", "login-button-hover", "login-button-active",
                      "selector", "hover", "active"):
            if state == "selector":
                open_selector(page)
            if state in ("hover", "active", "login-button-hover", "login-button-active"):
                button = page.locator("#login-submit") if state.startswith("login-button") \
                    else page.get_by_role("radio").first
                button.hover()
                if state.endswith("active"):
                    page.mouse.down()
            rows = rendered_contrast(page, target / f"{prefix}-{state}-background-mask.png")
            result["states"][state] = rows
            failures.extend({"state": state, **row} for row in rows if row["minimum_ratio"] < 4.5)
            if state.endswith("active"):
                page.mouse.move(0, 0)
                page.mouse.up()
        page.keyboard.press("Escape")
        choose(page, theme)
        page.locator("#username").fill("")
        page.locator("#password").fill("")
        page.locator("#login-submit").click()
        assert page.locator("#login-error").inner_text()
        page.screenshot(path=str(target / f"{prefix}-form-error-visible.png"))
        rows = rendered_contrast(page, target / f"{prefix}-form-error-background-mask.png")
        result["states"]["form-error"] = rows
        failures.extend({"state": "form-error", **row} for row in rows if row["minimum_ratio"] < 4.5)
        pending = []
        page.route("**/auth/login", lambda route: pending.append(route))
        page.locator("#username").fill("acceptance-fixture")
        page.locator("#password").fill("fixture-not-a-real-credential")
        page.locator("#login-submit").click()
        page.wait_for_function("document.querySelector('#login-submit').disabled")
        assert len(pending) == 1
        page.wait_for_timeout(250)
        page.screenshot(path=str(target / f"{prefix}-loading-disabled-visible.png"))
        rows = rendered_contrast(page, target / f"{prefix}-loading-disabled-background-mask.png")
        result["states"]["loading-disabled"] = rows
        failures.extend({"state": "loading-disabled", **row} for row in rows if row["minimum_ratio"] < 4.5)
        pending.pop().fulfill(status=401, json={"error": {"code": "E1002"}})
        page.wait_for_function("!document.querySelector('#login-submit').disabled")
        page.unroute("**/auth/login")
        install_fixtures(page)
        sign_in(page, acceptance_portal.base)
        for state in ("normal", "warning", "failure", "unknown", "stale", "empty"):
            install_fixtures(page, state)
            page.goto(acceptance_portal.base + "/?acceptance_state=" + state + "#overview")
            page.locator("#view-overview").wait_for(state="visible")
            page.locator("#overview-services > details > summary").click()
            if state == "stale":
                page.locator('.nav-tab[data-view="remote"]').evaluate("n=>n.click()")
                page.locator(".remote-diagnostics").evaluate("n=>n.open=true")
            page.evaluate("() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
            rows = rendered_contrast(page, target / f"{prefix}-{state}-background-mask.png")
            result["states"][state] = rows
            failures.extend({"state": state, **row} for row in rows if row["minimum_ratio"] < 4.5)
        result["failures"] = failures
        (target / f"{prefix}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        assert not failures, ("Rendered background text contrast <4.5; inspect raw samples", failures)
    finally:
        context.close()


@pytest.mark.parametrize("size", ((390, 844), (1280, 800)))
def test_acceptance_theme_noncolour_pair_signatures(
        acceptance_browser, acceptance_portal, size):
    context = acceptance_browser.new_context(viewport={"width": size[0], "height": size[1]})
    page = context.new_page()
    install_fixtures(page)
    target = EVIDENCE / "noncolour-signatures"
    target.mkdir(exist_ok=True)
    signatures = {}
    try:
        sign_in(page, acceptance_portal.base)
        for theme in THEMES:
            choose(page, theme)
            page.evaluate("() => new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
            signatures[theme] = page.evaluate("""() => {
              const out={};
              for(const selector of ['#view-overview','.product-hero','.purpose-panel',
                '#product-title','.link-card','.metric','.metric-label','.metric-value','.topbar']){
                const n=document.querySelector(selector),s=getComputedStyle(n),r=n.getBoundingClientRect();
                out[selector]={display:s.display,columns:s.gridTemplateColumns,
                  radius:s.borderRadius,borderStyle:s.borderStyle,borderWidth:s.borderWidth,
                  fontSize:s.fontSize,fontWeight:s.fontWeight,fontFamily:s.fontFamily,
                  letterSpacing:s.letterSpacing,textTransform:s.textTransform,
                  clipPath:s.clipPath,padding:s.padding,gap:s.gap,shadow:s.boxShadow,
                  rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
              }return out;
            }""")
        pairs = []
        for a, b in itertools.combinations(THEMES, 2):
            differences = [{"selector": selector, "feature": key, "a": value,
                            "b": signatures[b][selector][key]}
                           for selector, values in signatures[a].items()
                           for key, value in values.items()
                           if key not in ("rect", "shadow") and signatures[b][selector][key] != value]
            categories = set()
            for diff in differences:
                feature = diff["feature"]
                categories.add("typography" if feature in (
                    "fontSize", "fontWeight", "fontFamily", "letterSpacing", "textTransform")
                    else "shape" if feature in (
                        "radius", "clipPath", "borderStyle", "borderWidth")
                    else "organization")
            pairs.append({"a": a, "b": b, "differences": differences,
                          "noncolour_categories": sorted(categories)})
            assert len(categories) >= 2, ("Need at least two independent non-colour traits", a, b, differences)
        (target / f"{size[0]}x{size[1]}.json").write_text(json.dumps({
            "browser": acceptance_browser.version, "viewport": size,
            "signatures": signatures, "pairs": pairs,
            "limitation": "Computed signatures plus separate masked screenshot review; not token-only proof",
        }, indent=2) + "\n")
    finally:
        context.close()


@pytest.mark.parametrize("theme", THEMES)
def test_acceptance_theme_essential_boundary_pixels(
        acceptance_browser, acceptance_portal, theme):
    import re
    context = acceptance_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    target = EVIDENCE / "rendered-contrast"
    target.mkdir(exist_ok=True)
    samples = []
    try:
        page.goto(acceptance_portal.base + "/login")
        choose(page, theme)
        for selector in ("#username", "#password", "#login-submit", "[data-theme-control] > button"):
            node = page.locator(selector)
            node.scroll_into_view_if_needed()
            data = node.evaluate("""n=>{
              const s=getComputedStyle(n),r=n.getBoundingClientRect();
              return {selector:n.id||n.className,border:s.borderTopColor,
                width:parseFloat(s.borderTopWidth),background:s.backgroundColor,
                rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
            }""")
            image = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
            rect = data["rect"]
            x, y = int(rect["x"] + rect["w"] / 2), max(0, int(rect["y"] - 2))
            outer = image.getpixel((x, y))
            border = list(map(float, re.findall(r"[\d.]+", data["border"])))
            alpha = border[3] if len(border) == 4 else 1
            stroke = [border[i] * alpha + outer[i] * (1 - alpha) for i in range(3)]
            fill_y = min(image.height - 1, int(rect["y"] + max(4, data["width"] + 2)))
            fill = image.getpixel((x, fill_y))
            data.update({"adjacent_actual_rgb": outer, "fill_actual_rgb": fill,
                         "border_ratio": ratio(stroke, outer), "fill_ratio": ratio(fill, outer)})
            samples.append(data)
        (target / f"{theme}-necessary-control-boundaries.json").write_text(json.dumps({
            "browser": acceptance_browser.version,
            "method": "Computed border composited against actual adjacent screenshot RGB; "
            "filled primary control may use its contrasting fill as boundary",
            "samples": samples,
        }, indent=2) + "\n")
        assert all(max(s["border_ratio"], s["fill_ratio"]) >= 3 for s in samples), samples
    finally:
        context.close()


@pytest.mark.parametrize("theme,locale", itertools.product(THEMES, LANGUAGES))
@pytest.mark.parametrize("motion", ("no-preference", "reduce"))
def test_acceptance_theme_loading_transition_contrast(
        acceptance_browser, acceptance_portal, theme, locale, motion):
    context = acceptance_browser.new_context(
        viewport={"width": 1280, "height": 800}, reduced_motion=motion)
    page = context.new_page()
    pending = []
    page.route("**/auth/login", lambda route: pending.append(route))
    try:
        page.goto(acceptance_portal.base + "/login")
        language(page, locale)
        choose(page, theme)
        page.locator("#username").fill("acceptance-fixture")
        page.locator("#password").fill("not-a-real-credential")
        page.evaluate(r"""() => {
          const n=document.querySelector('#login-submit');
          window.__acceptanceDisabledContrast={enter:[],release:[]};
          const rgba=s=>s.match(/[\d.]+/g).map(Number);
          const lum=c=>c.slice(0,3).map(v=>{
            v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4);
          }).reduce((a,v,i)=>a+v*[.2126,.7152,.0722][i],0);
          const capture=(phase,started)=>{
            const s=getComputedStyle(n),f=rgba(s.color),b=rgba(s.backgroundColor);
            let painted=[0,0,0],alpha=0,solidBackgroundValidated=true;
            const backgroundLayers=[];
            for(let element=n;element&&alpha<1;element=element.parentElement){
              const cs=getComputedStyle(element),stops=cs.backgroundImage.match(/rgba?\([^)]+\)/g)||[];
              const uniform=/^linear-gradient\(/.test(cs.backgroundImage)
                &&stops.length>=2&&stops.every(c=>c===stops[0])
                &&(rgba(stops[0]).length===3||rgba(stops[0])[3]===1);
              const colour=uniform?rgba(stops[0]):rgba(cs.backgroundColor);
              backgroundLayers.push({id:element.id,className:element.className,
                colour,image:cs.backgroundImage,opacity:cs.opacity});
              if((cs.backgroundImage!=='none'&&!uniform)||cs.opacity!=='1'
                ||(element!==n&&cs.filter!=='none'))solidBackgroundValidated=false;
              const a=colour.length===3?1:colour[3];
              painted=painted.map((v,i)=>v+colour[i]*a*(1-alpha));
              alpha+=a*(1-alpha);
            }
            solidBackgroundValidated=solidBackgroundValidated&&alpha===1
              &&(f.length===3||f[3]===1);
            const brightness=s.filter.match(/^brightness\(([\d.]+)\)$/);
            if(s.filter!=='none'&&!brightness)solidBackgroundValidated=false;
            const gain=brightness?Number(brightness[1]):1;
            if(gain!==1&&backgroundLayers.length!==1)solidBackgroundValidated=false;
            painted=painted.map(v=>Math.min(255,v*gain));
            const actualForeground=f.slice(0,3).map(v=>Math.min(255,v*gain));
            const a=lum(actualForeground),z=lum(painted);
            window.__acceptanceDisabledContrast[phase].push({
              time:performance.now(),elapsed_ms:performance.now()-started,
              foreground:s.color,background:s.backgroundColor,
              backgroundImage:s.backgroundImage,opacity:s.opacity,
              filter:s.filter,actualForeground,backgroundLayers,
              actualOpaquePaintedBackground:painted,solidBackgroundValidated,
              text:n.textContent,disabled:n.disabled,
              ratio:(Math.max(a,z)+.05)/(Math.min(a,z)+.05)});
            if(performance.now()-started<350)
              requestAnimationFrame(()=>capture(phase,started));
          };
          new MutationObserver(()=>{
            const phase=n.disabled?'enter':'release';
            if(!window.__acceptanceDisabledContrast[phase].length)
              capture(phase,performance.now());
          }).observe(n,{attributes:true,attributeFilter:['disabled']});
        }""")
        page.locator("#login-submit").click()
        page.wait_for_function("window.__acceptanceDisabledContrast.enter.length && "
                               "window.__acceptanceDisabledContrast.enter.at(-1).elapsed_ms>=350")
        assert len(pending) == 1, "Exactly one fixture authentication request must be pending"
        target = EVIDENCE / "rendered-contrast"
        target.mkdir(exist_ok=True)
        page.screenshot(path=str(
            target / f"{theme}-{locale}-{motion}-loading-transition-settled.png"))
        pending.pop().fulfill(status=401, json={"error": {"code": "E1001"}})
        page.wait_for_function("window.__acceptanceDisabledContrast.release.length && "
                               "window.__acceptanceDisabledContrast.release.at(-1).elapsed_ms>=350")
        phases = page.evaluate("window.__acceptanceDisabledContrast")
        rows = phases["enter"] + phases["release"]
        (target / f"{theme}-{locale}-{motion}-loading-transition.json").write_text(json.dumps({
            "browser": acceptance_browser.version, "theme": theme, "language": locale,
            "reduced_motion": motion,
            "method": "Same-frame computed foreground/background sampled immediately on disabled "
            "entry and release mutations and every rAF for >=350ms each; fixture authentication "
            "held until entry sampling completes then rejected; opaque constant-colour gradient "
            "stops resolve to actual paint, transparent colour layers composite through solid "
            "ancestors, uniform brightness is applied; unresolved gradients/opacity rejected; "
            "no screenshot timing mismatch",
            "phases": phases,
        }, ensure_ascii=False, indent=2) + "\n")
        page.screenshot(path=str(
            target / f"{theme}-{locale}-{motion}-loading-release-settled.png"))
        assert all(s["solidBackgroundValidated"] and s["opacity"] == "1" for s in rows), \
            ("Needs composited pixel measurement; not a solid colour assertion", rows)
        assert all(s["ratio"] >= 4.5 for s in rows), \
            ("Loading/disabled text falls below 4.5 during actual transition", rows)
        assert all(s["disabled"] for s in phases["enter"])
        assert not any(s["disabled"] for s in phases["release"])
        assert page.locator("#username").input_value() == "acceptance-fixture"
        assert page.locator("#password").input_value() == "not-a-real-credential"
    finally:
        for route in pending:
            route.abort()
        context.close()
