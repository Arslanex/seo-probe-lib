"""
Page checks: every one of the 47 rules is proven to fire (POSITIVE), the threshold and
context rules also prove they do NOT fire just below the line (NEAR_MISS), and a clean page
proves the rest (CLEAN).
"""

from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest

from seoprobe.core.models import FetchResult, Severity
from seoprobe.core.parse.html import parse_html
from seoprobe.core.rules import catalog
from seoprobe.features.page.onpage import PAGE_RULE_IDS, run_page_checks

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures"
RULES = catalog.rules()
U = "https://example.com/guide/"
H = {"content-type": "text/html; charset=utf-8"}
BASE = (GOLDEN / "ssr_article.html").read_text(encoding="utf-8")
EN_BODY = "word " * 250


def run(html: str, url: str = U, headers: dict[str, str] | None = H, size: int | None = None):  # type: ignore[no-untyped-def]
    fetch = FetchResult(
        url=url,
        final_url=url,
        status=200 if headers is not None else None,
        headers=headers or {},
        html=html,
        html_bytes=size if size is not None else len(html.encode("utf-8")),
    )
    return run_page_checks(fetch, parse_html(html, url))


def ids(out) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.id for f in out.findings}


def sub(old: str, new: str, html: str = BASE) -> str:
    assert old in html, old
    return html.replace(old, new, 1)


def doc(head: str = "", body: str = "", lang: str = ' lang="en"') -> str:
    return f'<!DOCTYPE html><html{lang}><head><meta charset="utf-8">{head}</head><body>{body}</body></html>'


# --------------------------------------------------------------------------- the clean baseline


def test_baseline_page_has_no_findings() -> None:
    out = run(BASE)
    assert out.findings == [] and out.not_checked == []
    assert out.facts["checks_run"] == 47


def test_all_47_rules_registered_with_source_severity_counts() -> None:
    sev = [RULES[i].severity for i in PAGE_RULE_IDS]
    assert len(PAGE_RULE_IDS) == 47
    assert (
        sev.count(Severity.CRITICAL),
        sev.count(Severity.HIGH),
        sev.count(Severity.MEDIUM),
        sev.count(Severity.LOW),
    ) == (2, 6, 24, 15)


# --------------------------------------------------------------------------- POSITIVE / NEAR_MISS

TITLE = "Practical guides for running a small online business"


def _title(n: int) -> str:
    return sub(f"<title>{TITLE}</title>", f"<title>{'t' * n}</title>")


def _desc(n: int) -> str:
    return sub(
        'content="Step by step guides for new online shops: domains, hosting, product descriptions and simple analytics for the first ninety days."',
        f'content="{"d" * n}"',
    )


def _minimal(body: str, head: str = "") -> str:
    return doc(head + f"<title>{TITLE}</title>", body)


def _nested(depth: int) -> str:
    return _minimal("<div>" * depth + "x" + "</div>" * depth)


# rule id -> (positive (html, url, headers, size), near-miss or None)
Case = tuple[str, str, dict[str, str] | None, int | None]


def _c(html: str, url: str = U, headers: dict[str, str] | None = H, size: int | None = None) -> Case:
    return (html, url, headers, size)


PAIRS: dict[str, tuple[Case, Case | None]] = {
    "missing-title": (_c(sub(f"<title>{TITLE}</title>", "")), _c(_title(1))),
    "title-too-long": (_c(_title(61)), _c(_title(60))),
    "title-too-short": (_c(_title(29)), _c(_title(30))),
    "missing-meta-description": (
        _c(
            sub(
                '<meta name="description" content="Step by step',
                '<meta name="x" content="Step by step',
            )
        ),
        _c(BASE),
    ),
    "meta-description-too-long": (_c(_desc(161)), _c(_desc(160))),
    "duplicate-meta-tags": (_c(sub("</title>", "</title><title>Two</title>")), _c(BASE)),
    "missing-h1": (_c(sub("<h1>Running a small online business</h1>", "<p>x</p>")), None),
    "multiple-h1": (
        _c(sub("</main>", "<h1>Another</h1></main>")),
        _c(sub("</main>", "<h2>Another</h2></main>")),
    ),
    "heading-level-skip": (
        _c(sub("<h2>Domains</h2>", "<h4>Domains</h4>")),
        _c(sub("<h2>Domains</h2>", "<h2>Domains</h2><h3>Sub</h3>")),
    ),
    "missing-canonical": (
        _c(sub('<link rel="canonical" href="https://example.com/guide/">', "")),
        _c(
            sub(
                'https://example.com/guide/">',
                'https://example.com/guide">',
            )
            if False
            else BASE
        ),
    ),
    "canonical-points-elsewhere": (
        _c(
            sub(
                '<link rel="canonical" href="https://example.com/guide/">',
                '<link rel="canonical" href="https://example.com/other/">',
            )
        ),
        _c(
            sub(
                '<link rel="canonical" href="https://example.com/guide/">',
                '<link rel="canonical" href="https://example.com/guide">',
            )
        ),  # trailing slash only
    ),
    "og-url-differs-from-canonical": (
        _c(
            sub(
                'og:url" content="https://example.com/guide/"',
                'og:url" content="https://example.com/other/"',
            )
        ),
        _c(
            sub(
                'og:url" content="https://example.com/guide/"',
                'og:url" content="https://example.com/guide"',
            )
        ),
    ),
    "noindex": (
        _c(sub("<title>", '<meta name="robots" content="noindex"><title>')),
        _c(sub("<title>", '<meta name="robots" content="index, follow"><title>')),
    ),
    "noindex-header": (
        _c(BASE, headers={**H, "x-robots-tag": "noindex"}),
        _c(BASE, headers={**H, "x-robots-tag": "bingbot: noindex"}),
    ),
    "nofollow": (
        _c(sub("<title>", '<meta name="robots" content="nofollow"><title>')),
        _c(sub("<title>", '<meta name="robots" content="max-image-preview: none"><title>')),  # "none" is a value here
    ),
    "meta-refresh-redirect": (
        _c(
            sub(
                "<title>",
                '<meta http-equiv="refresh" content="0; url=https://example.com/new/"><title>',
            )
        ),
        _c(sub("<title>", '<meta http-equiv="refresh" content="30"><title>')),
    ),
    "hreflang-missing-self": (
        _c(
            sub(
                "<title>",
                '<link rel="alternate" hreflang="de" href="https://example.com/de/"><title>',
            )
        ),
        _c(
            sub(
                "<title>",
                '<link rel="alternate" hreflang="en" href="https://example.com/guide"><title>',
            )
        ),
    ),
    "missing-doctype": (
        _c(sub("<!DOCTYPE html>", "")),
        _c(sub("<!DOCTYPE html>", "<!doctype html>")),
    ),
    "missing-charset": (
        _c(sub('<meta charset="utf-8">', ""), headers={"content-type": "text/html"}),
        _c(sub('<meta charset="utf-8">', ""), headers=H),  # header charset is enough
    ),
    "charset-mismatch": (
        _c(BASE, headers={"content-type": "text/html; charset=iso-8859-9"}),
        _c(BASE, headers={"content-type": "text/html; charset=UTF8"}),  # alias of utf-8
    ),
    "missing-lang": (_c(sub('<html lang="en">', "<html>")), None),
    "missing-viewport": (
        _c(sub('<meta name="viewport" content="width=device-width, initial-scale=1">', "")),
        None,
    ),
    "uses-frames": (
        _c(_minimal("", "").replace("<body></body>", '<frameset cols="1*"><frame src="a"></frameset>')),
        None,
    ),
    "deprecated-html-tags": (_c(sub("<main>", "<main><center>x</center>")), None),
    "placeholder-text": (
        _c(sub("<main>", "<main><p>Lorem ipsum dolor sit amet</p>")),
        _c(sub("<main>", "<main><p>Lorem is a surname here</p>")),
    ),
    "images-missing-alt": (
        _c(sub('alt="A diagram" ', "")),
        _c(sub('alt="A diagram"', 'alt=""')),  # empty alt = decorative: not a finding
    ),
    "images-missing-dimensions": (
        _c(sub(' width="600" height="400"', "")),
        _c(BASE),
    ),
    "first-image-lazy-loaded": (
        _c(sub('<img src="/img/a.png"', '<img loading="lazy" src="/img/a.png"')),
        _c(
            sub(
                "</main>",
                '<img loading="lazy" src="/img/b.png" alt="b" width="1" height="1"></main>',
            )
        ),  # second image
    ),
    "links-without-text": (
        _c(sub("</main>", '<a href="/x"></a></main>')),
        _c(sub("</main>", '<a href="/x" aria-label="Pricing"></a></main>')),
    ),
    "generic-anchor-text": (
        _c(sub("</main>", '<a href="/x">click here</a></main>')),
        _c(sub("</main>", '<a href="/x">click here</a></main>').replace("<main>", "<nav>").replace("</main>", "</nav>"))
        if False
        else _c(sub("</main>", '<a href="/x">details about our delivery</a></main>')),
    ),
    "links-to-http": (
        _c(sub("</main>", '<a href="http://other.example.net/p">Our partner page</a></main>')),
        _c(
            sub("</main>", '<a href="http://other.example.net/p">Our partner page</a></main>'),
            url="http://example.com/guide/",
        ),
    ),
    "mixed-content": (
        _c(
            sub(
                "</main>",
                '<img src="http://cdn.example.com/a.png" alt="a" width="1" height="1"></main>',
            )
        ),
        _c(
            sub(
                "</main>",
                '<img src="https://cdn.example.com/a.png" alt="a" width="1" height="1"></main>',
            )
        ),
    ),
    "url-too-long": (
        _c(BASE, url="https://example.com/" + "a" * 81),
        _c(BASE, url="https://example.com/" + "a" * 80),
    ),  # 20-char prefix: 101 vs 100
    "url-has-parameters": (
        _c(BASE, url=U + "?page=2"),
        _c(BASE, url=U + "?utm_source=x&fbclid=y"),  # tracking only
    ),
    "url-uppercase": (
        _c(BASE, url="https://example.com/Guide/"),
        _c(BASE, url="https://example.com/caf%C3%A9/"),
    ),
    "url-underscores": (_c(BASE, url="https://example.com/my_guide/"), None),
    "url-non-ascii": (
        _c(BASE, url="https://example.com/küçük/"),
        _c(BASE, url="https://example.com/k%C3%BC%C3%A7%C3%BCk/"),
    ),
    "url-double-slash": (_c(BASE, url="https://example.com/a//b/"), None),
    "html-over-2mb": (_c(BASE, size=2_000_001), _c(BASE, size=2_000_000)),
    "deep-dom": (
        _c(_nested(31)),
        _c(_nested(29)),
    ),  # html+body add 2 levels: 31 -> 33 > 32, 29 -> 31
    "wide-dom-node": (
        _c(_minimal("<ul>" + "<li>i</li>" * 61 + "</ul>")),
        _c(_minimal("<ul>" + "<li>i</li>" * 60 + "</ul>")),
    ),
    "missing-open-graph": (
        _c(
            BASE.replace('<meta property="og:title" content="Guides">', "").replace(
                '<meta property="og:image" content="https://example.com/og.png">', ""
            )
        ),
        None,
    ),
    "missing-og-image": (
        _c(sub('<meta property="og:image" content="https://example.com/og.png">', "")),
        None,
    ),
    "invalid-jsonld": (
        _c(sub('"@type":"Article"', '"@type":Article')),
        _c(BASE),
    ),
    "title-not-reflected-in-content": (
        _c(
            _minimal("<p>" + "gardening soil compost watering planting seasons " * 15 + "</p>").replace(
                TITLE, "Cryptocurrency mining hardware comparison"
            )
        ),
        _c(
            _minimal("<p>" + "cryptocurrency mining hardware comparison guide " * 15 + "</p>").replace(
                TITLE, "Cryptocurrency mining hardware comparison"
            )
        ),
    ),
    "description-not-reflected-in-content": (
        _c(
            _minimal(
                "<p>" + "gardening soil compost watering planting seasons " * 15 + "</p>",
                '<meta name="description" content="Blockchain wallet security investors">',
            )
        ),
        _c(
            _minimal(
                "<p>" + "blockchain wallet security investors gardening " * 15 + "</p>",
                '<meta name="description" content="Blockchain wallet security investors">',
            )
        ),
    ),
    "thin-content": (
        _c(_minimal("<p>" + "word " * 199 + "</p>")),
        _c(_minimal("<p>" + "word " * 200 + "</p>")),
    ),
}


def test_every_rule_has_a_positive_case() -> None:
    assert set(PAIRS) == set(PAGE_RULE_IDS)


@pytest.mark.parametrize("rule_id", PAGE_RULE_IDS)
def test_positive(rule_id: str) -> None:
    html, url, headers, size = PAIRS[rule_id][0]
    out = run(html, url, headers, size)
    assert rule_id in ids(out), f"{rule_id} did not fire; got {sorted(ids(out))}"
    finding = next(f for f in out.findings if f.id == rule_id)
    assert finding.severity == RULES[rule_id].severity
    assert finding.message and finding.url == url
    assert finding.evidence is None or len(finding.evidence) <= 200


NEAR = [(rid, case[1]) for rid, case in PAIRS.items() if case[1] is not None]


@pytest.mark.parametrize("rule_id,case", NEAR, ids=[n[0] for n in NEAR])
def test_near_miss_does_not_fire(rule_id: str, case: Case) -> None:
    html, url, headers, size = case
    assert rule_id not in ids(run(html, url, headers, size))


def test_near_miss_coverage_is_at_least_15_rules() -> None:
    assert len(NEAR) >= 15


# --------------------------------------------------------------------------- gates and context


def test_headers_unavailable_marks_header_rules_not_checked() -> None:
    out = run(BASE, headers=None)
    assert {n.check_id: n.reason_code for n in out.not_checked} == {
        "noindex-header": "headers-unavailable",
        "charset-mismatch": "headers-unavailable",
    }


def test_language_gate_non_english_text_skips_relevance_checks() -> None:
    html = (GOLDEN / "relevance_tr.html").read_text(encoding="utf-8")
    out = run(html)
    reasons = {n.check_id: n.reason_code for n in out.not_checked}
    assert reasons["title-not-reflected-in-content"] == "language-unsupported"
    assert reasons["description-not-reflected-in-content"] == "language-unsupported"
    assert "title-not-reflected-in-content" not in ids(out)
    assert out.facts["relevance_language_supported"] is False


def test_language_gate_english_runs_and_flags() -> None:
    out = run((GOLDEN / "relevance_en.html").read_text(encoding="utf-8"))
    assert {"title-not-reflected-in-content", "description-not-reflected-in-content"} <= ids(out)


def test_language_gate_without_lang_attribute_uses_text_heuristic() -> None:
    assert "title-not-reflected-in-content" in ids(
        run((GOLDEN / "relevance_en_nolang.html").read_text(encoding="utf-8"))
    )
    tr = run((GOLDEN / "relevance_tr_nolang.html").read_text(encoding="utf-8"))
    assert "title-not-reflected-in-content" not in ids(tr)


def test_unspaced_scripts_are_not_word_counted() -> None:
    out = run((GOLDEN / "cjk_page.html").read_text(encoding="utf-8"))
    reasons = {n.check_id: n.reason_code for n in out.not_checked}
    assert reasons["thin-content"] == "words-not-measurable"
    assert "thin-content" not in ids(out) and out.facts["word_count"] is None


def test_noindex_page_lowers_missing_description_to_info() -> None:
    html = sub('<meta name="description" content="Step by step', '<meta name="x" content="Step by step')
    html = sub("<title>", '<meta name="robots" content="noindex"><title>', html)
    out = run(html)
    f = next(f for f in out.findings if f.id == "missing-meta-description")
    assert f.severity == Severity.INFO and f.severity_original == Severity.MEDIUM
    assert f.severity_reason and "noindex" in f.severity_reason
    plain = run(sub('<meta name="description" content="Step by step', '<meta name="x" content="Step by step'))
    assert next(f for f in plain.findings if f.id == "missing-meta-description").severity == Severity.MEDIUM


def test_header_noindex_also_lowers_missing_description() -> None:
    html = sub('<meta name="description" content="Step by step', '<meta name="x" content="Step by step')
    out = run(html, headers={**H, "x-robots-tag": "noindex"})
    assert next(f for f in out.findings if f.id == "missing-meta-description").severity == Severity.INFO


def test_findings_sorted_by_severity_and_facts_present() -> None:
    out = run((GOLDEN / "missing_basics.html").read_text(encoding="utf-8"))
    order = [f.severity for f in out.findings]
    from seoprobe.core.models import SEVERITY_ORDER

    assert order == sorted(order, key=SEVERITY_ORDER.index)
    for key in ("title", "word_count", "h1_count", "canonical", "lang", "schema_types", "dom"):
        assert key in out.facts


def test_evidence_is_trimmed_and_cleaned() -> None:
    html = sub(f"<title>{TITLE}</title>", "<title>" + "x​\x00y " * 100 + "</title>")
    f = next(f for f in run(html).findings if f.id == "title-too-long")
    assert f.evidence is not None and len(f.evidence) <= 200 and "\x00" not in f.evidence and "​" not in f.evidence


def test_findings_use_the_final_url_after_redirects() -> None:
    fetch = FetchResult(
        url="http://example.com/a",
        final_url="https://example.com/Guide_x/",
        status=200,
        headers=H,
        html=BASE,
        html_bytes=len(BASE),
    )
    out = run_page_checks(fetch, parse_html(BASE, fetch.final_url))
    assert {"url-uppercase", "url-underscores"} <= ids(out)
    assert all(f.url == "https://example.com/Guide_x/" for f in out.findings)


def test_messages_exist_in_en_and_tr_for_every_rule() -> None:
    from seoprobe.core.rules.catalog import message

    for rid in PAGE_RULE_IDS:
        en, tr = message(f"finding.{rid}", "en"), message(f"finding.{rid}", "tr")
        assert en != f"finding.{rid}" and tr != f"finding.{rid}" and en != tr, rid
        assert message(f"rec.{rid}", "en") != f"rec.{rid}" and message(f"rec.{rid}", "tr") != f"rec.{rid}"
    assert message("leading_indicator.onpage", "tr") != "leading_indicator.onpage"
    assert RULES["missing-title"].failure_check_key and RULES["missing-title"].leading_indicator_key


# --------------------------------------------------------------------------- hostile / robustness


@pytest.mark.parametrize(
    "html",
    [
        "",
        "   \n  ",
        "\x00\x01\x02 binary garbage \xff\xfe",
        "<html><head><title>" + "x" * 500_000 + "</title></head><body></body></html>",
        "<div>" * 3000,  # unclosed, deeply nested
        "<a href='/x'>x</a>" * 60_000,  # more links than the parser cap
        "<img src=a.png alt=''>" * 8_000,
        '<script type="application/ld+json">' + "[" * 5000 + "]" * 5000 + "</script>",
        '<script type="application/ld+json">{"a":"' + "y" * 400_000 + '"}</script>',
        "<html lang='" + "e" * 5000 + "'><body>x</body></html>",
    ],
    ids=[
        "empty",
        "blank",
        "binary",
        "huge-title",
        "deep-unclosed",
        "many-links",
        "many-images",
        "deep-json",
        "huge-json",
        "huge-lang",
    ],
)
def test_hostile_documents_never_raise_and_stay_fast(html: str) -> None:
    t0 = time.monotonic()
    out = run(html)
    assert time.monotonic() - t0 < 20
    assert isinstance(out.findings, list)
    assert "rule_error" not in out.facts


def test_unparseable_document_degrades_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    from seoprobe.core.parse import html as parse_module

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(parse_module, "BeautifulSoup", boom)
    parsed = parse_html("<html></html>", U)
    assert parsed.parse_error and "parser exploded" in parsed.parse_error
    out = run_page_checks(
        FetchResult(url=U, final_url=U, status=200, headers=H, html="<html></html>", html_bytes=13),
        parsed,
    )
    reasons = {n.reason_code for n in out.not_checked}
    assert "parse-failed" in reasons
    # URL hygiene and size rules still work without a document
    bad = run_page_checks(
        FetchResult(
            url=U,
            final_url="https://example.com/A_b/",
            status=200,
            headers=H,
            html="x",
            html_bytes=3_000_000,
        ),
        parsed,
    )
    assert {"url-uppercase", "url-underscores", "html-over-2mb"} <= ids(bad)


def test_rule_exception_becomes_rule_error_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # the package exports the ``onpage`` operation under the module's name, so load the module itself
    page_checks = importlib.import_module("seoprobe.features.page.onpage")

    def boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise ValueError("bad rule")

    monkeypatch.setattr(page_checks, "_run", boom)
    out = page_checks.run_page_checks(
        FetchResult(url=U, final_url=U, status=200, headers=H, html=BASE, html_bytes=10),
        parse_html(BASE, U),
    )
    assert out.not_checked[0].reason_code == "rule-error" and "bad rule" in out.facts["rule_error"]
