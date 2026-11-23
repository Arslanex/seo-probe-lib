"""Schema suggestions: what is suggested, that it validates, what is never suggested."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest

from seoprobe.core.page import Page
from seoprobe.core.rules.catalog import rules
from seoprobe.features.content import FEATURE, suggest_schema
from tests import fakes
from tests.features.content.support import HOST, page

# the suggestions module (templates included)
S = importlib.import_module("seoprobe.features.content.schema.suggest")
T = S  # the JSON-LD templates live in the same module


async def _run(pages: list[Page]) -> Any:
    async with fakes.session() as s:
        res = await suggest_schema(pages[0], pages=pages, session=s)
    assert res.ok
    return res


def html(body: str = "", head: str = "", lang: str = "en") -> str:
    return f'<html lang="{lang}"><head><title>Acme Shop</title>{head}</head><body><main>{body}</main></body></html>'


HOME_HEAD = '<meta property="og:site_name" content="Acme"><meta property="og:image" content="/logo.png">'


def types(items: list[S.SchemaSuggestion]) -> list[str]:
    return [i.type for i in items]


def test_registered_and_rule_ids_do_not_collide() -> None:
    RULES = rules()
    assert "suggest_schema" in {op.name for op in FEATURE.operations}
    # the Product gap rules belong to schema_checks alone: no second rule for the same fields
    assert "product-schema-incomplete" not in RULES
    assert "schema-product-missing-fields" in RULES


def test_home_gets_organization_and_website_with_marked_placeholders() -> None:
    items = S.suggest_for_page(page("/", html("<h1>Acme</h1>", HOME_HEAD)))
    assert types(items) == ["Organization", "WebSite"]
    org, site = items
    data = json.loads(org.json_ld)
    assert data["name"] == "Acme" and data["url"] == f"{HOST}/"
    assert T.is_placeholder(data["logo"])  # a logo cannot be read from the page: marked
    assert org.complete is False and {"logo", "sameAs.0"} <= set(org.missing_fields)
    assert "fill in" in org.warning
    assert site.complete is True and site.missing_fields == [] and site.warning == ""


def test_existing_types_are_not_suggested_again() -> None:
    ld = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"A","url":"https://example.com/","logo":"https://example.com/l.png"}</script>'
    items = S.suggest_for_page(page("/", html("", HOME_HEAD + ld)))
    assert types(items) == ["WebSite"]


def test_article_signals_and_fields() -> None:
    head = (
        '<meta property="og:type" content="article">'
        '<meta property="article:published_time" content="2026-03-04T10:00:00+00:00">'
        '<meta name="author" content="Ada Lovelace"><meta name="description" content="About x.">'
        '<meta property="og:image" content="https://example.com/i.jpg">'
        '<meta property="og:site_name" content="Acme">'
    )
    p = page("/blog/how-to-x", html("<h1>How to X</h1>", head))
    items = S.suggest_for_page(p)
    art = next(i for i in items if i.type == "BlogPosting")
    d = json.loads(art.json_ld)
    assert d["author"]["name"] == "Ada Lovelace"
    assert d["datePublished"].startswith("2026-03-04")
    assert d["headline"] == "How to X" and d["image"] == "https://example.com/i.jpg"
    assert art.complete is True and art.validation_issues == []
    assert "BreadcrumbList" in types(items)


def test_product_uses_only_reliable_price_sources() -> None:
    head = '<meta property="og:type" content="product"><meta property="product:price:amount" content="19,90"><meta property="product:price:currency" content="try">'
    items = S.suggest_for_page(page("/p/shoe", html("<h1>Shoe</h1>", head)))
    prod = next(i for i in items if i.type == "Product")
    d = json.loads(prod.json_ld)
    assert d["offers"]["price"] == "19.90" and d["offers"]["priceCurrency"] == "TRY"
    assert T.is_placeholder(d["offers"]["availability"])  # stock status is not guessed
    assert T.is_placeholder(d["sku"])
    assert "offers.availability" in prod.missing_fields and prod.complete is False


def test_price_in_text_alone_is_not_copied() -> None:
    p = page("/p/shoe", html("<h1>Shoe</h1><p>$25.00</p><button>Add to cart</button>"))
    prod = next(i for i in S.suggest_for_page(p) if i.type == "Product")
    d = json.loads(prod.json_ld)
    assert T.is_placeholder(d["offers"]["price"])  # a text price is not trusted


def test_local_business_needs_address_words_and_phone() -> None:
    body = "<h1>Acme Cafe</h1><p>Address: 5 Main Street, Springfield. Phone +1 555 123 4567</p>"
    items = S.suggest_for_page(page("/contact", html(body)))
    lb = next(i for i in items if i.type == "LocalBusiness")
    assert json.loads(lb.json_ld)["telephone"] == "+1 555 123 4567"
    assert S.suggest_for_page(page("/contact", html("<h1>Hi</h1><p>Phone +1 555 123 4567</p>"))) == []


def test_breadcrumb_for_deep_pages_names_derived_from_url() -> None:
    items = S.suggest_for_page(page("/shop/shoes/red-boots", html("<h1>Red Boots</h1>")))
    bc = json.loads(next(i for i in items if i.type == "BreadcrumbList").json_ld)
    names = [e["name"] for e in bc["itemListElement"]]
    assert names == ["Home", "Shop", "Shoes", "Red Boots"]
    assert bc["itemListElement"][-1]["item"] == f"{HOST}/shop/shoes/red-boots"
    assert S.suggest_for_page(page("/shop", html("<h1>Shop</h1>"))) == []


def test_turkish_output_language() -> None:
    items = S.suggest_for_page(page("/", html("<h1>x</h1>")), "tr")
    org = items[0]
    assert "<doldurun:" in org.json_ld and "doldurun" in org.warning
    assert "Organization" in org.reason or "ana sayfa" in org.reason.lower()


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_every_generated_block_passes_our_own_schema_checks(lang: str) -> None:
    pages = [
        page("/", html("<h1>Acme</h1>", HOME_HEAD)),
        page(
            "/blog/a/b",
            html(
                "<h1>Post</h1>",
                '<meta property="og:type" content="article"><meta property="og:image" content="https://example.com/i.jpg">',
            ),
        ),
        page(
            "/p/x",
            html("<h1>X</h1>", '<meta property="og:type" content="product">'),
        ),
        page("/contact", html("<h1>C</h1><p>Address: 1 Main Street. +90 212 555 1234</p>")),
    ]
    seen: set[str] = set()
    for p in pages:
        for s in S.suggest_for_page(p, lang):
            seen.add(s.type)
            assert s.validation_issues == [], (s.type, s.validation_issues)
            data = json.loads(s.json_ld)
            assert data["@context"] == "https://schema.org"  # https, never http
    assert {
        "Organization",
        "WebSite",
        "BlogPosting",
        "Product",
        "LocalBusiness",
        "BreadcrumbList",
    } <= seen


def test_validation_catches_a_broken_block() -> None:
    bad = json.dumps({"@context": "http://schema.org", "@type": "Product", "name": "x"})
    assert "schema-context-not-https" in S.validate_json_ld(bad)
    assert S.validate_json_ld("{nope") == ["invalid-jsonld"]


def test_howto_faq_and_retired_types_are_never_suggested() -> None:
    body = (
        "<h1>How to bake</h1><h2>Frequently asked questions</h2><p>Step 1: mix. Step 2: bake.</p>"
        "<details><summary>Q?</summary>A.</details>"
    )
    head = '<meta property="og:type" content="article">'
    for path in ("/", "/blog/how-to-bake", "/faq", "/how-to/bake"):
        for s in S.suggest_for_page(page(path, html(body, head))):
            assert s.type not in T.NEVER_SUGGEST
            assert "HowTo" not in s.json_ld and "FAQPage" not in s.json_ld
    for t in ("HowTo", "FAQPage", "SpecialAnnouncement", "ClaimReview", "VehicleListing"):
        assert t in T.NEVER_SUGGEST
    assert not hasattr(T, "howto") and not hasattr(T, "faq_page")


def test_product_gaps_only_for_existing_products() -> None:
    ld = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"X","offers":{"@type":"Offer","price":"5"}}</script>'
    gaps = S.product_gaps(page("/p/x", html("", ld)))
    assert "image" in gaps and "offers.priceCurrency" in gaps
    ok = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"X","image":"https://e.com/i.jpg","offers":{"@type":"Offer","price":"5","priceCurrency":"USD","availability":"https://schema.org/InStock"}}</script>'
    assert S.product_gaps(page("/p/x", html("", ok))) == []
    assert S.product_gaps(page("/", html("", ""))) == []
    nooffer = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"X","image":"https://e.com/i.jpg"}</script>'
    assert "offers" in S.product_gaps(page("/p", html("", nooffer)))


async def test_module_dedupes_site_level_types_and_reports_findings() -> None:
    pages = [
        page("/", html("<h1>A</h1>", HOME_HEAD)),
        page("/about", html("<h1>B</h1>")),
        page(
            "/p/x",
            html(
                "",
                '<script type="application/ld+json">{"@type":"Product","name":"X"}</script>',
            ),
        ),
    ]
    res = await _run(pages)
    assert res.operation == "suggest_schema"
    assert types(res.data.items).count("Organization") == 1
    assert res.findings == []  # the module raises no finding; the gap is data
    assert res.data.product_gaps and res.data.product_gaps[0]["url"].endswith("/p/x")
    assert "suggestions-are-not-part-of-the-page" in res.data.notes


async def test_hostile_pages_do_not_crash() -> None:
    evil = html(
        "<h1>" + "<b>" * 3000 + "</h1>" + "$" * 50000,
        '<meta property="og:type" content="product"><meta property="product:price:amount" content="9999999999999999999999"><meta property="og:image" content="javascript:alert(1)">',
    )
    res = await _run([page("/", evil), page("/x", "")])
    for i in res.data.items:
        json.loads(i.json_ld)
        assert "javascript:" not in i.json_ld


def test_template_helpers() -> None:
    assert T.placeholder_paths({"a": {"b": T.placeholder("x")}, "c": ["ok", T.placeholder("y", "tr")]}) == [
        "a.b",
        "c.1",
    ]
    assert T.strip_nones({"a": None, "b": "", "c": 1}) == {"c": 1}
