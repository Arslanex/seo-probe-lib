"""Readability, term analysis and keyword audit (pure functions + module run)."""

from __future__ import annotations

import importlib
import time

import pytest

from seoprobe.features.content import readability
from tests import fakes

# the operation shadows the module name on the package: load the module itself
mr = importlib.import_module("seoprobe.features.content.readability")
ts = mr  # textstats lives in the same module

EASY_EN = "The cat sat on the mat. The dog ran to the park. We like to play. It is fun. " * 10
HARD_EN = (
    "Notwithstanding the aforementioned institutional considerations, organizational "
    "transformation necessitates comprehensive interdisciplinary collaboration across "
    "heterogeneous administrative jurisdictions, particularly regarding infrastructural "
    "standardization and multidimensional regulatory harmonization. "
) * 6
EASY_TR = "Ali topu at. Ayşe eve git. Biz okula gideriz. Bu çok güzel bir gün. " * 10
HARD_TR = (
    "Uluslararası sözleşmelerin değerlendirilmesinde kullanılan yorumlama yöntemlerinin "
    "karşılaştırmalı incelenmesi, hukuk sistemlerinin birbirinden farklılaşan kavramsal "
    "çerçevelerini anlamayı güçleştiren yapısal sorunları ortaya çıkarmaktadır. "
) * 6


def test_flesch_easy_vs_hard() -> None:
    easy = ts.readability_scores(EASY_EN, "en")
    hard = ts.readability_scores(HARD_EN, "en")
    assert easy and hard
    assert easy["formula"] == "flesch" and easy["score"] > 80
    assert hard["score"] < 10 and ts.is_very_difficult(hard)
    assert not ts.is_very_difficult(easy)
    assert easy["sentences"] == 40 and easy["grade"] < 4


def test_atesman_formula_and_turkish_syllables() -> None:
    assert ts.syllables_tr("okula") == 3 and ts.syllables_tr("ığdır") == 2
    assert ts.syllables_tr("İstanbul") == 3
    r = ts.readability_scores(EASY_TR, "tr")
    assert r and r["formula"] == "atesman" and r["score"] > 70
    # the formula by hand: 198.825 - 40.175*spw - 2.610*wps
    expect = 198.825 - 40.175 * r["avg_syllables_per_word"] - 2.610 * r["avg_sentence_words"]
    assert r["score"] == pytest.approx(expect, abs=1.0)
    hard = ts.readability_scores(HARD_TR, "tr")
    assert hard and ts.is_very_difficult(hard) and hard["score"] < 40


def test_long_sentence_share_and_counts() -> None:
    long_s = " ".join(["word"] * 30) + ". "
    r = ts.readability_scores(long_s * 2 + "Short one. Short two.", "en")
    assert r and r["long_sentence_share"] == 0.5 and r["sentences"] == 4


def test_unsupported_language_gives_no_number() -> None:
    data, findings, nc = mr.analyze(
        text="Das ist ein Satz. " * 40, lang="de", title="t", h1=[], description="",
        outline=[], url="https://x.test/", keywords=[],
    )  # fmt: skip
    assert data.readability is None and not findings
    assert ("readability", "language-unsupported") in {(n.check_id, n.reason_code) for n in nc}


def test_turkish_fold_dotted_and_dotless_i() -> None:
    assert ts.fold("İSTANBUL") == ts.fold("istanbul") == ts.fold("ISTANBUL") == "istanbul"
    assert ts.fold("ILIK") == ts.fold("ılık")
    assert ts.tokens("İstanbul'da Işık") == ["istanbul'da", "işik"]


def test_turkish_terms_are_shown_with_the_spelling_of_the_page_not_the_folded_form() -> None:
    text = "Programı seç. Bağımsız kafe pazarı için programı seç. " * 12
    data, _, _ = mr.analyze(
        text=text, lang="tr", title="Programı seç", h1=[], description="",
        outline=[], url="https://x.test/", keywords=[],
    )  # fmt: skip
    shown = {t["term"] for t in data.terms} | {p["term"] for p in data.phrases}
    assert {"programı", "bağımsız", "pazarı", "programı seç"} <= shown
    assert not {"programi", "bagimsiz", "bağimsiz", "pazari"} & shown
    assert "programı" in {t["term"] for t in data.title_terms}
    assert ts.show("programi sec", ts.display_forms("Programı sec")) == "programı sec"


def test_stopwords_and_top_terms() -> None:
    assert "the" in ts.stopwords("en") and "ve" in ts.stopwords("tr")
    assert ts.fold("için") in ts.stopwords("tr")
    toks = ts.tokens("the best coffee beans and the best coffee grinder for the coffee shop " * 3)
    out = ts.top_terms(toks, ts.stopwords("en"))
    assert out["terms"][0]["term"] == "coffee"
    assert all(t["term"] != "the" for t in out["terms"])
    assert "best coffee" in {p["term"] for p in out["phrases"]}
    assert all(not p["term"].startswith("the ") for p in out["phrases"])


def test_no_turkish_stemming() -> None:
    toks = ts.tokens("araba arabanın arabada araba")
    out = ts.top_terms(toks, frozenset())
    assert {t["term"] for t in out["terms"]} == {"araba", "arabanin", "arabada"}


def test_parse_keywords() -> None:
    assert ts.parse_keywords("kahve, Kahve ; çay\nsüt") == ["kahve", "çay", "süt"]
    assert ts.parse_keywords(["a", " a ", "b"]) == ["a", "b"]
    assert ts.parse_keywords(None) == []
    assert len(ts.parse_keywords([str(i) for i in range(50)])) == 10


def _check(**kw: object) -> mr.KeywordCheck:
    base = dict(body=[], title="", h1=[], description="", url="https://x.test/", subheadings=[])
    base.update(kw)
    return mr.check_keyword("İstanbul kahve", **base)  # type: ignore[arg-type]


def test_keyword_placement_and_density_turkish_case() -> None:
    body = ts.tokens("istanbul kahve rehberi " + "dolgu " * 90 + "ISTANBUL KAHVE")
    k = _check(
        body=body, title="En iyi İstanbul Kahve dükkanları", h1=["Istanbul kahve"],
        description="kahve", url="https://x.test/istanbul-kahve", subheadings=["Kahve"],
    )  # fmt: skip
    assert k.in_title and k.in_h1 and k.in_url and k.in_first_100_words
    assert not k.in_description and not k.in_subheadings  # phrase must match whole
    assert k.occurrences == 2
    assert k.density_percent == pytest.approx(100 * 2 * 2 / len(body), abs=0.01)


def test_keyword_findings_stuffing_and_missing() -> None:
    text = "kahve " * 100 + "başka bir şey"
    data, findings, nc = mr.analyze(
        text=text, lang="tr", title="Çay", h1=["Çay"], description="Çay rehberi", outline=[],
        url="https://x.test/", keywords=["kahve"],
    )  # fmt: skip
    ids = {f.id for f in findings}
    assert {"keyword-missing-in-title", "keyword-missing-in-h1", "keyword-missing-in-description",
            "keyword-stuffing"} <= ids  # fmt: skip
    assert all(f.evidence and "kahve" in f.evidence for f in findings if f.id.startswith("keyword"))
    assert data.keywords[0].density_percent > 3
    assert not any(n.check_id == "keyword-targeting" for n in nc)


def test_no_keywords_gives_not_checked_and_suggestions() -> None:
    text = "green tea benefits and green tea brewing guide. " * 12
    data, findings, nc = mr.analyze(
        text=text, lang="en", title="t", h1=[], description="", outline=[],
        url="https://x.test/", keywords=[],
    )  # fmt: skip
    assert ("keyword-targeting", "no-keywords") in {(n.check_id, n.reason_code) for n in nc}
    assert data.suggested_targets and "green tea" in data.suggested_targets[0]
    assert not any(f.id.startswith("keyword") for f in findings)


def test_readability_findings_registered_with_messages() -> None:
    from seoprobe.core.rules import catalog

    for rid in mr.RULE_IDS:
        d = catalog.get(rid)
        assert d.category.value == "content"
        for key in (
            d.message_key,
            d.recommendation_key,
            d.failure_check_key,
            d.leading_indicator_key,
        ):
            assert catalog.has_text(key)
            assert catalog.message(key, "en") != key and catalog.message(key, "tr") != key


def test_hostile_inputs_are_fast() -> None:
    huge = "kelime başka şey. " * 400_000
    t0 = time.monotonic()
    data, _f, _nc = mr.analyze(
        text=huge, lang="tr", title="", h1=[], description="", outline=[],
        url="https://x.test/", keywords=["kelime", "yok yok"],
    )  # fmt: skip
    assert time.monotonic() - t0 < 1.5  # generous: CI noise
    empty, f2, nc2 = mr.analyze(
        text="", lang="en", title="", h1=[], description="", outline=[],
        url="https://x.test/", keywords=[],
    )  # fmt: skip
    assert empty.readability is None and not f2 and nc2
    assert ts.readability_scores("", "en") is None


async def test_module_run_reads_parsed_page() -> None:
    html = (
        "<html lang='en'><head><title>Green tea guide</title>"
        "<meta name='description' content='All about green tea'></head><body><h1>Green tea</h1>"
        f"<p>{'Green tea is a drink made from leaves. ' * 20}</p></body></html>"
    )
    url = "https://x.test/tea"
    async with fakes.session({url: fakes.html_page(html)}) as s:
        res = await readability(url, keywords="green tea, matcha", lang="en", session=s)
    assert res.ok and res.operation == "readability"
    assert {k.keyword for k in res.data.keywords} == {"green tea", "matcha"}  # type: ignore[union-attr]
    ids = [(f.id, f.evidence) for f in res.findings]
    assert ("keyword-missing-in-title", "matcha") in ids
    assert ("keyword-missing-in-title", "green tea") not in ids
