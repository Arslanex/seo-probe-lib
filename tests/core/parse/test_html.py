"""parse_html: extraction facts, URL helpers, limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from seoprobe.core.parse import html as H
from seoprobe.core.parse.html import canonicalize_url, parse_html, trim, url_match_key, words_measurable

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures"
U = "https://example.com/guide/"


def page(name: str) -> H.ParsedPage:
    return parse_html((GOLDEN / f"{name}.html").read_text(encoding="utf-8"), U)


def test_basic_fields_of_a_good_article() -> None:
    p = page("ssr_article")
    assert p.parse_error is None
    assert p.title == "Practical guides for running a small online business"
    assert p.lang == "en" and p.has_doctype and p.has_viewport and p.meta_charset == "utf-8"
    assert p.h1 == ["Running a small online business"]
    assert (2, "Domains") in p.outline and p.outline[0] == (1, "Running a small online business")
    assert p.canonical == "https://example.com/guide/" and p.canonical_link_count == 1
    assert p.og["og:image"] == "https://example.com/og.png"
    assert p.schema_types == ["Article"] and p.jsonld_total == 1 and p.jsonld[0].error is None
    assert p.word_count > 200 and p.main_text and "Privacy" not in p.visible_text  # footer is chrome
    assert p.dom["max_depth"] >= 4 and p.dom["nodes"] > 10


def test_links_carry_zone_text_and_flags() -> None:
    p = page("links_generic_notext")
    by_href = {lk.href: lk for lk in p.links}
    assert by_href["/a"].generic and by_href["/a"].zone == "main" and by_href["/a"].resolved == "https://example.com/a"
    assert not by_href["/c"].named  # empty link
    assert not by_href["/d"].named  # image without alt
    assert by_href["/e"].named  # aria-label
    assert by_href["mailto:a@b.c"].resolved == "" and by_href["#top"].resolved == ""
    assert by_href["/x"].zone == "chrome" and not by_href["/x"].generic  # generic text inside <nav>


def test_images_flags() -> None:
    p = parse_html(
        '<img src="/a.png" alt="x" width="1" height="1"><img src="/b.png"><img src="/c.png" alt="">'
        '<div aria-hidden="true"><img src="/d.png"></div><img src="data:image/gif;base64,AAA">'
        '<img data-src="/lazy.png" src="data:image/gif;base64,AAA" loading="lazy">',
        U,
    )
    srcs = [i.src for i in p.images]
    assert srcs == ["/a.png", "/b.png", "/c.png", "/d.png", "/lazy.png"] or "/lazy.png" in srcs
    by = {i.src: i for i in p.images}
    assert by["/a.png"].has_alt_attr and by["/a.png"].has_width and by["/a.png"].has_height
    assert not by["/b.png"].has_alt_attr and by["/c.png"].has_alt_attr and by["/c.png"].alt == ""
    assert by["/lazy.png"].lazy
    assert by["/d.png"].decorative  # inside aria-hidden


def test_base_href_resolves_relative_urls() -> None:
    p = parse_html(
        '<base href="https://cdn.example.org/root/"><link rel="canonical" href="page/"><a href="x">x</a>',
        "https://example.com/z/",
    )
    assert p.base_url == "https://cdn.example.org/root/"
    assert p.canonical == "https://cdn.example.org/root/page/"
    assert p.links[0].resolved == "https://cdn.example.org/root/x"


def test_svg_title_is_not_the_document_title() -> None:
    p = parse_html("<svg><title>icon</title></svg><title>Real</title>", U)
    assert p.title == "Real" and p.title_tag_count == 1


def test_visible_text_excludes_scripts_nav_footer_and_site_header() -> None:
    p = parse_html(
        "<header>SITEHEAD</header><nav>NAVTEXT</nav><script>var x=1</script><style>.a{}</style>"
        "<article><header><h1>OWN HEADING</h1></header>body words</article><footer>FOOT</footer>",
        U,
    )
    assert "OWN HEADING" in p.visible_text and "body words" in p.visible_text
    for chrome in ("SITEHEAD", "NAVTEXT", "var x", "FOOT"):
        assert chrome not in p.visible_text


def test_heading_skips_content_vs_template() -> None:
    p = page("headings_skip")
    assert p.heading_level_skips == 1 and p.template_heading_level_skips >= 1


def test_mixed_content_only_on_https_pages() -> None:
    src = '<img src="http://x.test/a.png"><script src="http://x.test/b.js"></script><link rel="stylesheet" href="http://x.test/c.css">'
    assert len(parse_html(src, "https://example.com/").mixed_content) == 3
    assert parse_html(src, "http://example.com/").mixed_content == []


def test_jsonld_graph_types_errors_and_limits() -> None:
    blocks = "".join('<script type="application/ld+json">{"@type":"Thing","n":%d}</script>' % i for i in range(60))
    p = parse_html(blocks + '<script type="application/ld+json">{bad json}</script>', U)
    assert p.jsonld_total == 61 and len(p.jsonld) == H.JSON_LD_MAX_BLOCKS  # cap
    g = parse_html(
        '<script type="application/ld+json">{"@graph":[{"@type":"Organization"},{"@type":["Product","Thing"]}]}</script>',
        U,
    )
    assert g.schema_types == ["Organization", "Product", "Thing"]
    big = parse_html(
        '<script type="application/ld+json">{"a":"' + "x" * (H.JSON_LD_MAX_BLOCK_BYTES + 10) + '"}</script>',
        U,
    )
    assert big.jsonld[0].error == "block too large to parse"


def test_link_and_image_caps_are_flagged() -> None:
    p = parse_html("<a href='/x'>x</a>" * (H.MAX_LINKS + 5), U)
    assert len(p.links) == H.MAX_LINKS and p.links_truncated
    q = parse_html("<img src='a.png'>" * (H.MAX_IMAGES + 5), U)
    assert len(q.images) == H.MAX_IMAGES and q.images_truncated


def test_doctype_detection_ignores_bom_and_leading_comments() -> None:
    assert parse_html("﻿<!-- c --><!DOCTYPE html><html></html>", U).has_doctype
    assert not parse_html("<html></html>", U).has_doctype
    assert parse_html("<html></html>", U).doctype_head.startswith("<html")


def test_main_text_can_be_disabled_and_falls_back() -> None:
    assert parse_html("<p>" + "word " * 50 + "</p>", U, extract_main_text=False).main_text == ""
    assert parse_html("<p>" + "word " * 50 + "</p>", U).main_text  # trafilatura or fallback, never empty here


# --------------------------------------------------------------------------- URL helpers


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://example.com/a", "https://example.com/a/"),
        ("https://EXAMPLE.com/a?utm_source=x", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com/caf%c3%a9", "https://example.com/café"),
        ("https://example.com//a///b", "https://example.com/a/b"),
        ("https://bücher.de/", "https://xn--bcher-kva.de/"),
    ],
)
def test_url_match_key_equivalences(a: str, b: str) -> None:
    assert url_match_key(a) == url_match_key(b)


def test_url_match_key_distinguishes_real_differences() -> None:
    assert url_match_key("https://example.com/a") != url_match_key("https://example.com/b")
    assert url_match_key("https://example.com/a?x=1") != url_match_key("https://example.com/a?x=2")
    assert url_match_key("https://example.com/%2F") != url_match_key("https://example.com/")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a url",
        "ftp://example.com/",
        "https://",
        "https://exa mple.com/",
        "https://example.com:99999999/",
    ],
)
def test_canonicalize_rejects_non_web_urls(bad: str) -> None:
    assert canonicalize_url(bad) is None


def test_canonicalize_strips_fragment_and_tracking() -> None:
    assert canonicalize_url("https://example.com/p?gclid=1&a=2#frag") == "https://example.com/p?a=2"


def test_trim_cleans_controls_and_cuts() -> None:
    assert trim("a​\x07  b\n c") == "a b c"
    out = trim("x" * 500, 50)
    assert len(out) == 50 and out.endswith("...")


def test_words_measurable() -> None:
    assert words_measurable("plain english text", "en")
    assert not words_measurable("anything", "ja") and not words_measurable("anything", "zh-CN")
    assert not words_measurable("私たちは" * 20, "")  # detected from the script itself
    assert words_measurable("", "")
