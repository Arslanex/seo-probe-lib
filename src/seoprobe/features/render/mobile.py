"""
Mobile view: tap targets, small text, horizontal overflow, viewport and desktop content parity.

The page is loaded twice in the guarded browser (``Renderer.open_page``): once with the mobile
viewport, user agent and pixel ratio, once on desktop for the parity comparison. Every number is
measured in CSS pixels after the ``load`` event plus a short wait; late layout changes are not
seen. Thresholds are keyword parameters; their defaults live in ``thresholds.json``.
"""

from __future__ import annotations

import asyncio
import json
from functools import cache
from importlib.resources import files
from typing import Any, Literal

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.browser import (
    DESKTOP_VIEWPORT,
    MOBILE_DPR,
    MOBILE_UA,
    MOBILE_VIEWPORT,
    PlaywrightError,
    Renderer,
    RenderUnavailable,
)
from seoprobe.core.net.safety import URLSafetyError
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog

from .models import DeviceView, MobileReport, TapOverlap, TapTarget

NAV_TIMEOUT_MS = 20000
SETTLE_MS = 800
MOBILE_RULES = (
    "mobile-small-tap-targets",
    "mobile-overlapping-tap-targets",
    "mobile-small-text",
    "mobile-horizontal-overflow",
    "mobile-viewport-not-device-width",
)
PARITY_RULE = "mobile-content-parity"

Device = Literal["desktop", "mobile"]


@cache
def defaults() -> dict[str, float]:
    data: dict[str, dict[str, float]] = json.loads(
        files(__package__).joinpath("thresholds.json").read_text(encoding="utf-8")
    )
    return data["mobile"]


# Shared selector builder: "#id" when unique, else a short tag:nth-of-type path.
CSS_PATH_JS = """
const cssPath = el => {
  if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) return '#' + CSS.escape(el.id);
  const parts = [];
  let cur = el;
  while (cur && cur.nodeType === 1 && cur !== document.documentElement && parts.length < 4) {
    let part = cur.tagName.toLowerCase();
    if (cur.id && document.querySelectorAll('#' + CSS.escape(cur.id)).length === 1) {
      parts.unshift('#' + CSS.escape(cur.id)); break;
    }
    const parent = cur.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
      if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
    }
    parts.unshift(part);
    cur = parent;
  }
  return parts.join(' > ');
};
"""

MOBILE_JS = (
    "(opts) => {"
    + CSS_PATH_JS
    + """
  const vw = window.innerWidth;
  const root = document.documentElement;
  const shown = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'
           && cs.opacity !== '0'; };
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], [onclick]';
  const targets = Array.from(document.querySelectorAll(sel)).slice(0, 2000).filter(shown);
  const rects = targets.map(t => t.getBoundingClientRect());
  const small = [];
  targets.forEach((t, i) => {
    const r = rects[i];
    if (r.width < opts.minTap || r.height < opts.minTap) small.push({
      selector: cssPath(t), width: Math.round(r.width * 10) / 10, height: Math.round(r.height * 10) / 10,
      text: (t.innerText || t.value || t.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 60)});
  });
  const overlaps = [];
  let overlapCount = 0;
  const n = Math.min(targets.length, 600);
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      const a = rects[i], b = rects[j];
      if (targets[i].contains(targets[j]) || targets[j].contains(targets[i])) continue;
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (w > 0.5 && h > 0.5) {
        overlapCount++;
        if (overlaps.length < opts.maxExamples) overlaps.push({first: cssPath(targets[i]), second: cssPath(targets[j])});
      }
    }
  }
  let textNodes = 0, smallText = 0;
  const walker = document.createTreeWalker(document.body || root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && textNodes < 5000) {
    const t = walker.currentNode.nodeValue.trim(); const el = walker.currentNode.parentElement;
    if (!t || !el || ['SCRIPT','STYLE','NOSCRIPT','TEMPLATE'].includes(el.tagName) || !shown(el)) continue;
    textNodes++;
    if (parseFloat(getComputedStyle(el).fontSize) < opts.smallFont) smallText++;
  }
  const meta = document.querySelector('meta[name=viewport]');
  return {tap_targets: targets.length, small_tap_targets: small.length,
          small_tap_examples: small.slice(0, opts.maxExamples),
          overlapping_tap_targets: overlapCount, overlap_examples: overlaps,
          text_nodes: textNodes, small_text_nodes: smallText,
          scroll_width: root.scrollWidth, viewport_width: vw,
          viewport_meta: meta ? (meta.getAttribute('content') || '') : null};
}
"""
)

VIEW_JS = """
() => {
  const host = location.host;
  let links = 0;
  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
    if (!a.getClientRects().length || getComputedStyle(a).visibility === 'hidden') continue;
    try { const u = new URL(a.getAttribute('href'), document.baseURI);
          if ((u.protocol === 'http:' || u.protocol === 'https:') && u.host === host) links++; } catch (e) {}
  }
  return {visible_text_chars: ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim().length,
          internal_links: links};
}
"""


async def _measure(
    renderer: Renderer, url: str, device: Device, opts: dict[str, float]
) -> tuple[DeviceView, dict[str, Any] | None]:
    mobile = device == "mobile"
    async with renderer.open_page(
        viewport=MOBILE_VIEWPORT if mobile else DESKTOP_VIEWPORT,
        mobile=mobile,
        device_scale_factor=MOBILE_DPR if mobile else 1.0,
        user_agent=MOBILE_UA if mobile else None,
        blocked_types=(),
        max_requests=300,
        max_bytes=30 * 1024 * 1024,
    ) as sess:
        page = sess.page
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(SETTLE_MS)
        view = DeviceView(**await page.evaluate(VIEW_JS))
        facts = await page.evaluate(MOBILE_JS, opts) if mobile else None
    return view, facts


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 3) if b else None


def mobile_findings(rep: MobileReport, url: str, lang: str = "en") -> list[Finding]:
    th = rep.thresholds
    out: list[Finding] = []

    def add(rid: str, evidence: str, count: int | None = None) -> None:
        out.append(catalog.make_finding(rid, url=url, evidence=evidence, count=count, lang=lang))

    if rep.tap_targets and rep.small_tap_share >= th["small_tap_share"]:
        ex = ", ".join(f"{t.selector} ({t.width:g}x{t.height:g})" for t in rep.small_tap_examples[:5])
        add(
            "mobile-small-tap-targets",
            f"{rep.small_tap_targets} of {rep.tap_targets} tap targets ({round(rep.small_tap_share * 100)}%) "
            f"are smaller than {th['min_tap_px']:g}x{th['min_tap_px']:g} px "
            f"(threshold {round(th['small_tap_share'] * 100)}%): {ex}",
            rep.small_tap_targets,
        )
    if rep.overlapping_tap_targets >= th["min_overlaps"]:
        ex = ", ".join(f"{o.first} / {o.second}" for o in rep.overlap_examples[:5])
        add(
            "mobile-overlapping-tap-targets",
            f"{rep.overlapping_tap_targets} overlapping tap target pairs (threshold {th['min_overlaps']:g}): {ex}",
            rep.overlapping_tap_targets,
        )
    if rep.text_nodes and rep.small_text_share >= th["small_text_share"]:
        add(
            "mobile-small-text",
            f"{rep.small_text_nodes} of {rep.text_nodes} text blocks ({round(rep.small_text_share * 100)}%) "
            f"are below {th['small_font_px']:g} px (threshold {round(th['small_text_share'] * 100)}%)",
            rep.small_text_nodes,
        )
    if rep.horizontal_overflow:
        add(
            "mobile-horizontal-overflow",
            f"page width {rep.scroll_width} px, viewport {rep.viewport_width} px",
        )
    if not rep.viewport_device_width:
        add(
            "mobile-viewport-not-device-width",
            f"viewport meta: '{rep.viewport_meta}'" if rep.viewport_meta_present else "no viewport meta tag",
        )
    low: list[str] = []
    if rep.mobile is not None and rep.desktop is not None:
        for label, ratio, m, d in (
            ("visible text", rep.text_ratio, rep.mobile.visible_text_chars, rep.desktop.visible_text_chars),
            ("internal links", rep.link_ratio, rep.mobile.internal_links, rep.desktop.internal_links),
        ):
            if ratio is not None and ratio < th["min_parity_ratio"]:
                low.append(f"{label} {round(ratio * 100)}% of desktop ({m} vs {d})")
    if low:
        add(PARITY_RULE, "; ".join(low) + f" (threshold {round(th['min_parity_ratio'] * 100)}%)")
    return out


@operation(
    "mobile",
    output=MobileReport,
    requires=Requirements(network=True, browser=True, extras=["browser"]),
    timeout_s=90.0,
)
async def mobile(
    ctx: Context,
    target: str | Page,
    *,
    min_tap_px: float | None = None,
    small_tap_share: float | None = None,
    min_overlaps: int | None = None,
    small_font_px: float | None = None,
    small_text_share: float | None = None,
    min_parity_ratio: float | None = None,
) -> Outcome[MobileReport]:
    """Mobile usability checks in a phone-sized browser and content parity with desktop."""
    page = await ctx.page(target)
    url = page.final_url or page.url
    given = {
        "min_tap_px": min_tap_px,
        "small_tap_share": small_tap_share,
        "min_overlaps": min_overlaps,
        "small_font_px": small_font_px,
        "small_text_share": small_text_share,
        "min_parity_ratio": min_parity_ratio,
    }
    th = {k: float(v) for k, v in defaults().items()}
    th.update({k: float(v) for k, v in given.items() if v is not None})
    rep = MobileReport(url=url, thresholds=th)
    renderer = await ctx.browser()
    try:
        await renderer.judge(url)
    except URLSafetyError:
        return Outcome(
            data=rep,
            not_checked=[ctx.not_checked(r, "target-rejected", url=url) for r in (*MOBILE_RULES, PARITY_RULE)],
        )
    opts = {
        "minTap": th["min_tap_px"],
        "smallFont": th["small_font_px"],
        "maxExamples": int(th["max_examples"]),
    }
    not_checked: list[NotChecked] = []
    devices: tuple[Device, ...] = ("mobile", "desktop")
    for device in devices:
        try:
            view, facts = await asyncio.wait_for(
                _measure(renderer, url, device, opts),
                timeout=NAV_TIMEOUT_MS / 1000 + 15,
            )
        except (RenderUnavailable, PlaywrightError, TimeoutError, OSError) as exc:
            rep.errors[device] = f"render-failed: {type(exc).__name__}"
            rules = (*MOBILE_RULES, PARITY_RULE) if device == "mobile" else (PARITY_RULE,)
            not_checked += [ctx.not_checked(r, "render-failed", url=url) for r in rules]
            if device == "mobile":
                return Outcome(data=rep, not_checked=not_checked)
            continue
        if device == "desktop":
            rep.desktop = view
            continue
        assert facts is not None
        rep.mobile = view
        rep.tap_targets = int(facts["tap_targets"])
        rep.small_tap_targets = int(facts["small_tap_targets"])
        rep.small_tap_share = round(rep.small_tap_targets / rep.tap_targets, 3) if rep.tap_targets else 0.0
        rep.small_tap_examples = [TapTarget(**t) for t in facts["small_tap_examples"]]
        rep.overlapping_tap_targets = int(facts["overlapping_tap_targets"])
        rep.overlap_examples = [TapOverlap(**o) for o in facts["overlap_examples"]]
        rep.text_nodes = int(facts["text_nodes"])
        rep.small_text_nodes = int(facts["small_text_nodes"])
        rep.small_text_share = round(rep.small_text_nodes / rep.text_nodes, 3) if rep.text_nodes else 0.0
        rep.scroll_width = int(facts["scroll_width"])
        rep.viewport_width = int(facts["viewport_width"])
        rep.horizontal_overflow = rep.scroll_width > rep.viewport_width
        meta = facts["viewport_meta"]
        rep.viewport_meta = meta
        rep.viewport_meta_present = meta is not None
        rep.viewport_device_width = meta is not None and "width=device-width" in meta.replace(" ", "").lower()
    if rep.mobile is not None and rep.desktop is not None:
        rep.text_ratio = _ratio(rep.mobile.visible_text_chars, rep.desktop.visible_text_chars)
        rep.link_ratio = _ratio(rep.mobile.internal_links, rep.desktop.internal_links)
    return Outcome(data=rep, findings=mobile_findings(rep, url, ctx.lang), not_checked=not_checked)
