"""
Copy-paste JSON-LD suggestions for what a page is missing.

Suggestions are adapted from the upstream project ``skills/seo-schema/SKILL.md`` (Generation,
templates, status tables), ``skills/seo/references/schema-types.md`` and
``scripts/schema_ecommerce_validate.py`` (merchant listing minimum). MIT.

The suggestions are DATA, not findings and not part of the page: they are never counted as
markup the site has. Existing markup is judged by ``checks`` (which also own the
Product/Offer missing-field rules); ``suggest_schema`` raises no finding of its own and only
records the gaps of an existing Product as data. Rules of generation:

* only values read from the page are filled in; everything else is a marked placeholder and the
  suggestion is ``complete=False`` with the fields listed in ``missing_fields``;
* HowTo, new FAQPage and every type the reference calls retired are never suggested;
* every suggestion is run through the JSON-LD checks (placeholders replaced by well-formed
  fillers) so a generated block cannot fail our own rules;
* no ``AggregateRating``/``Review`` is ever invented.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.html import ParsedPage, parse_html

from ..models import SchemaSuggestion, SchemaSuggestions
from .checks import _present, _types_of, _walk, run_schema_checks

# =============================================================================== templates
#
# JSON-LD builders adapted from the upstream project ``skills/seo-schema/SKILL.md`` ("Common
# Schema Templates", "Generation": *include only truthful, verifiable data; use placeholders
# clearly marked for the user to fill*), ``skills/seo/references/schema-types.md`` (key
# properties per type) and ``scripts/schema_generate.py`` (``@context: https://schema.org``,
# absolute URLs, ``_strip_nones``).
#
# Only values read from the page are filled in. Everything else is an explicit placeholder
# ``<doldurun: ...>`` / ``<fill in: ...>`` that the user must replace; ``is_placeholder`` finds
# them. Types Google retired (HowTo, FAQ rich results, ...) have no builder on purpose.

CONTEXT = "https://schema.org"
PLACEHOLDER_OPEN = {"tr": "<doldurun: ", "en": "<fill in: "}
_PLACEHOLDER_MARKS = tuple(PLACEHOLDER_OPEN.values())

# Types that are never suggested (the upstream project schema-types.md "Deprecated: Never
# recommend" and the FAQPage decision). Used as a hard filter and by the tests.
NEVER_SUGGEST = frozenset(
    {
        "HowTo",
        "FAQPage",
        "SpecialAnnouncement",
        "CourseInfo",
        "EstimatedSalary",
        "LearningVideo",
        "ClaimReview",
        "VehicleListing",
    }
)


def placeholder(what: str, lang: str = "en") -> str:
    return f"{PLACEHOLDER_OPEN.get(lang, PLACEHOLDER_OPEN['en'])}{what}>"


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_PLACEHOLDER_MARKS)


def placeholder_paths(data: Any, prefix: str = "") -> list[str]:
    """Dotted paths of every placeholder value in a JSON-LD structure."""
    out: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k.startswith("@"):
                continue
            out.extend(placeholder_paths(v, f"{prefix}{k}."))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out.extend(placeholder_paths(v, f"{prefix}{i}."))
    elif is_placeholder(data):
        out.append(prefix.rstrip("."))
    return out


def strip_nones(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if v not in (None, "", [], {})}


def _ph(what: str, lang: str) -> str:
    return placeholder(what, lang)


def organization(*, name: str | None, url: str, logo: str | None, lang: str = "en") -> dict[str, Any]:
    return {
        "@context": CONTEXT,
        "@type": "Organization",
        "name": name or _ph("organization name" if lang == "en" else "kuruluş adı", lang),
        "url": url,
        "logo": logo or _ph("absolute logo URL" if lang == "en" else "logo URL'si (mutlak)", lang),
        "sameAs": [_ph("social profile URL" if lang == "en" else "sosyal profil URL'si", lang)],
    }


def website(*, name: str | None, url: str, lang: str = "en") -> dict[str, Any]:
    return {
        "@context": CONTEXT,
        "@type": "WebSite",
        "name": name or _ph("site name" if lang == "en" else "site adı", lang),
        "url": url,
    }


def article(
    *,
    kind: str,
    headline: str,
    url: str,
    description: str | None,
    image: str | None,
    author: str | None,
    published: str | None,
    publisher: str | None,
    lang: str = "en",
) -> dict[str, Any]:
    tr = lang == "tr"
    return strip_nones(
        {
            "@context": CONTEXT,
            "@type": kind,
            "headline": headline[:110],
            "description": description,
            "mainEntityOfPage": url,
            "image": image or _ph("absolute image URL" if not tr else "görsel URL'si (mutlak)", lang),
            "author": {
                "@type": "Person",
                "name": author or _ph("author name" if not tr else "yazar adı", lang),
            },
            "datePublished": published
            or _ph(
                "ISO 8601 date, e.g. 2026-01-31" if not tr else "ISO 8601 tarih, örn. 2026-01-31",
                lang,
            ),
            "publisher": {
                "@type": "Organization",
                "name": publisher or _ph("publisher name" if not tr else "yayıncı adı", lang),
            },
        }
    )


def product(
    *,
    name: str,
    url: str,
    description: str | None,
    image: str | None,
    price: str | None,
    currency: str | None,
    lang: str = "en",
) -> dict[str, Any]:
    tr = lang == "tr"
    return strip_nones(
        {
            "@context": CONTEXT,
            "@type": "Product",
            "name": name,
            "description": description,
            "image": image or _ph("absolute image URL" if not tr else "görsel URL'si (mutlak)", lang),
            "sku": _ph("product SKU" if not tr else "ürün SKU", lang),
            "offers": {
                "@type": "Offer",
                "url": url,
                "price": price or _ph("price, digits only" if not tr else "fiyat (yalnız rakam)", lang),
                "priceCurrency": currency
                or _ph(
                    "ISO 4217 currency, e.g. USD" if not tr else "ISO 4217 para birimi, örn. TRY",
                    lang,
                ),
                "availability": _ph(
                    "https://schema.org/InStock or OutOfStock"
                    if not tr
                    else "https://schema.org/InStock veya OutOfStock",
                    lang,
                ),
            },
        }
    )


def local_business(*, name: str | None, url: str, telephone: str | None, lang: str = "en") -> dict[str, Any]:
    tr = lang == "tr"
    return strip_nones(
        {
            "@context": CONTEXT,
            "@type": "LocalBusiness",
            "name": name or _ph("business name" if not tr else "işletme adı", lang),
            "url": url,
            "telephone": telephone or _ph("phone number" if not tr else "telefon numarası", lang),
            "address": {
                "@type": "PostalAddress",
                "streetAddress": _ph("street address" if not tr else "açık adres", lang),
                "addressLocality": _ph("city" if not tr else "şehir", lang),
                "addressCountry": _ph("ISO 3166-1 country code" if not tr else "ISO 3166-1 ülke kodu", lang),
            },
        }
    )


def breadcrumb_list(items: list[tuple[str, str]]) -> dict[str, Any]:
    """``items``: (name, absolute url) from the home page down to the current page."""
    return {
        "@context": CONTEXT,
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": n, "item": u} for i, (n, u) in enumerate(items, 1)
        ],
    }


# =============================================================================== suggestions

MAX_PAGES = 25
MAX_ITEMS_PER_PAGE = 5

_WARNING = {
    "en": "Fill in every <fill in: ...> placeholder with real values before publishing.",
    "tr": "Yayınlamadan önce her <doldurun: ...> yer tutucusunu gerçek değerle değiştirin.",
}
# Conservative notes taken from schema-types.md; nothing here promises a ranking or a result.
_NOTES: dict[str, dict[str, str]] = {
    "Organization": {
        "en": "Helps search engines understand the organization's name, logo and profiles. No rich result is guaranteed.",
        "tr": "Arama motorlarının kuruluşun adını, logosunu ve profillerini anlamasına yardımcı olur. Zengin sonuç garanti edilmez.",
    },
    "WebSite": {
        "en": "Names the site for search engines. A SearchAction gives no sitelinks search box benefit, so none is suggested.",
        "tr": "Siteyi arama motorlarına adlandırır. SearchAction'ın site içi arama kutusu faydası yoktur, bu yüzden önerilmedi.",
    },
    "Article": {
        "en": "Helps Google understand headline, author and dates. Google lists no dedicated rich result you can count on.",
        "tr": "Google'ın başlık, yazar ve tarihleri anlamasına yardımcı olur. Güvenebileceğiniz özel bir zengin sonuç yoktur.",
    },
    "Product": {
        "en": "Product snippets and merchant listings need name, image and an offer with price, currency and availability that match the page.",
        "tr": "Ürün snippet'ları ve satıcı listeleri, sayfayla uyuşan ad, görsel ve fiyat, para birimi, stok durumu olan bir teklif ister.",
    },
    "LocalBusiness": {
        "en": "Business details must match what the page shows (name, address, phone). Eligibility for local features is not guaranteed.",
        "tr": "İşletme bilgileri sayfada görünenle (ad, adres, telefon) uyuşmalı. Yerel özelliklere uygunluk garanti edilmez.",
    },
    "BreadcrumbList": {
        "en": "Can show the breadcrumb trail in results (desktop only since January 2025).",
        "tr": "Sonuçlarda gezinme yolunu gösterebilir (Ocak 2025'ten beri yalnız masaüstünde).",
    },
}
_REASONS: dict[str, dict[str, str]] = {
    "Organization": {
        "en": "Home page without Organization markup.",
        "tr": "Ana sayfada Organization işaretlemesi yok.",
    },
    "WebSite": {
        "en": "Home page without WebSite markup.",
        "tr": "Ana sayfada WebSite işaretlemesi yok.",
    },
    "Article": {
        "en": "The page looks like an article or blog post (type, byline/date metadata or blog path) and has no Article markup.",
        "tr": "Sayfa bir makale/blog yazısına benziyor (tür, yazar/tarih üst verisi veya blog yolu) ve Article işaretlemesi yok.",
    },
    "Product": {
        "en": "The page looks like a product page (product metadata, or a price with an add-to-cart control) and has no Product markup.",
        "tr": "Sayfa bir ürün sayfasına benziyor (ürün üst verisi veya sepete ekle ile fiyat) ve Product işaretlemesi yok.",
    },
    "LocalBusiness": {
        "en": "The page shows a phone number and an address but no LocalBusiness markup (heuristic).",
        "tr": "Sayfada telefon ve adres var ama LocalBusiness işaretlemesi yok (sezgisel).",
    },
    "BreadcrumbList": {
        "en": "The page is two or more levels deep and has no BreadcrumbList. Names are derived from the URL: check them.",
        "tr": "Sayfa iki veya daha fazla seviye derinde ve BreadcrumbList yok. Adlar URL'den türetildi: kontrol edin.",
    },
}

_PHONE = re.compile(r"(?<![\w])(\+?\d[\d\s().-]{7,16}\d)(?![\w])")
_ADDRESS = re.compile(
    r"\b(?:street|st\.|avenue|ave\.|road|rd\.|address|cad\.|cadde|sok\.|sokak|mah\.|mahalle|adres)\b",
    re.I,
)
_CART = re.compile(r"add to (?:cart|basket|bag)|buy now|sepete ekle|satın al", re.I)
_PRICE = re.compile(r"(?:[$€£₺]\s?\d[\d.,]*|\d[\d.,]*\s?(?:₺|TL|TRY|USD|EUR|€|\$))")
_BLOG_PATH = re.compile(r"/(?:blog|news|haber|haberler|makale|yazi|yazilar|articles?)(?:/|$)", re.I)
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY"}


def _meta(parsed: ParsedPage, *keys: str) -> str:
    wanted = {k.lower() for k in keys}
    for name, prop, content in parsed.meta_tags:
        if (name.lower() in wanted or prop.lower() in wanted) and content.strip():
            return content.strip()
    return ""


def _abs(value: str, base: str) -> str | None:
    if not value:
        return None
    u = urljoin(base, value)
    return u if u.lower().startswith(("http://", "https://")) else None


def _path(url: str) -> str:
    return urlparse(url).path or "/"


def _site_name(parsed: ParsedPage) -> str | None:
    return parsed.og.get("og:site_name", "").strip()[:100] or None


def _title(parsed: ParsedPage) -> str:
    return (parsed.h1[0] if parsed.h1 else parsed.title).strip()


def _present_types(parsed: ParsedPage) -> set[str]:
    return set(parsed.schema_types)


def _is_article(parsed: ParsedPage, url: str) -> bool:
    if parsed.og.get("og:type", "").lower() == "article":
        return True
    if _meta(parsed, "article:published_time"):
        return True
    return bool(_BLOG_PATH.search(_path(url)) and parsed.h1 and parsed.word_count >= 300)


def _is_product(parsed: ParsedPage) -> bool:
    if parsed.og.get("og:type", "").lower() in ("product", "og:product"):
        return True
    if _meta(parsed, "product:price:amount", "og:price:amount"):
        return True
    text = parsed.visible_text[:20000]
    return bool(_CART.search(text) and _PRICE.search(text))


def _telephone(parsed: ParsedPage) -> str | None:
    m = _PHONE.search(parsed.visible_text[:20000])
    return m.group(1).strip() if m else None


def _breadcrumb_items(url: str, parsed: ParsedPage, lang: str) -> list[tuple[str, str]]:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    segs = [s for s in p.path.split("/") if s]
    items = [("Ana sayfa" if lang == "tr" else "Home", origin + "/")]
    acc = ""
    for i, seg in enumerate(segs):
        acc += f"/{seg}"
        last = i == len(segs) - 1
        name = _title(parsed) if last and _title(parsed) else re.sub(r"[-_]+", " ", seg).strip().title()
        items.append((name[:100], f"{origin}{acc}" + ("" if last else "/")))
    return items


_FILL_URL_KEYS = {"url", "image", "logo", "sameas", "item", "mainentityofpage"}
_FILL_DATE_KEYS = {"datepublished", "datemodified"}


def _fill(data: Any, key: str = "") -> Any:
    """Replace placeholders by well-formed fillers so the structure can be validated."""
    if isinstance(data, dict):
        return {k: _fill(v, k) for k, v in data.items()}
    if isinstance(data, list):
        return [_fill(v, key) for v in data]
    if is_placeholder(data):
        lk = key.lower()
        if lk in _FILL_URL_KEYS:
            return "https://example.org/x"
        if lk in _FILL_DATE_KEYS:
            return "2026-01-01"
        if lk == "price":
            return "10"
        return "Example"
    return data


def validate_json_ld(text: str) -> list[str]:
    """Rule ids our own schema checks raise on ``text`` (placeholders filled in); [] = clean."""
    try:
        data = json.loads(text)
    except ValueError:
        return ["invalid-jsonld"]
    doc = json.dumps(_fill(data))
    # </script> inside a value cannot occur: we escape "<" when serialising for the page.
    safe = doc.replace("<", "\\u003c")
    html = f'<html><head><script type="application/ld+json">{safe}</script></head></html>'
    parsed = parse_html(html, "https://example.org/")
    return sorted({f.id for f in run_schema_checks(parsed, "https://example.org/").findings})


def _serialise(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _suggestion(url: str, kind: str, data: dict[str, Any], lang: str) -> SchemaSuggestion:
    assert kind not in NEVER_SUGGEST
    missing = placeholder_paths(data)
    text = _serialise(data)
    return SchemaSuggestion(
        url=url,
        type=kind,
        reason=_REASONS[kind if kind != "BlogPosting" else "Article"][lang],
        json_ld=text,
        complete=not missing,
        missing_fields=missing,
        validation_issues=validate_json_ld(text),
        rich_result_note=_NOTES[kind if kind != "BlogPosting" else "Article"][lang],
        warning=_WARNING[lang] if missing else "",
    )


def suggest_for_page(page: Page, lang: str = "en") -> list[SchemaSuggestion]:
    parsed = page.parsed
    if parsed is None or page.error or page.status is None or page.status >= 400:
        return []
    url = page.effective_url
    present = _present_types(parsed)
    out: list[SchemaSuggestion] = []
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    description = parsed.meta_description.strip()[:300] or None
    image = _abs(parsed.og.get("og:image", ""), url)
    site = _site_name(parsed)

    if _path(url) == "/":
        if not present & {"Organization", "LocalBusiness"}:
            out.append(
                _suggestion(
                    url,
                    "Organization",
                    organization(name=site, url=origin + "/", logo=None, lang=lang),
                    lang,
                )
            )
        if "WebSite" not in present:
            out.append(_suggestion(url, "WebSite", website(name=site, url=origin + "/", lang=lang), lang))

    if not present & {"Article", "BlogPosting", "NewsArticle"} and _is_article(parsed, url) and _title(parsed):
        kind = "BlogPosting" if "/blog" in _path(url).lower() else "Article"
        published = _meta(parsed, "article:published_time")
        data = article(
            kind=kind,
            headline=_title(parsed),
            url=url,
            description=description,
            image=image,
            author=_meta(parsed, "author", "article:author") or None,
            published=published[:25] if re.match(r"^\d{4}-\d{2}-\d{2}", published) else None,
            publisher=site,
            lang=lang,
        )
        out.append(_suggestion(url, kind, data, lang))

    if "Product" not in present and _is_product(parsed) and _title(parsed):
        raw_price = _meta(parsed, "product:price:amount", "og:price:amount")
        price = raw_price if re.fullmatch(r"\d+(?:[.,]\d{1,2})?", raw_price) else None
        cur = _meta(parsed, "product:price:currency", "og:price:currency").upper()
        currency = cur if re.fullmatch(r"[A-Z]{3}", cur) else None
        data = product(
            name=_title(parsed),
            url=url,
            description=description,
            image=image,
            price=price.replace(",", ".") if price else None,
            currency=currency,
            lang=lang,
        )
        out.append(_suggestion(url, "Product", data, lang))

    if (
        not present & {"LocalBusiness", "Organization"}
        and _ADDRESS.search(parsed.visible_text[:20000])
        and (tel := _telephone(parsed))
        and not _is_product(parsed)
    ):
        out.append(
            _suggestion(
                url,
                "LocalBusiness",
                local_business(name=site, url=url, telephone=tel, lang=lang),
                lang,
            )
        )

    if "BreadcrumbList" not in present and len([s for s in _path(url).split("/") if s]) >= 2:
        out.append(
            _suggestion(
                url,
                "BreadcrumbList",
                breadcrumb_list(_breadcrumb_items(url, parsed, lang)),
                lang,
            )
        )

    return out[:MAX_ITEMS_PER_PAGE]


_OFFER_FIELDS = ("price", "priceCurrency", "availability")


def product_gaps(page: Page) -> list[str]:
    """Merchant-listing fields missing from Product markup that already exists on the page."""
    parsed = page.parsed
    if parsed is None:
        return []
    missing: list[str] = []
    for block in parsed.jsonld:
        if block.error or block.data is None:
            continue
        nodes: list[dict[str, Any]] = []
        _walk(block.data, nodes)
        for node in nodes:
            if "Product" not in _types_of(node):
                continue
            for prop in ("name", "image"):
                if not _present(node, prop):
                    missing.append(prop)
            offers = node.get("offers")
            offer_list = [o for o in (offers if isinstance(offers, list) else [offers]) if isinstance(o, dict)]
            if not offer_list:
                missing.append("offers")
                continue
            for prop in _OFFER_FIELDS:
                if not any(_present(o, prop) for o in offer_list):
                    missing.append(f"offers.{prop}")
    return list(dict.fromkeys(missing))


@operation("suggest_schema", output=SchemaSuggestions, requires=Requirements(network=True), timeout_s=15)
async def suggest_schema(
    ctx: Context, target: str | Page, *, pages: list[Page] | None = None
) -> Outcome[SchemaSuggestions]:
    """Copy-paste JSON-LD for the types a page is missing, with rich result notes."""
    page = await ctx.page(target)
    lang = "tr" if ctx.lang == "tr" else "en"
    result = SchemaSuggestions()
    findings: list[Finding] = []  # none: the page rules live in the schema checks
    seen: set[tuple[str, str]] = set()
    examined = 0
    for p in (pages if pages is not None else [page])[:MAX_PAGES]:
        if p.parsed is None:
            continue
        examined += 1
        gaps = product_gaps(p)
        if gaps:
            result.product_gaps.append({"url": p.url, "missing": gaps})
        for s in suggest_for_page(p, lang):
            # Organization/WebSite are site-level: one suggestion is enough
            if s.type in ("Organization", "WebSite", "LocalBusiness") and (s.type, "site") in seen:
                continue
            if s.type in ("Organization", "WebSite"):
                seen.add((s.type, "site"))
            result.items.append(s)
    result.pages_examined = examined
    if not result.items:
        result.notes.append("no-missing-types-detected")
    result.notes.append("suggestions-are-not-part-of-the-page")
    return Outcome(data=result, findings=findings)
