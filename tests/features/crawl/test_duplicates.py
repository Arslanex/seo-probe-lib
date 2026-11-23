"""Near-duplicate content: exact, near, different, short, errors, aliases, Turkish, size cap."""

from __future__ import annotations

import json

from seoprobe.core.page import Page
from seoprobe.features.crawl import duplicates
from seoprobe.features.crawl.duplicates import find_duplicates, fold, jaccard, shingles, words
from tests import fakes
from tests.features.crawl.support import with_parsed

HOST = "https://example.com"


def text(prefix: str, n: int = 150) -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def pg(
    path: str,
    body: str,
    *,
    status: int | None = 200,
    kind: str = "core",
    final: str | None = None,
    extract: bool = False,
) -> Page:
    url = f"{HOST}{path}"
    html = f"<html><body><nav>Shared navigation menu home about</nav><main><p>{body}</p></main></body></html>"
    p = Page(
        url=url,
        final_url=final or url,
        status=status,
        content_type="text/html",
        html=html,
        sample_kind=kind,  # type: ignore[arg-type]
    )
    return with_parsed(p, extract_main_text=extract)


def test_identical_text_is_flagged_on_both_pages() -> None:
    out = find_duplicates([pg("/a", text("alpha")), pg("/b", text("alpha"))])
    assert {f.url for f in out.findings} == {f"{HOST}/a", f"{HOST}/b"}
    f = out.findings[0]
    assert f.id == "duplicate-content" and "100%" in (f.evidence or "") and f.count == 1
    assert out.facts["duplicate_pairs"] == 1


def test_evidence_names_the_other_page() -> None:
    out = find_duplicates([pg("/a", text("alpha")), pg("/b", text("alpha"))])
    by = {f.url: f.evidence for f in out.findings}
    assert by[f"{HOST}/a"].endswith(f"{HOST}/b") and by[f"{HOST}/b"].endswith(f"{HOST}/a")  # type: ignore[union-attr]


def test_different_texts_are_not_flagged() -> None:
    assert find_duplicates([pg("/a", text("alpha")), pg("/b", text("beta"))]).findings == []


def test_near_duplicate_above_and_below_the_threshold() -> None:
    base = text("alpha").split()
    two = list(base)
    two[40], two[100] = "changed1", "changed2"  # 10 of 146 shingles differ -> ~0.87
    ten = list(base)
    for i in range(5, 150, 14):
        ten[i] = f"changed{i}"
    assert len(find_duplicates([pg("/a", " ".join(base)), pg("/b", " ".join(two))]).findings) == 2
    assert find_duplicates([pg("/a", " ".join(base)), pg("/b", " ".join(ten))]).findings == []


def test_threshold_is_a_parameter() -> None:
    base = text("alpha").split()
    two = list(base)
    two[40], two[100] = "x1", "x2"
    pages = [pg("/a", " ".join(base)), pg("/b", " ".join(two))]
    assert find_duplicates(pages, threshold=0.95).findings == []
    assert len(find_duplicates(pages, threshold=0.5).findings) == 2


def test_shared_navigation_alone_does_not_make_pages_duplicates() -> None:
    # both pages carry the same nav in the HTML; with real main-text extraction only the article counts
    out = find_duplicates([pg("/a", text("alpha"), extract=True), pg("/b", text("beta"), extract=True)])
    assert out.findings == []


def test_short_pages_are_skipped_even_when_identical() -> None:
    assert find_duplicates([pg("/a", text("alpha", 40)), pg("/b", text("alpha", 40))]).findings == []
    assert len(find_duplicates([pg("/a", text("alpha", 40)), pg("/b", text("alpha", 40))], min_words=30).findings) == 2


def test_error_pages_and_unparsed_pages_are_skipped() -> None:
    bad = pg("/b", text("alpha"), status=404)
    unparsed = Page(url=f"{HOST}/c", final_url=f"{HOST}/c", status=200)
    failed = Page(url=f"{HOST}/d", final_url="", status=None, error="timeout")
    assert find_duplicates([pg("/a", text("alpha")), bad, unparsed, failed]).findings == []


def test_two_sampled_urls_that_end_on_the_same_page_are_one_page() -> None:
    a = pg("/a", text("alpha"))
    alias = pg("/a-old", text("alpha"), final=f"{HOST}/a")
    assert find_duplicates([a, alias]).findings == []


def test_three_way_duplicate_counts_every_match() -> None:
    out = find_duplicates([pg("/a", text("alpha")), pg("/b", text("alpha")), pg("/c", text("alpha"))])
    assert len(out.findings) == 3 and all(f.count == 2 for f in out.findings)


def test_turkish_dotted_and_dotless_i_fold_together() -> None:
    tr = " ".join(["ışık", "İstanbul", "çok", "güzel", "bir", "şehir", "ağaç", "iğne", "ılık", "iç"] * 15)
    out = find_duplicates([pg("/a", tr), pg("/b", tr.upper())])
    assert len(out.findings) == 2
    assert fold("IŞIK İSTANBUL") == fold("ışık istanbul")


def test_helper_functions() -> None:
    assert words("Hello, WORLD! 42") == ["hello", "world", "42"]
    assert shingles(["a", "b"]) == set() and len(shingles(list("abcdefg"))) == 3
    assert jaccard({1, 2}, {2, 3}) == 1 / 3 and jaccard(set(), {1}) == 0.0


def test_very_long_texts_are_sketched_and_still_match() -> None:
    long_a = " ".join(f"w{i}" for i in range(12000))
    out = find_duplicates([pg("/a", long_a), pg("/b", long_a)])
    assert len(out.findings) == 2


def test_more_than_two_hundred_pages_are_capped_and_reported() -> None:
    pages = [pg(f"/p{i}", text(f"u{i}x")) for i in range(205)]
    out = find_duplicates(pages)
    assert out.findings == [] and len(out.not_checked) == 5
    assert {(n.check_id, n.reason_code) for n in out.not_checked} == {("duplicate-content", "sample-too-large")}


def test_core_pages_are_compared_before_exploration_pages_when_capped() -> None:
    pages = [pg(f"/e{i}", text(f"e{i}x"), kind="exploration") for i in range(200)]
    pages += [pg("/c1", text("core")), pg("/c2", text("core"))]
    out = find_duplicates(pages)
    assert {f.url for f in out.findings} == {f"{HOST}/c1", f"{HOST}/c2"}
    assert len(out.not_checked) == 2 and all("/e" in (n.url or "") for n in out.not_checked)


def _with_long_nav(path: str, main: str, *, extract: bool) -> Page:
    nav = " ".join(
        f"nav{i}" for i in range(2000)
    )  # the same 2000-word menu on every page, in a plain div (a <nav> tag is already stripped)
    url = f"{HOST}{path}"
    html = f'<html><body><div class="menu">{nav}</div><main><article><p>{main}</p></article></main></body></html>'
    p = Page(url=url, final_url=url, status=200, content_type="text/html", html=html)
    return with_parsed(p, extract_main_text=extract)


def test_main_text_is_used_so_a_shared_menu_does_not_create_false_duplicates() -> None:
    pages = [
        _with_long_nav("/a", text("alpha", 120), extract=True),
        _with_long_nav("/b", text("beta", 120), extract=True),
    ]
    assert all(p.parsed and p.parsed.main_text for p in pages), "trafilatura must find the article"
    assert find_duplicates(pages).findings == []
    # control: on the whole visible text the same pages WOULD look like duplicates
    whole = [
        _with_long_nav("/a", text("alpha", 120), extract=False),
        _with_long_nav("/b", text("beta", 120), extract=False),
    ]
    assert len(find_duplicates(whole).findings) == 2


# ----------------------------------------------------------------------------- the operation


async def test_duplicates_operation_returns_findings_and_pair_count() -> None:
    a = Page(
        url=f"{HOST}/a",
        final_url=f"{HOST}/a",
        status=200,
        html=f"<html><body><main><p>{text('alpha')}</p></main></body></html>",
    )
    b = Page(
        url=f"{HOST}/b",
        final_url=f"{HOST}/b",
        status=200,
        html=f"<html><body><main><p>{text('alpha')}</p></main></body></html>",
    )
    async with fakes.session({}) as s:
        res = await duplicates([a, b], session=s)
    assert res.ok and res.data is not None and res.data.duplicate_pairs == 1
    assert [f.id for f in res.findings] == ["duplicate-content", "duplicate-content"]
    json.loads(res.to_json())
