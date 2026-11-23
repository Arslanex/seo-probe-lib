"""Language and business type detection, and the ``classify`` operation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seoprobe.core.page import Page
from seoprobe.core.parse.html import parse_html
from seoprobe.core.result import Status
from seoprobe.features.page import Classification, classify
from seoprobe.features.page.classify import detect_business_type, detect_lang
from tests import fakes

U = "https://example.com/"


# --------------------------------------------------------------------------- detect_lang


@pytest.mark.parametrize(
    "html_lang,text,expected",
    [
        ("en-US", "", "en"),
        (" TR ", "anything", "tr"),
        ("pt_BR", "", "pt"),
        (None, "Küçük işletmeler için çevrimiçi satış rehberi ve öneriler", "tr"),
        (None, "A plain English sentence about small online shops", "en"),
        (None, "", "en"),
        (None, "Où est la bibliothèque, s'il vous plaît", "unknown"),  # non-ASCII, not Turkish
    ],
)
def test_detect_lang(html_lang: str | None, text: str, expected: str) -> None:
    assert detect_lang(html_lang, text) == expected


# --------------------------------------------------------------------------- detect_business_type


@pytest.mark.parametrize(
    "html,links,expected",
    [
        ("<button>Add to cart</button> checkout", [], "ecommerce"),
        ("", ["/sepet", "/urun/1"], "ecommerce"),
        ("Start your free trial", ["/pricing"], "saas"),
        ("", ["/blog", "/news/x"], "publisher"),
        ("our work", ["/portfolio"], "agency"),
        ("Call +90 212 555 12 34, address: Main St. get directions", [], "local"),
        ("<p>hello</p>", ["/about"], "other"),
        ("add to cart", [], "other"),  # one signal is not enough
    ],
)
def test_detect_business_type(html: str, links: list[str], expected: str) -> None:
    assert detect_business_type(html, links) == expected


# --------------------------------------------------------------------------- operation


def _expected(page: Page) -> Classification:
    """The audit pipeline's own computation."""
    html = page.content_html
    parsed = parse_html(html, page.effective_url) if html else None
    lang = detect_lang(
        getattr(parsed, "lang", None) or None,
        getattr(parsed, "main_text", "") or html[:3000],
    )
    btype = detect_business_type(html, [ln.href for ln in parsed.links] if parsed else [])
    return Classification(url=page.url, lang=lang, business_type=btype)


@pytest.mark.parametrize(
    "name", ["turkish_page.html", "ssr_article.html", "relevance_tr_nolang.html", "relevance_en_nolang.html"]
)
async def test_classify_matches_the_audit_pipeline(fixtures: Path, name: str) -> None:
    pages = {U: fakes.html_page((fixtures / name).read_text(encoding="utf-8"))}
    async with fakes.session(pages) as s:
        res = await classify(U, session=s)
        page = Page.from_fetch(await s.http.fetch(U))
    assert res.status == Status.OK and res.data == _expected(page)


async def test_classify_turkish_ecommerce_page() -> None:
    html = (
        '<html lang="tr-TR"><body><a href="/sepet">Sepet</a><a href="/urun/1">Ürün</a>'
        "<button>Sepete ekle</button></body></html>"
    )
    async with fakes.session({U: fakes.html_page(html)}) as s:
        res = await classify(U, session=s)
    assert res.data == Classification(url=U, lang="tr", business_type="ecommerce")


async def test_classify_uses_rendered_html() -> None:
    page = Page(url=U, final_url=U, status=200, html="<html><body></body></html>").with_rendered(
        '<html lang="de"><body><a href="/blog">b</a><a href="/news">n</a></body></html>'
    )
    async with fakes.session({}) as s:
        res = await classify(page, session=s)
    assert res.data == Classification(url=U, lang="de", business_type="publisher")


async def test_classify_without_html_falls_back() -> None:
    async with fakes.session({U: (200, {"content-type": "application/pdf"}, b"%PDF")}) as s:
        res = await classify(U, session=s)
    assert res.data == Classification(url=U, lang="en", business_type="other")


async def test_classify_fetch_error_fails() -> None:
    async with fakes.session(errors={U: "unreachable"}) as s:
        res = await classify(U, session=s)
    assert res.status == Status.ERROR and res.data is None


async def test_classify_result_is_json_serializable() -> None:
    async with fakes.session({U: fakes.html_page('<html lang="en"><body>x</body></html>')}) as s:
        res = await classify(U, session=s)
    body = json.loads(res.to_json())
    assert body["operation"] == "classify" and body["data"] == {"url": U, "lang": "en", "business_type": "other"}
