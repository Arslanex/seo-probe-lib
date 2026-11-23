"""Business type and language detection (deterministic, label-only) and the ``classify`` operation."""

from __future__ import annotations

import asyncio
import re

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.html import parse_html

from .models import Classification

_TR_CHARS = re.compile(r"[ışğüöçİŞĞÜÖÇ]")
_PATTERNS: dict[str, tuple[str, ...]] = {
    "ecommerce": (
        "add to cart",
        "sepete ekle",
        "/cart",
        "/sepet",
        "/products",
        "/urun",
        "/collections",
        "checkout",
        "ödeme",
    ),
    "saas": (
        "/pricing",
        "/fiyat",
        "free trial",
        "ücretsiz dene",
        "/integrations",
        "/features",
        "/docs",
        "sign up",
        "kaydol",
    ),
    "publisher": ("/blog", "/articles", "/makale", "/news", "/haber", "published", "yayınlandı"),
    "agency": (
        "/case-studies",
        "/portfolio",
        "/portfolyo",
        "our work",
        "projelerimiz",
        "/industries",
    ),
    "local": (
        "serving ",
        "hizmet bölge",
        "get directions",
        "yol tarifi",
        "google.com/maps",
        "maps.google",
    ),
}
_PHONE = re.compile(r"(?:tel:|\+?\d[\d\s().-]{8,}\d)")


def detect_business_type(html: str, link_hrefs: list[str]) -> str:
    hay = html[:200_000].lower() + " " + " ".join(link_hrefs[:300]).lower()
    scores = {k: sum(1 for p in pats if p in hay) for k, pats in _PATTERNS.items()}
    if _PHONE.search(hay) and ("address" in hay or "adres" in hay):
        scores["local"] += 1
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= 2 else "other"


def detect_lang(html_lang: str | None, text: str) -> str:
    """Primary language subtag; falls back to a Turkish-character heuristic."""
    if html_lang:
        return html_lang.strip().lower().split("-")[0].split("_")[0]
    sample = text[:5000]
    letters = sum(ch.isalpha() for ch in sample) or 1
    if len(_TR_CHARS.findall(sample)) / letters > 0.015:
        return "tr"
    return "en" if sample.isascii() else "unknown"


@operation("classify", output=Classification, requires=Requirements(network=True), timeout_s=30)
async def classify(ctx: Context, target: str | Page) -> Outcome[Classification]:
    """Detect the page's primary language and business type."""
    page = await ctx.page(target)
    html = page.content_html
    parsed = await asyncio.to_thread(parse_html, html, page.effective_url) if html else None
    lang = detect_lang(
        getattr(parsed, "lang", None) or None,
        getattr(parsed, "main_text", "") or html[:3000],
    )
    business_type = detect_business_type(html, [ln.href for ln in parsed.links] if parsed else [])
    return Outcome(data=Classification(url=page.url, lang=lang, business_type=business_type))
