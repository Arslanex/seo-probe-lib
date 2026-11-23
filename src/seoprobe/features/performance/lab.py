"""
Lab performance, page weight, resource and image analysis.

One page is loaded in a guarded browser context (the same route guard as ``Renderer.render``:
every request judged by the SSRF policy, redirects followed by us, service workers blocked).
The numbers are LAB values from this machine and network, not field data (CrUX/PSI).

Measurement notes (kept honest, see also the report's methodology):

* Requests are fulfilled by our route handler after it fetched the whole body. So the browser's
  own Navigation Timing (TTFB, FCP, LCP, load) includes that buffering. ``ttfb_ms`` is therefore
  taken from the handler: time to fetch the main document (server time + HTML download).
* Transferred bytes: ``content-length`` of the origin response when present (that is the encoded
  size on the wire), otherwise the decoded body length (an over-estimate for compressed bodies
  without a content-length).
* ``render_blocking`` counts Resource Timing entries whose ``renderBlockingStatus`` is
  ``"blocking"``; it is 0 when the browser does not expose the property.
* The registrable domain ("eTLD+1") is approximated: last two labels, last three when the last two
  form a known multi-label suffix from a small built-in list. NOT verified against the Public
  Suffix List; unknown multi-label suffixes are treated as two labels.
* ``third_party`` keeps the ten heaviest hosts only.
* ``uncompressed_text`` and ``no_cache_static`` list FIRST-PARTY resources only (the fix is a
  server setting of the audited site).
* The tap-target check follows WCAG 2.2 AA (24 CSS px minimum, the upstream project notes 48 px as the
  comfortable size) and skips inline links inside running text (the WCAG "inline" exception,
  approximated: inline element whose block parent has other text).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from seoprobe.core.errors import ErrorCode
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.net.browser import (
    DESKTOP_VIEWPORT,
    MOBILE_DPR,
    MOBILE_UA,
    MOBILE_VIEWPORT,
    PlaywrightError,
    Renderer,
    RenderUnavailable,
    playwright_installed,
)
from seoprobe.core.net.safety import URLSafetyError
from seoprobe.core.page import Page

from .models import ImageRecord, LabMetrics, PagePerf
from .vitals import perf_checks

Device = Literal["desktop", "mobile"]

KIB = 1024
# the upstream project skills/seo-images/SKILL.md "Tiered thresholds by image category" (KB taken as 1024 bytes):
# category -> (warning above, critical above)
IMAGE_SIZE_THRESHOLDS: dict[str, tuple[int, int]] = {
    "thumbnail": (100 * KIB, 200 * KIB),
    "content": (200 * KIB, 500 * KIB),
    "hero": (300 * KIB, 700 * KIB),
}
THUMBNAIL_MAX_W = 150
HERO_VIEWPORT_SHARE = 0.6
LEGACY_FORMAT_MIN_BYTES = 100 * KIB
OVERSIZED_RATIO = 2.0  # natural width > 2x (displayed width x device pixel ratio)
OVERSIZED_MIN_BYTES = 30 * KIB
MAX_IMAGES = 150
MAX_LISTED = 20

_TEXT_TYPES = {"document", "stylesheet", "script"}
_TEXT_CT = re.compile(r"(?:json|xml|svg|javascript|ecmascript|css|html|text/)", re.I)
_MULTI_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.tr", "org.tr", "net.tr", "gen.tr", "edu.tr",
    "gov.tr", "com.au", "net.au", "org.au", "co.jp", "co.kr", "co.in", "co.nz", "co.za",
    "com.br", "com.mx", "com.ar", "com.cn", "com.hk", "com.sg", "com.tw", "com.ua", "com.pl",
}  # fmt: skip


# ----------------------------------------------------------------------------- pure helpers


def registrable_domain(host: str) -> str:
    """Approximate eTLD+1 (see module docstring: not verified against the Public Suffix List)."""
    host = host.lower().strip(".")
    if not host or re.fullmatch(r"[\d.]+|[0-9a-f:]+", host):
        return host  # IP literals are their own site
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def map_resource_type(resource_type: str) -> str:
    if resource_type in ("document", "stylesheet", "script", "image", "font", "media"):
        return resource_type
    if resource_type in ("xhr", "fetch"):
        return "xhr"
    return "other"


def transfer_bytes(entry: dict[str, Any]) -> int:
    cl = str(entry.get("headers", {}).get("content-length", "")).strip()
    if cl.isdigit():
        return int(cl)
    return int(entry.get("body_len", 0))


def served_format(head: bytes, content_type: str | None) -> str:
    """Format actually served: magic bytes first (a .jpg URL may serve WebP), then Content-Type."""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:4] == b"GIF8":
        return "gif"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    if head[:4] == b"\x00\x00\x01\x00":
        return "ico"
    ct = (content_type or "").split(";")[0].strip().lower()
    table = {
        "image/webp": "webp",
        "image/avif": "avif",
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/gif": "gif",
        "image/svg+xml": "svg",
        "image/x-icon": "ico",
        "image/vnd.microsoft.icon": "ico",
    }
    if ct in table:
        return table[ct]
    if head.lstrip()[:5] in (b"<?xml", b"<svg "):
        return "svg"
    return "unknown"


def image_category(
    displayed_w: float | None, above_fold: bool, viewport_w: int
) -> Literal["thumbnail", "content", "hero"]:
    if displayed_w is not None and displayed_w <= THUMBNAIL_MAX_W:
        return "thumbnail"
    if above_fold and displayed_w is not None and displayed_w >= HERO_VIEWPORT_SHARE * viewport_w:
        return "hero"
    return "content"


def image_issues(
    fmt: str | None,
    nbytes: int | None,
    category: str,
    natural_w: int | None,
    displayed_w: float | None,
    dpr: float,
) -> list[str]:
    """
    Check ids this image triggers. Raster only: SVG/ICO are exempt from every size rule.

    A WebP/AVIF image is NEVER flagged as legacy format, whatever its URL extension says.
    """
    out: list[str] = []
    raster = fmt not in ("svg", "ico")
    if raster and nbytes is not None:
        warn, crit = IMAGE_SIZE_THRESHOLDS.get(category, IMAGE_SIZE_THRESHOLDS["content"])
        if nbytes > crit:
            out.append("image-critically-large")
        elif nbytes > warn:
            out.append("image-too-large")
        if fmt in ("jpeg", "png") and nbytes > LEGACY_FORMAT_MIN_BYTES:
            out.append("image-legacy-format")
        if (
            natural_w
            and displayed_w
            and nbytes >= OVERSIZED_MIN_BYTES
            and natural_w > OVERSIZED_RATIO * displayed_w * dpr
        ):
            out.append("image-oversized-for-display")
    return out


def summarize_resources(entries: list[dict[str, Any]], page_host: str) -> dict[str, Any]:
    """Aggregate the recorder log into the PagePerf resource fields (pure)."""
    first_party = registrable_domain(page_host)
    by_type: dict[str, dict[str, int]] = {}
    third: dict[str, dict[str, Any]] = {}
    uncompressed: list[str] = []
    no_cache: list[str] = []
    total = 0
    html_bytes = 0
    ttfb: int | None = None
    for e in entries:
        kind = map_resource_type(str(e.get("resource_type", "other")))
        size = transfer_bytes(e)
        total += size
        slot = by_type.setdefault(kind, {"count": 0, "bytes": 0})
        slot["count"] += 1
        slot["bytes"] += size
        host = (urlsplit(str(e.get("resolved_url") or e.get("url"))).hostname or "").lower()
        headers: dict[str, str] = e.get("headers", {})
        if e.get("is_main"):
            html_bytes = size
            ttfb = int(e.get("fetch_ms", 0))
        is_first = registrable_domain(host) == first_party
        if not is_first:
            t = third.setdefault(host, {"host": host, "requests": 0, "bytes": 0})
            t["requests"] += 1
            t["bytes"] += size
            continue
        url = str(e.get("url"))[:200]
        enc = headers.get("content-encoding", "").strip().lower()
        ctype = headers.get("content-type", "")
        if (
            (kind in _TEXT_TYPES or _TEXT_CT.search(ctype))
            and int(e.get("body_len", 0)) > KIB
            and enc in ("", "identity")
            and len(uncompressed) < MAX_LISTED
        ):
            uncompressed.append(url)
        if kind in ("script", "stylesheet", "image", "font") and int(e.get("status", 0)) == 200:
            if _short_lived(headers) and len(no_cache) < MAX_LISTED:
                no_cache.append(url)
    top = sorted(third.values(), key=lambda t: (-int(t["bytes"]), -int(t["requests"])))[:10]
    return {
        "total_requests": len(entries),
        "total_bytes": total,
        "html_bytes": html_bytes,
        "by_type": by_type,
        "third_party": top,
        "uncompressed_text": uncompressed,
        "no_cache_static": no_cache,
        "ttfb_ms": ttfb,
    }


def _short_lived(headers: dict[str, str]) -> bool:
    """True when a static response is not cacheable for at least a day and has no Expires."""
    if headers.get("expires"):
        return False
    cc = headers.get("cache-control", "").lower()
    if not cc:
        return True
    if "immutable" in cc:
        return False
    if "no-store" in cc or "no-cache" in cc:
        return True
    m = re.search(r"max-age\s*=\s*(\d+)", cc)
    return m is None or int(m.group(1)) < 86400


# ----------------------------------------------------------------------------- page scripts

INIT_JS = """
(() => {
  window.__seoprobe = {lcp: null, cls: 0, longtask: 0};
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) window.__seoprobe.lcp = e.startTime; })
        .observe({type: 'largest-contentful-paint', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) window.__seoprobe.cls += e.value; })
        .observe({type: 'layout-shift', buffered: true}); } catch (e) {}
  try { new PerformanceObserver(l => { for (const e of l.getEntries()) if (e.duration > 50) window.__seoprobe.longtask += e.duration - 50; })
        .observe({type: 'longtask', buffered: true}); } catch (e) {}
})();
"""

METRICS_JS = """
() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const fcp = (performance.getEntriesByName('first-contentful-paint')[0] || {}).startTime;
  const blocking = performance.getEntriesByType('resource')
    .filter(r => r.renderBlockingStatus === 'blocking').length;
  const s = window.__seoprobe || {};
  return {responseStart: nav.responseStart || null, dcl: nav.domContentLoadedEventEnd || null,
          load: nav.loadEventEnd || null, fcp: fcp || null, lcp: s.lcp, cls: s.cls || 0,
          tbt: s.longtask || 0, blocking: blocking};
}
"""

SCROLL_JS = """
async () => {
  const h = Math.min(document.documentElement.scrollHeight, 12000);
  for (let y = 0; y < h; y += Math.max(200, window.innerHeight)) {
    window.scrollTo(0, y);
    await new Promise(r => setTimeout(r, 120));
  }
  window.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 300));
}
"""

IMAGES_JS = """
() => Array.from(document.images).slice(0, 300).map(img => {
  const r = img.getBoundingClientRect();
  return {src: img.currentSrc || img.src || '', natW: img.naturalWidth || 0, natH: img.naturalHeight || 0,
          w: r.width, h: r.height, top: r.top, bottom: r.bottom,
          alt: img.hasAttribute('alt'), lazy: (img.getAttribute('loading') || '').toLowerCase() === 'lazy',
          vh: window.innerHeight};
})
"""

MOBILE_JS = """
() => {
  const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  let total = 0, small = 0, nodes = 0;
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && nodes < 5000) {
    nodes++;
    const t = walker.currentNode.nodeValue.trim();
    const el = walker.currentNode.parentElement;
    if (!t || !el || ['SCRIPT','STYLE','NOSCRIPT'].includes(el.tagName) || !visible(el)) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    total += t.length;
    if (fs < 12) small += t.length;
  }
  const sel = 'a[href], button, input[type=submit], input[type=button], [role=button]';
  let tiny = 0; const examples = [];
  for (const el of Array.from(document.querySelectorAll(sel)).slice(0, 2000)) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width >= 24 && r.height >= 24) continue;
    if (el.tagName === 'A' && getComputedStyle(el).display === 'inline') {
      const p = el.parentElement;
      if (p && (p.textContent || '').trim().length > (el.textContent || '').trim().length + 10) continue;
    }
    tiny++;
    if (examples.length < 3) examples.push((el.tagName + ' ' + (el.textContent || el.getAttribute('aria-label') || '').trim()).slice(0, 40));
  }
  // clientWidth is the LAYOUT viewport (390 with a viewport meta, 980 without). window.innerWidth is
  // unreliable on mobile: Chromium shrinks to fit wide content and innerWidth grows with it.
  const root = document.documentElement;
  return {horizontal_overflow: root.scrollWidth > root.clientWidth + 1,
          small_font_share: total ? small / total : 0, font_chars: total,
          small_tap_targets: tiny, tap_target_examples: examples,
          viewport_meta: !!document.querySelector('meta[name=viewport]')};
}
"""


# ----------------------------------------------------------------------------- measurement


def _num(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and v > 0 else None


def build_images(
    raw: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    viewport_w: int,
    dpr: float,
    page_url: str,
) -> list[ImageRecord]:
    by_url: dict[str, dict[str, Any]] = {}
    for e in entries:
        if e.get("resource_type") == "image":
            by_url[str(e["url"])] = e
            by_url.setdefault(str(e.get("resolved_url", "")), e)
    out: list[ImageRecord] = []
    for im in raw:
        src = str(im.get("src", ""))
        w, h = float(im.get("w", 0)), float(im.get("h", 0))
        if not src or src.startswith("data:") or w <= 0 or h <= 0:
            continue
        entry = by_url.get(src)
        nbytes = transfer_bytes(entry) if entry else None
        fmt = served_format(entry.get("head", b""), entry.get("headers", {}).get("content-type")) if entry else None
        above = float(im.get("top", 0)) < float(im.get("vh", 0)) and float(im.get("bottom", 0)) > 0
        cat = image_category(w, above, viewport_w)
        nat_w = _num(im.get("natW"))
        issues = image_issues(fmt, nbytes, cat, nat_w, w, dpr)
        out.append(
            ImageRecord(
                page_url=page_url,
                src=src[:300],
                served_format=fmt,
                bytes=nbytes,
                natural_w=nat_w,
                natural_h=_num(im.get("natH")),
                displayed_w=int(round(w)),
                displayed_h=int(round(h)),
                has_alt=bool(im.get("alt")),
                lazy=bool(im.get("lazy")),
                above_fold=above,
                category=cat,
                issues=issues,
            )
        )
        if len(out) >= MAX_IMAGES:
            break
    return out


async def _measure(renderer: Renderer, url: str, device: Device, timeout_ms: int) -> PagePerf:
    mobile = device == "mobile"
    viewport = MOBILE_VIEWPORT if mobile else DESKTOP_VIEWPORT
    dpr = MOBILE_DPR if mobile else 1.0
    async with renderer.open_page(
        viewport=viewport,
        mobile=mobile,
        device_scale_factor=dpr,
        user_agent=MOBILE_UA if mobile else None,
        blocked_types=(),
        max_requests=400,
        max_bytes=40 * 1024 * 1024,
    ) as sess:
        page = sess.page
        await page.add_init_script(INIT_JS)
        try:
            await page.goto(url, wait_until="load", timeout=timeout_ms)
        except PlaywrightError as exc:
            msg = str(exc)
            if "Timeout" in msg:
                return PagePerf(url=url, device=device, error="render-failed: timeout")
            return PagePerf(
                url=url,
                device=device,
                error=f"render-failed: navigation: {msg.splitlines()[0][:200]}",
            )
        await page.wait_for_timeout(1000)  # let LCP / layout shifts settle before reading them
        m = await page.evaluate(METRICS_JS)
        await page.evaluate(SCROLL_JS)  # trigger lazy loading AFTER the metrics were read
        raw_images = await page.evaluate(IMAGES_JS)
        mobile_info = await page.evaluate(MOBILE_JS) if mobile else None
        entries = list(sess.recorder)

    host = (urlsplit(url).hostname or "").lower()
    res = summarize_resources(entries, host)
    ttfb = res.pop("ttfb_ms") or _num(m.get("responseStart"))
    lcp = m.get("lcp")
    return PagePerf(
        url=url,
        device=device,
        ttfb_ms=ttfb,
        fcp_ms=_num(m.get("fcp")),
        lcp_ms=int(lcp) if isinstance(lcp, (int, float)) and lcp > 0 else None,
        cls=round(float(m.get("cls") or 0.0), 4),
        tbt_ms=int(m.get("tbt") or 0),
        dom_content_loaded_ms=_num(m.get("dcl")),
        load_ms=_num(m.get("load")),
        render_blocking=int(m.get("blocking") or 0),
        mobile=mobile_info,
        images=build_images(raw_images, entries, viewport["width"], dpr, url),
        **res,
    )


async def measure_page(
    renderer: Renderer, url: str, *, device: Device = "desktop", timeout_ms: int = 20000
) -> PagePerf:
    """Lab-measure ``url``. Never raises: any failure is returned in ``PagePerf.error``."""
    try:
        await renderer.judge(url)
    except URLSafetyError as exc:
        return PagePerf(url=url, device=device, error=f"target-rejected: {exc}")
    try:
        return await asyncio.wait_for(_measure(renderer, url, device, timeout_ms), timeout=timeout_ms / 1000 + 25)
    except RenderUnavailable as exc:
        return PagePerf(url=url, device=device, error=f"render-failed: {str(exc)[:200]}")
    except TimeoutError:
        return PagePerf(url=url, device=device, error="render-failed: timeout")
    except Exception as exc:  # noqa: BLE001 - a render failure is a result, not an exception
        return PagePerf(url=url, device=device, error=f"render-failed: {type(exc).__name__}: {str(exc)[:200]}")


LabTarget = str | Page | tuple[str, Device]


def _normalize(target: LabTarget) -> tuple[str, Device]:
    if isinstance(target, Page):
        return target.effective_url, "desktop"
    if isinstance(target, str):
        return target, "desktop"
    return target[0], target[1]


@operation(
    "lab_metrics",
    output=LabMetrics,
    requires=Requirements(network=True, browser=True, extras=["browser"]),
    timeout_s=180,
)
async def lab_metrics(ctx: Context, targets: list[LabTarget]) -> Outcome[LabMetrics]:
    """
    Lab-measure (url, device) pairs; a plain URL or Page is measured on desktop.

    A page that cannot be measured (browser failed to start, navigation error) is kept with its
    ``error`` and its checks are reported as not checked.
    """
    if not playwright_installed():
        ctx.fail(ErrorCode.MISSING_EXTRA, missing="browser")
    renderer = await ctx.session.browser()  # the renderer limits concurrency itself
    pairs = [_normalize(t) for t in targets]
    perfs = list(await asyncio.gather(*(measure_page(renderer, u, device=d) for u, d in pairs)))
    out = perf_checks(perfs)
    return Outcome(LabMetrics(items=perfs), out.findings, out.not_checked)
