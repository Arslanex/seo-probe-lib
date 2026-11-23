"""js_gap: pure raw-vs-rendered comparison."""

from __future__ import annotations

from seoprobe.features.render.render import MAX_HTML_CHARS, js_gap, js_gap_reasons
from seoprobe.features.render.render import js_gap as gap_fn

WORDS = " ".join(f"word{i}" for i in range(80))


def doc(body: str = "", head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def test_none_when_a_side_is_missing() -> None:
    assert js_gap(None, "<p>x</p>") is None
    assert js_gap("<p>x</p>", None) is None


def test_empty_raw_is_a_real_document_and_is_compared() -> None:
    g = js_gap("", doc(f"<p>{WORDS}</p>"))
    assert g is not None and g.diff.words_only_rendered == 80
    assert g.significant and "100% of visible words" in g.reasons[0]


def test_identical_documents_have_no_gap() -> None:
    d = doc(f"<h1>T</h1><p>{WORDS}</p><a href='/a'>a</a>", "<title>x</title>")
    g = js_gap(d, d)
    assert g is not None and not g.significant and g.reasons == []
    assert g.diff.words_only_rendered_share == 0.0


def test_words_threshold_needs_share_and_minimum() -> None:
    raw = doc("<p>" + " ".join(f"a{i}" for i in range(70)) + "</p>")
    extra_small = doc(
        "<p>" + " ".join(f"a{i}" for i in range(70)) + " " + " ".join(f"n{i}" for i in range(10)) + "</p>"
    )
    g = js_gap(raw, extra_small)
    assert g is not None and not g.significant  # 12.5% and only 10 words
    big = doc("<p>" + " ".join(f"a{i}" for i in range(70)) + " " + " ".join(f"n{i}" for i in range(40)) + "</p>")
    g2 = js_gap(raw, big)
    assert g2 is not None and g2.significant  # 36% and 40 words


def test_small_share_of_many_words_is_not_significant() -> None:
    base = " ".join(f"a{i}" for i in range(300))
    g = js_gap(doc(f"<p>{base}</p>"), doc(f"<p>{base} " + " ".join(f"n{i}" for i in range(30)) + "</p>"))
    assert g is not None and g.diff.words_only_rendered == 30 and not g.significant  # 9%


def test_links_only_after_js() -> None:
    raw = doc("<a href='/1'>1</a>")
    rendered = doc("<a href='/1'>1</a>" + "".join(f"<a href='/n{i}'>n</a>" for i in range(5)))
    g = js_gap(raw, rendered)
    assert g is not None and g.diff.links_only_rendered == 5
    assert any("links appear only after JavaScript" in r for r in g.reasons)


def test_non_link_hrefs_are_ignored() -> None:
    d = doc("<a href='#x'>a</a><a href='javascript:void(0)'>b</a><a href='mailto:a@b.c'>c</a><a href='tel:1'>d</a>")
    g = js_gap(d, d)
    assert g is not None and g.rendered.links == 0


def test_schema_types_only_rendered() -> None:
    ld = '<script type="application/ld+json">{"@context":"https://schema.org","@type":["Product","Thing"],"x":{"@type":"Offer"}}</script>'
    g = js_gap(doc("<p>x</p>"), doc("<p>x</p>", ld))
    assert g is not None
    assert g.diff.schema_types_only_rendered == ["Offer", "Product", "Thing"]
    assert any("structured data appears only after JavaScript" in r for r in g.reasons)


def test_invalid_jsonld_is_ignored() -> None:
    g = js_gap(doc(), doc("", '<script type="application/ld+json">{nope</script>'))
    assert g is not None and g.rendered.schema_types == []


def test_h1_variants() -> None:
    g = js_gap(doc("<h1>Old</h1>"), doc("<h1>New</h1>"))
    assert g is not None and "the H1 changes after JavaScript" in g.reasons
    g2 = js_gap(doc(), doc("<h1>New</h1>"))
    assert g2 is not None and "the H1 appears only after JavaScript" in g2.reasons


def test_title_canonical_robots_changes() -> None:
    raw = doc(
        "",
        "<title>A</title><link rel='canonical' href='https://x/a'><meta name='robots' content='index, follow'>",
    )
    rendered = doc(
        "",
        "<title>B</title><link rel='canonical' href='https://x/b'><meta name='robots' content='noindex'>",
    )
    g = js_gap(raw, rendered)
    assert g is not None
    assert g.diff.title_changed and g.diff.canonical_changed and g.diff.meta_robots_changed
    assert len(g.reasons) == 3 and "'A'" in g.reasons[0] and "'B'" in g.reasons[0]


def test_robots_directive_order_is_not_a_change() -> None:
    a = doc("", "<meta name='robots' content='index, follow'>")
    b = doc("", "<meta name='robots' content='follow,index'>")
    g = js_gap(a, b)
    assert g is not None and not g.diff.meta_robots_changed


def test_svg_title_is_not_the_document_title() -> None:
    d = doc("<svg><title>icon</title></svg>", "")
    g = js_gap(d, d)
    assert g is not None and g.raw.title is None


def test_output_is_bounded() -> None:
    many_h1 = "".join(f"<h1>h{i}</h1>" for i in range(100))
    long_title = "<title>" + "x" * 5000 + "</title>"
    g = js_gap(doc(), doc(many_h1, long_title))
    assert g is not None
    assert len(g.rendered.h1) == 12 and len(g.rendered.title or "") == 300


def test_oversized_html_is_skipped() -> None:
    big = "x" * (MAX_HTML_CHARS + 1)
    assert js_gap(big, "<p>a</p>") is None


def test_result_is_json_serialisable_and_reasons_helper() -> None:
    g = gap_fn("", doc(f"<p>{WORDS}</p>"))
    assert g is not None
    assert g.to_dict()["significant"] is True
    assert js_gap_reasons(None) == []
    assert js_gap_reasons(g) == g.reasons


def test_raw_truncated_flag_is_carried() -> None:
    g = js_gap("<p>a</p>", "<p>a</p>", raw_truncated=True)
    assert g is not None and g.raw_truncated is True
