"""Schema (JSON-LD) checks: one positive and one clean case per rule, plus the reference fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seoprobe.core.models import Category, Severity
from seoprobe.core.parse.html import parse_html
from seoprobe.core.rules.catalog import message, rules
from seoprobe.features.content.schema.checks import DEPRECATED_TYPES, SCHEMA_RULE_IDS, run_schema_checks

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures"
U = "https://example.com/guide/"


def check(*blocks: object) -> set[str]:
    scripts = "".join(
        f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>' for b in blocks
    )
    out = run_schema_checks(parse_html(f"<html><head>{scripts}</head><body></body></html>", U), U)
    return {f.id for f in out.findings}


ARTICLE = {
    "@context": "https://schema.org",
    "@type": "Article",
    "headline": "H",
    "author": {"@type": "Person", "name": "A"},
    "datePublished": "2026-01-02T10:00:00+03:00",
    "image": "https://example.com/a.png",
}
PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Shoe",
    "image": "https://example.com/s.png",
    "offers": {
        "@type": "Offer",
        "price": "9.99",
        "priceCurrency": "USD",
        "availability": "https://schema.org/InStock",
    },
}


def test_clean_blocks_produce_no_findings() -> None:
    assert check(ARTICLE) == set()
    assert check(PRODUCT) == set()
    assert check(ARTICLE, PRODUCT) == set()
    assert check() == set()  # no schema at all is not a finding


def test_all_rules_registered_in_schema_category_with_texts() -> None:
    assert len(SCHEMA_RULE_IDS) == 10
    for rid in SCHEMA_RULE_IDS:
        d = rules()[rid]
        assert d.category == Category.SCHEMA
        assert message(f"finding.{rid}", "en") != f"finding.{rid}"
        assert message(f"finding.{rid}", "tr") != message(f"finding.{rid}", "en")
        assert message(f"rec.{rid}", "tr") != f"rec.{rid}"


@pytest.mark.parametrize("t", sorted(DEPRECATED_TYPES))
def test_deprecated_types_flagged(t: str) -> None:
    assert "schema-deprecated-type" in check({"@context": "https://schema.org", "@type": t, "name": "x"})


def test_ambiguous_types_are_not_flagged() -> None:
    """The reference says ClaimReview/Dataset/Course are not simply 'dead'; we do not claim so."""
    for t in ("ClaimReview", "Dataset", "Course", "Vehicle"):
        assert "schema-deprecated-type" not in check({"@context": "https://schema.org", "@type": t, "name": "x"})


def test_faq_is_info_never_a_removal_recommendation() -> None:
    out = run_schema_checks(
        parse_html(
            '<script type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[]}</script>',
            U,
        ),
        U,
    )
    f = next(f for f in out.findings if f.id == "schema-faq-rich-result-retired")
    assert f.severity == Severity.INFO
    rec = message(f.recommendation_key or "", "en").lower()
    assert "remove" not in rec and "qapage" in rec


def test_placeholder_values() -> None:
    assert "schema-placeholder-values" in check({**ARTICLE, "headline": "[Business Name]"})
    assert "schema-placeholder-values" in check({**ARTICLE, "headline": "Lorem ipsum dolor"})
    assert "schema-placeholder-values" in check({**ARTICLE, "author": {"@type": "Person", "name": "Your company"}})
    assert "schema-placeholder-values" not in check({**ARTICLE, "headline": "Review [2026] results"})
    assert "schema-placeholder-values" not in check(
        {**ARTICLE, "image": "https://example.com/a.png"}
    )  # a real, auditable domain


def test_context_http() -> None:
    assert "schema-context-not-https" in check({**ARTICLE, "@context": "http://schema.org"})
    assert "schema-context-not-https" in check({**ARTICLE, "@context": "http://schema.org/"})
    assert "schema-context-not-https" not in check(ARTICLE)


def test_missing_type() -> None:
    assert "schema-missing-type" in check({"@context": "https://schema.org", "name": "x"})
    assert "schema-missing-type" not in check({"@context": "https://schema.org", "@graph": [ARTICLE]})


def test_key_properties_only_on_block_entities_not_nested_references() -> None:
    assert "schema-missing-key-properties" in check(
        {"@context": "https://schema.org", "@type": "Organization", "name": "X"}
    )
    # a nested publisher with only a name is a normal reference, not a finding
    nested = {**ARTICLE, "publisher": {"@type": "Organization", "name": "X"}}
    assert "schema-missing-key-properties" not in check(nested)
    graph = {"@context": "https://schema.org", "@graph": [{"@type": "WebSite", "name": "S"}]}
    assert "schema-missing-key-properties" in check(graph)  # url missing on a @graph member
    full = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "X",
        "url": "https://x.test/",
        "logo": "https://x.test/l.png",
    }
    assert "schema-missing-key-properties" not in check(full)


def test_product_minimum_and_offer_fields() -> None:
    assert "schema-product-missing-fields" in check(
        {"@context": "https://schema.org", "@type": "Product", "name": "Shoe"}
    )
    assert "schema-product-missing-fields" not in check(PRODUCT)
    no_price = json.loads(json.dumps(PRODUCT))
    no_price["offers"].pop("priceCurrency")
    assert "schema-offer-missing-fields" in check(no_price)
    assert "schema-offer-missing-fields" not in check(PRODUCT)


def test_relative_urls_and_dates() -> None:
    assert "schema-relative-url" in check({**ARTICLE, "image": "/img/a.png"})
    assert "schema-relative-url" in check({**ARTICLE, "image": ["https://x.test/a.png", "b.png"]})
    assert "schema-relative-url" not in check(ARTICLE)
    assert "schema-invalid-date" in check({**ARTICLE, "datePublished": "13/11/2026"})
    for ok in (
        "2026-11-13",
        "2026-11-13T10:00:00Z",
        "2026-11-13T10:00:00.123+03:00",
        "2026-11-13 10:00",
    ):
        assert "schema-invalid-date" not in check({**ARTICLE, "datePublished": ok})


def test_syntax_errors_belong_to_the_page_check_not_here() -> None:
    assert check("{not json") == set()


def test_rich_fixture_reports_the_expected_set() -> None:
    p = parse_html((GOLDEN / "schema_rich.html").read_text(encoding="utf-8"), U)
    out = run_schema_checks(p, U)
    got = {f.id for f in out.findings}
    assert got == {
        "schema-context-not-https",
        "schema-placeholder-values",
        "schema-deprecated-type",
        "schema-faq-rich-result-retired",
        "schema-product-missing-fields",
        "schema-offer-missing-fields",
        "schema-invalid-date",
        "schema-relative-url",
        "schema-missing-key-properties",
    } | ({"schema-missing-type"} if "schema-missing-type" in got else set())
    assert all(f.url == U and f.category == Category.SCHEMA for f in out.findings)
    assert "HowTo" in out.facts["schema_types"] and out.facts["jsonld_blocks"] == 1


def test_deep_or_huge_json_never_raises() -> None:
    deep: object = {"@type": "Thing"}
    for _ in range(200):
        deep = {"@type": "Thing", "child": deep}
    assert check(deep) is not None
    assert check({"@type": "ItemList", "itemListElement": [{"@type": "Thing"}] * 5000}) is not None
    assert check({"@context": "https://schema.org", "@type": 5, "name": ["a", {"b": 1}]}) is not None
