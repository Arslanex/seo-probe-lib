"""suggest_links: relevance order, existing links, determinism, thresholds, limits, JSON, rule."""

from __future__ import annotations

import json
import random

from seoprobe.core.config import Config
from seoprobe.core.page import Page
from seoprobe.features.crawl import FEATURE, LinkSuggestions, LinkSummary, PageLinkStats, suggest_links
from seoprobe.features.crawl.duplicates import fold
from seoprobe.features.crawl.suggest import RULE_ID, defaults, stopwords_for, terms
from tests import fakes

HOST = "https://example.com"

COFFEE = "espresso roast beans grinder brewing arabica barista crema latte filter kettle aroma".split()
BIKES = "bicycle saddle gears chain pedals helmet tyres brakes frame wheels cycling derailleur".split()
GARDEN = "tomatoes compost seedlings watering soil mulch pruning roses greenhouse harvest weeds shovel".split()
COMMON = "the guide for our readers explains every step with care and simple tips".split()


def text(vocab: list[str], seed: int, n: int = 120) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(vocab) if i % 3 else rng.choice(COMMON) for i in range(n))


def pg(path: str, vocab: list[str], seed: int, *, links: tuple[str, ...] = (), title: str = "", h1: str = "") -> Page:
    url = f"{HOST}{path}"
    anchors = "".join(f'<a href="{h}">more</a> ' for h in links)
    html = (
        f'<html lang="en"><head><title>{title or path}</title></head><body><main><article>'
        f"<h1>{h1 or path}</h1><p>{text(vocab, seed)}</p><p>{anchors}</p></article></main></body></html>"
    )
    return Page(url=url, final_url=url, status=200, content_type="text/html", html=html)


def site() -> list[Page]:
    return [
        pg("/", COMMON + COFFEE[:2] + BIKES[:2], 0, links=("/coffee/b", "/coffee/c", "/bikes/a", "/bikes/b")),
        pg("/coffee/a", COFFEE, 1, title="How to brew espresso", h1="Brewing espresso at home"),
        pg("/coffee/b", COFFEE, 2, links=("/coffee/c",)),
        pg("/coffee/c", COFFEE, 3, links=("/coffee/b",)),
        pg("/bikes/a", BIKES, 4, links=("/bikes/b",)),
        pg("/bikes/b", BIKES, 5, links=("/bikes/a",)),
        pg("/garden/a", GARDEN, 6),
    ]


async def run(pages: list[Page], config: Config | None = None, **kw):  # type: ignore[no-untyped-def]
    async with fakes.session({}, config=config) as s:
        res = await suggest_links(pages, session=s, **kw)
        assert s.http.requested == []  # type: ignore[attr-defined]
    assert res.ok, res.error
    return res


def row(res, path: str):  # type: ignore[no-untyped-def]
    return next(r for r in res.data.targets if r.url == f"{HOST}{path}")


async def test_topic_close_pages_rank_first_and_other_topics_fall_below_the_threshold() -> None:
    res = await run(site())
    r = row(res, "/coffee/a")
    assert r.inlinks == 0
    sources = [s.source_url for s in r.suggestions]
    assert set(sources[:2]) == {f"{HOST}/coffee/b", f"{HOST}/coffee/c"}
    assert sources[2:] == [f"{HOST}/"]  # the home page mentions espresso and roast too
    sims = [s.similarity for s in r.suggestions]
    assert sims == sorted(sims, reverse=True) and all(x >= defaults()["min_similarity"] for x in sims)


async def test_without_a_threshold_every_other_topic_ranks_below_the_same_topic() -> None:
    res = await run(site(), targets=[f"{HOST}/coffee/a"], min_similarity=0.0, max_suggestions=10)
    sources = [s.source_url.removeprefix(HOST) for s in row(res, "/coffee/a").suggestions]
    assert set(sources[:2]) == {"/coffee/b", "/coffee/c"}
    assert set(sources[2:]) <= {"/", "/bikes/a", "/bikes/b", "/garden/a"}


async def test_sources_that_already_link_to_the_target_are_excluded() -> None:
    res = await run(site(), targets=[f"{HOST}/coffee/c"], min_similarity=0.0, max_suggestions=10)
    r = row(res, "/coffee/c")
    sources = {s.source_url for s in r.suggestions}
    assert f"{HOST}/coffee/b" not in sources and f"{HOST}/" not in sources  # both link to /coffee/c
    assert f"{HOST}/coffee/c" not in sources and f"{HOST}/coffee/a" in sources
    assert r.inlinks == 2


async def test_default_targets_are_pages_with_few_inlinks() -> None:
    res = await run(site())
    got = {r.url.removeprefix(HOST): r.inlinks for r in res.data.targets}
    assert got == {"/": 0, "/coffee/a": 0, "/garden/a": 0}  # every other page has 2 inlinks
    more = await run(site(), max_inlinks=2)
    assert len(more.data.targets) == 7 and more.data.thresholds["max_inlinks"] == 2


async def test_inlinks_come_from_the_link_summary_when_given() -> None:
    summary = LinkSummary(pages=[PageLinkStats(url=f"{HOST}/coffee/a", inlinks=9)])
    res = await run(site(), links=summary)
    assert f"{HOST}/coffee/a" not in {r.url for r in res.data.targets}


async def test_same_pages_give_the_same_output_in_any_order() -> None:
    a = await run(site())
    pages = site()
    shuffled = [pages[0], *random.Random(9).sample(pages[1:], len(pages) - 1)]
    b = await run(shuffled)
    assert sorted(a.data.targets, key=lambda r: r.url) == sorted(b.data.targets, key=lambda r: r.url)
    assert a.data == (await run(site())).data


async def test_ties_are_broken_by_url() -> None:
    twins = [pg("/t", COFFEE, 1), pg("/z", COFFEE, 2), pg("/a", COFFEE, 2)]  # /a and /z: same text
    res = await run(twins, targets=[f"{HOST}/t"])
    sugg = row(res, "/t").suggestions
    assert [s.source_url for s in sugg] == [f"{HOST}/a", f"{HOST}/z"] and sugg[0].similarity == sugg[1].similarity


async def test_thresholds_limit_the_suggestions() -> None:
    none = await run(site(), min_similarity=0.99)
    assert all(r.suggestions == [] for r in none.data.targets) and none.findings == []
    one = await run(site(), max_suggestions=1)
    assert len(row(one, "/coffee/a").suggestions) == 1
    assert one.data.thresholds["max_suggestions"] == 1 and none.data.thresholds["min_similarity"] == 0.99


async def test_anchor_candidates_are_title_h1_then_shared_terms_of_the_source() -> None:
    res = await run(site(), targets=[f"{HOST}/coffee/a"])
    best = row(res, "/coffee/a").suggestions[0]
    assert best.anchor_candidates[:2] == ["How to brew espresso", "Brewing espresso at home"]
    assert best.shared_terms and set(best.anchor_candidates[2:]) <= set(best.shared_terms)
    source = next(p for p in site() if p.url == best.source_url)
    assert source.parsed is not None
    folded = " ".join(fold(source.parsed.main_text or source.parsed.visible_text).split())
    assert all(t in folded for t in best.shared_terms)


async def test_one_finding_per_target_with_suggestions_and_the_thresholds_in_evidence() -> None:
    res = await run(site())
    with_sugg = [r for r in res.data.targets if r.suggestions]
    assert with_sugg and [f.url for f in res.findings] == [r.url for r in with_sugg]
    f = res.findings[0]
    assert f.id == RULE_ID and f.severity.value == "Info" and f.category.value == "links"
    assert f.count == len(with_sugg[0].suggestions)
    assert "(max 1)" in (f.evidence or "") and "(min 0.1)" in (f.evidence or "")


async def test_unknown_targets_short_pages_and_broken_pages() -> None:
    short = Page(url=f"{HOST}/short", final_url=f"{HOST}/short", status=200, html="<html><body>tiny</body></html>")
    broken = Page(url=f"{HOST}/gone", final_url=f"{HOST}/gone", status=404, html="<html></html>")
    res = await run([*site(), short, broken], targets=[f"{HOST}/short", f"{HOST}/nowhere", f"{HOST}/coffee/a/"])
    assert res.data.unknown_targets == [f"{HOST}/nowhere"]
    assert [r.url for r in res.data.targets] == [f"{HOST}/short", f"{HOST}/coffee/a"]
    assert row(res, "/short").suggestions == [] and res.data.pages_considered == 7


async def test_batch_and_page_caps_are_respected_and_reported() -> None:
    res = await run(site(), config=Config(max_batch_urls=1), targets=[f"{HOST}/coffee/a", f"{HOST}/garden/a"])
    assert res.data.targets_skipped == 1 and [r.url for r in res.data.targets] == [f"{HOST}/coffee/a"]
    capped = await run(site(), config=Config(max_pages_cap=3))
    assert capped.data.pages_skipped == 4 and capped.data.pages_considered == 3


def test_stopwords_of_the_page_language_are_dropped() -> None:
    assert fold("ama") in stopwords_for("tr-TR") and "the" in stopwords_for("en")
    assert "the" in stopwords_for("") and fold("ama") in stopwords_for("xx")
    counts, kept = terms("ama kahve çok güzel ama kahve", "tr", min_token_length=2)
    assert "ama" not in counts and counts["kahve"] == 2 and kept == 3


async def test_result_round_trips_through_json() -> None:
    res = await run(site())
    json.loads(res.to_json())
    assert res.data is not None
    assert LinkSuggestions.model_validate_json(res.data.model_dump_json()) == res.data


async def test_empty_input() -> None:
    res = await run([])
    assert res.data.targets == [] and res.findings == []


def test_rule_is_in_the_catalog_unscored_with_texts() -> None:
    d = FEATURE.catalog.rules[RULE_ID]
    assert d.scored is False and d.category.value == "links"
    msgs = FEATURE.catalog.messages
    for lang in ("en", "tr"):
        assert msgs[lang][d.message_key] and msgs[lang][d.recommendation_key]
