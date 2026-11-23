"""
Screenshots: desktop and mobile "above the fold" pictures plus a DOM check.

The page is loaded in a guarded browser context (``Renderer.open_page``: every request is judged
by the SSRF policy, same guard as ``render``), once per device (two loads). The picture is the
viewport only (1366x768 desktop, 390x844 at device pixel ratio 2 mobile), a JPEG (quality
lowered until it fits ``MAX_BYTES``) returned base64-encoded in the model.

Above-the-fold DOM facts are heuristics: "main action" is a button or a link that looks like a
button (class contains btn/button/cta, or role=button), the overlay test looks at fixed/sticky
elements that cover more than 30% of the viewport. Pages that load such layers late (after our
~1 s wait) are not seen. This is a lab view from this machine, not what every visitor sees.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Literal

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.browser import (
    DESKTOP_VIEWPORT,
    MOBILE_UA,
    MOBILE_VIEWPORT,
    PlaywrightError,
    Renderer,
    RenderUnavailable,
)
from seoprobe.core.net.safety import URLSafetyError
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog

from .models import AboveFold, ScreenshotFile, Screenshots

MOBILE_DPR = 2.0
MAX_BYTES = 400 * 1024
QUALITIES = (70, 55, 40, 28)
OVERLAY_SHARE = 0.30
NAV_TIMEOUT_MS = 20000

Device = Literal["desktop", "mobile"]

ABOVE_FOLD_JS = """
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const root = document.documentElement;
  const shown = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'
           && cs.opacity !== '0'; };
  const inView = el => { const r = el.getBoundingClientRect();
    return r.top < vh && r.bottom > 0 && r.left < vw && r.right > 0; };
  const h1s = Array.from(document.querySelectorAll('h1'));
  const h1_visible = h1s.some(h => shown(h) && inView(h));
  const ctaSel = 'button, input[type=submit], input[type=button], [role=button], a[class*=btn], a[class*=button], a[class*=cta]';
  const cta_visible = Array.from(document.querySelectorAll(ctaSel)).slice(0, 500)
    .some(e => shown(e) && inView(e) && (e.textContent || e.value || '').trim().length > 0);
  let chars = 0, nodes = 0;
  const walker = document.createTreeWalker(document.body || root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && nodes < 4000) {
    nodes++;
    const t = walker.currentNode.nodeValue.trim(); const el = walker.currentNode.parentElement;
    if (!t || !el || ['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName)) continue;
    if (shown(el) && inView(el)) chars += t.length;
  }
  let best = 0, label = '';
  for (const el of Array.from(document.querySelectorAll('body *')).slice(0, 4000)) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
    if (!shown(el)) continue;
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    const share = (w * h) / (vw * vh);
    if (share > best) { best = share; label = (el.id || el.getAttribute('role') || el.className || el.tagName).toString().slice(0, 60); }
  }
  return {h1_present: h1s.length > 0, h1_visible, cta_visible, text_chars: chars,
          horizontal_overflow: root.scrollWidth > root.clientWidth + 1,
          overlay_share: Math.round(best * 100) / 100, overlay_label: label};
}
"""


async def _shoot(page: Any) -> bytes:
    data = b""
    for q in QUALITIES:
        data = await page.screenshot(type="jpeg", quality=q, full_page=False)
        if len(data) <= MAX_BYTES:
            break
    return bytes(data)


async def _capture(renderer: Renderer, url: str, device: Device) -> tuple[ScreenshotFile, AboveFold]:
    mobile = device == "mobile"
    viewport = MOBILE_VIEWPORT if mobile else DESKTOP_VIEWPORT
    dpr = MOBILE_DPR if mobile else 1.0
    async with renderer.open_page(
        viewport=viewport,
        mobile=mobile,
        device_scale_factor=dpr,
        user_agent=MOBILE_UA if mobile else None,
        blocked_types=(),
        max_requests=300,
        max_bytes=30 * 1024 * 1024,
    ) as sess:
        page = sess.page
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(800)
        fold = AboveFold(**await page.evaluate(ABOVE_FOLD_JS))
        data = await _shoot(page)
    return (
        ScreenshotFile(
            device=device,
            name=f"{device}.jpg",
            width=int(viewport["width"] * dpr),
            height=int(viewport["height"] * dpr),
            bytes=len(data),
            data_base64=base64.b64encode(data).decode("ascii"),
        ),
        fold,
    )


def fold_findings(folds: dict[str, AboveFold], url: str, lang: str = "en") -> list[Finding]:
    out: list[Finding] = []
    no_h1 = [d for d, f in folds.items() if f.h1_present and not f.h1_visible]
    if no_h1:
        out.append(catalog.make_finding("above-fold-no-h1", url=url, evidence=", ".join(no_h1), lang=lang))
    covered = [
        f"{d} {round(f.overlay_share * 100)}% ({f.overlay_label})"
        for d, f in folds.items()
        if f.overlay_share > OVERLAY_SHARE
    ]
    if covered:
        out.append(catalog.make_finding("intrusive-interstitial", url=url, evidence="; ".join(covered), lang=lang))
    return out


@operation(
    "screenshots",
    output=Screenshots,
    requires=Requirements(network=True, browser=True, extras=["browser"]),
    timeout_s=60.0,
)
async def screenshots(ctx: Context, target: str | Page) -> Outcome[Screenshots]:
    """Desktop and mobile first-screen pictures and above-the-fold checks."""
    page = await ctx.page(target)
    url = page.final_url or page.url
    renderer = await ctx.browser()
    try:
        await renderer.judge(url)
    except URLSafetyError:
        return Outcome(
            data=Screenshots(),
            not_checked=[ctx.not_checked("screenshots", "target-rejected", url=url)],
        )
    data = Screenshots()
    not_checked: list[NotChecked] = []
    devices: tuple[Device, ...] = ("desktop", "mobile")
    for device in devices:
        try:
            f, fold = await asyncio.wait_for(
                _capture(renderer, url, device),
                timeout=NAV_TIMEOUT_MS / 1000 + 15,
            )
        except (RenderUnavailable, PlaywrightError, TimeoutError, OSError) as exc:
            data.errors[device] = f"render-failed: {type(exc).__name__}"
            not_checked.append(ctx.not_checked(f"screenshot-{device}", "render-failed", url=url))
            continue
        data.files.append(f)
        data.above_fold[device] = fold
    if not data.files:
        return Outcome(data=data, not_checked=not_checked)
    return Outcome(data=data, findings=fold_findings(data.above_fold, url, ctx.lang), not_checked=not_checked)
