"""
Previews: how the page may look in Google results and in social cards.

SERP part is pure and APPROXIMATE: Google truncates by rendered pixel width, not characters. We
estimate pixel width with a per-character width table (Arial-like, 20 px title font) and cut at
~580 px (desktop title). Descriptions are cut by characters (~160 desktop, ~120 mobile) because
Google's real limit is also pixel based and varies with the letters used. Google may rewrite
titles and snippets entirely; this is a preview of the *tags*, not of the live result.

Social part reads Open Graph / Twitter Card tags and fetches the og:image once (through the
session's safe HTTP client) to read its real pixel size from the file header (PNG, JPEG, WebP, GIF).

``missing-open-graph`` and ``missing-og-image`` already exist as page rules; this operation does
not repeat them.
"""

from __future__ import annotations

import re
import struct
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, Requirements
from seoprobe.core.net.client import HttpClient
from seoprobe.core.page import Page
from seoprobe.core.rules.catalog import make_finding

from .models import Previews, SerpPreview, SocialCard, SocialImage

TITLE_PX_DESKTOP = 580
TITLE_PX_MOBILE = 700
DESC_CHARS_DESKTOP = 160
DESC_CHARS_MOBILE = 120
IMAGE_MAX_FETCH = 2_000_000
IMAGE_HEAVY_BYTES = 5 * 1024 * 1024
MIN_W, MIN_H = 1200, 630
TARGET_RATIO = 1.91
RATIO_TOLERANCE = 0.15  # |ratio / 1.91 - 1|

RULE_IDS = (
    "og-image-unreachable",
    "og-image-too-small",
    "og-image-wrong-ratio",
    "og-image-relative-url",
    "twitter-card-missing",
)

_NARROW = frozenset("iljtf.,;:'!|()[]/\\ ")
_WIDE = frozenset("mwMW@%")
_UPPER_WIDE = frozenset("ABCDEGHKNOQRSUVXYZ")


# ----------------------------------------------------------------------------- SERP (pure)


def char_px(ch: str) -> float:
    """Approximate width of one character in Google's 20 px result title font (Arial-like)."""
    if ch in _NARROW:
        return 5.5
    if ch in _WIDE:
        return 16.5
    if ch.isupper():
        return 14.5 if ch in _UPPER_WIDE else 11.5
    if ch.isdigit():
        return 11.0
    if ord(ch) > 0x2E80:  # CJK and other full-width scripts
        return 20.0
    return 10.3


def text_px(text: str) -> int:
    return round(sum(char_px(c) for c in text))


def _cut_at_word(text: str, end: int) -> str:
    cut = text[:end]
    if end < len(text) and " " in cut and not text[end].isspace():
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-–—|") + "…"


def truncate_px(text: str, limit_px: int) -> tuple[str, bool]:
    text = " ".join(text.split())
    if text_px(text) <= limit_px:
        return text, False
    total = 0.0
    end = 0
    for i, ch in enumerate(text):
        total += char_px(ch)
        if total > limit_px - char_px("…"):
            break
        end = i + 1
    return _cut_at_word(text, end), True


def truncate_chars(text: str, limit: int) -> tuple[str, bool]:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text, False
    return _cut_at_word(text, limit - 1), True


def display_url(url: str) -> str:
    """Google's breadcrumb style: ``example.com › blog › post``."""
    p = urlsplit(url)
    host = (p.hostname or "").removeprefix("www.")
    parts = [unquote(s) for s in p.path.split("/") if s]
    shown = parts[:3]
    tail = " › …" if len(parts) > 3 else ""
    return " › ".join([host, *shown]) + tail


def build_serp(url: str, title: str, description: str) -> list[SerpPreview]:
    out: list[SerpPreview] = []
    for device, tpx, dch in (
        ("desktop", TITLE_PX_DESKTOP, DESC_CHARS_DESKTOP),
        ("mobile", TITLE_PX_MOBILE, DESC_CHARS_MOBILE),
    ):
        t_shown, t_cut = truncate_px(title, tpx)
        d_shown, d_cut = truncate_chars(description, dch)
        out.append(
            SerpPreview(
                device=device,
                display_url=display_url(url),
                title=title,
                title_shown=t_shown,
                title_truncated=t_cut,
                title_px=text_px(title),
                description=description,
                description_shown=d_shown,
                description_truncated=d_cut,
                description_missing=not description.strip(),
            )
        )
    return out


# ----------------------------------------------------------------------------- images (pure)


def image_info(data: bytes) -> dict[str, Any] | None:
    """``{format, width, height}`` from the file header (PNG, JPEG, WebP, GIF), or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return {"format": "png", "width": w, "height": h}
    if data[:4] == b"GIF8" and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return {"format": "gif", "width": w, "height": h}
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg(data)
    return None


def _webp(d: bytes) -> dict[str, Any] | None:
    kind = d[12:16]
    if kind == b"VP8 " and len(d) >= 30 and d[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", d[26:30])
        return {"format": "webp", "width": w & 0x3FFF, "height": h & 0x3FFF}
    if kind == b"VP8L" and len(d) >= 25 and d[20] == 0x2F:
        b = d[21:25]
        w = 1 + (((b[1] & 0x3F) << 8) | b[0])
        h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return {"format": "webp", "width": w, "height": h}
    if kind == b"VP8X" and len(d) >= 30:
        w = 1 + int.from_bytes(d[24:27], "little")
        h = 1 + int.from_bytes(d[27:30], "little")
        return {"format": "webp", "width": w, "height": h}
    return None


def _jpeg(d: bytes) -> dict[str, Any] | None:
    i, n = 2, len(d)
    while i + 9 < n:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
            i += 1 if marker == 0xFF else 2
            continue
        seglen = struct.unpack(">H", d[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", d[i + 5 : i + 9])
            return {"format": "jpeg", "width": w, "height": h}
        i += 2 + max(seglen, 2)
    return None


# ----------------------------------------------------------------------------- analysis


def _is_absolute(url: str) -> bool:
    return bool(re.match(r"^https?://", url.strip(), re.I))


def _social_tags(meta: list[tuple[str, str, str]]) -> tuple[dict[str, str], dict[str, str]]:
    og: dict[str, str] = {}
    tw: dict[str, str] = {}
    for name, prop, content in meta:
        key = prop or name
        if key.startswith("og:") and key not in og:
            og[key] = content[:300]
        elif (name.startswith("twitter:") or prop.startswith("twitter:")) and key not in tw:
            tw[key] = content[:300]
    return og, tw


async def _inspect_image(http: HttpClient, base_url: str, raw_url: str) -> SocialImage:
    img = SocialImage(url=raw_url[:300], absolute=_is_absolute(raw_url))
    target = urljoin(base_url, raw_url.strip())
    try:
        res = await http.fetch_bytes(target, max_bytes=IMAGE_MAX_FETCH)
    except Exception as exc:  # noqa: BLE001 - one image must not break the previews
        img.reachable, img.error = False, f"fetch-failed: {type(exc).__name__}"
        return img
    img.status = res.status
    if res.error or res.status is None or res.status >= 400:
        img.reachable = False
        img.error = res.error or f"http-{res.status}"
        return img
    info = image_info(res.body)
    if info is None:
        img.reachable, img.error = False, "not-an-image"
        return img
    img.reachable = True
    img.format, img.width, img.height = info["format"], info["width"], info["height"]
    if img.width and img.height:
        img.ratio = round(img.width / img.height, 2)
    clen = res.headers.get("content-length", "")
    img.bytes = int(clen) if clen.isdigit() else len(res.body)
    img.bytes_is_lower_bound = res.truncated and not clen.isdigit()
    img.too_heavy = img.bytes > IMAGE_HEAVY_BYTES or res.truncated
    return img


def social_findings(card: SocialCard, url: str) -> list[Finding]:
    out: list[Finding] = []
    img = card.image
    if img.url:
        if not img.absolute:
            out.append(make_finding("og-image-relative-url", url=url, evidence=img.url))
        if img.reachable is False:
            out.append(make_finding("og-image-unreachable", url=url, evidence=img.error or img.url))
        elif img.width and img.height:
            if img.width < MIN_W or img.height < MIN_H:
                out.append(make_finding("og-image-too-small", url=url, evidence=f"{img.width}x{img.height}"))
            if img.ratio and abs(img.ratio / TARGET_RATIO - 1) > RATIO_TOLERANCE:
                out.append(make_finding("og-image-wrong-ratio", url=url, evidence=f"{img.width}x{img.height}"))
    if "twitter:card" not in card.twitter:
        out.append(make_finding("twitter-card-missing", url=url))
    return out


@operation("previews", output=Previews, requires=Requirements(network=True), timeout_s=30)
async def previews(ctx: Context, target: str | Page) -> Outcome[Previews]:
    """Google SERP preview (desktop and mobile), Open Graph / Twitter card and og:image check."""
    page = await ctx.page(target)
    p = page.parsed
    if p is None:
        return Outcome(data=Previews(), not_checked=[ctx.not_checked("previews", "parse-failed", page.url)])
    data = Previews(serp=build_serp(page.url, p.title, p.meta_description))
    if not p.meta_description.strip():
        data.notes.append("description-missing-google-writes-snippet")
    data.notes.append("pixel-width-is-approximate")
    og, tw = _social_tags(p.meta_tags)
    card = SocialCard(og=og, twitter=tw)
    card.missing_og = [k for k in ("og:title", "og:description", "og:image") if k not in og]
    if og.get("og:image"):
        card.image = await _inspect_image(ctx.http, page.url, og["og:image"])
    data.social = card
    return Outcome(data=data, findings=social_findings(card, page.url))
