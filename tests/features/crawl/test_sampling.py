"""Stratified seeded sampling, and the crawl operation on the demo site (parity with the source)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from seoprobe.core.config import Config
from seoprobe.core.parse.urls import normalize_url
from seoprobe.core.result import Event
from seoprobe.core.session import Session
from seoprobe.features.crawl import crawl
from seoprobe.features.crawl.crawl import Candidate, select_sample
from tests import fakes
from tests.demo_site import BASE, build


def _cands() -> list[Candidate]:
    c = [
        Candidate(f"https://e.com/blog/post-{i}-x", lastmod=f"2026-01-{i % 28 + 1:02d}", in_sitemap=True)
        for i in range(300)
    ]
    c += [Candidate(f"https://e.com/urun/{i}", in_sitemap=True) for i in range(100)]
    c += [
        Candidate("https://e.com/about"),
        Candidate("https://e.com/contact"),
        Candidate("https://e.com/"),
    ]
    return c


def test_sample_is_deterministic_and_respects_n() -> None:
    a, _ = select_sample(_cands(), n=25, seed=7, mandatory=["https://e.com/"])
    b, _ = select_sample(_cands(), n=25, seed=7, mandatory=["https://e.com/"])
    assert [s.url for s in a] == [s.url for s in b] and len(a) == 25 and a[0].url == "https://e.com/"


def test_core_part_is_seed_independent_exploration_is_not() -> None:
    a, _ = select_sample(_cands(), n=25, seed=1, mandatory=["https://e.com/"])
    b, _ = select_sample(_cands(), n=25, seed=2, mandatory=["https://e.com/"])
    core = lambda s: {x.url for x in s if x.sample_kind == "core"}  # noqa: E731
    expl = lambda s: {x.url for x in s if x.sample_kind == "exploration"}  # noqa: E731
    assert core(a) == core(b) and expl(a) != expl(b)


def test_no_single_template_dominates_when_other_templates_can_fill_the_sample() -> None:
    c = _cands()
    c += [Candidate(f"https://e.com/news/story-{i}-a", in_sitemap=True) for i in range(200)]
    c += [Candidate(f"https://e.com/docs/guide-{i}-b", in_sitemap=True) for i in range(200)]
    s, stats = select_sample(c, n=30, seed=3, mandatory=["https://e.com/"])
    per: dict[str, int] = {}
    for x in s:
        per[x.template_key] = per.get(x.template_key, 0) + 1
    assert len(s) == 30 and max(per.values()) <= 11  # <= n/3 + the fixed representative
    assert {"/blog/*", "/urun/*", "/news/*", "/docs/*", "/about", "/contact"} <= set(per)
    assert stats["/blog/*"]["discovered"] == 300


def test_cap_relaxes_only_when_nothing_else_can_fill_the_sample() -> None:
    s, _ = select_sample(_cands(), n=30, seed=3, mandatory=["https://e.com/"])
    assert len(s) == 30  # two big templates + three singletons: the cap must give way to reach n


def test_small_sites_are_sampled_completely() -> None:
    cands = [Candidate(f"https://e.com/p{i}") for i in range(5)]
    s, _ = select_sample(cands, n=25, seed=1)
    assert len(s) == 5


def test_duplicates_are_collapsed() -> None:
    cands = [Candidate("https://e.com/a"), Candidate("http://www.e.com/a/")]
    s, _ = select_sample(cands, n=10, seed=1)
    assert len(s) == 1


# ----------------------------------------------------------------------------- the operation


def _snapshot(fixtures: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((fixtures / "parity" / "site_home.json").read_text("utf-8"))
    return data


async def test_crawl_on_the_demo_site_matches_the_source_snapshot(fixtures: Path) -> None:
    snap = _snapshot(fixtures)
    async with fakes.session(build(), config=Config(request_delay_s=0)) as s:
        res = await crawl(f"{BASE}/", seed=12345, session=s)
    assert res.ok and res.data is not None
    # crux/psi are added to the coverage by the audit's later stages
    expected = {k: v for k, v in snap["coverage"].items() if k not in ("crux", "psi")}
    assert res.data.coverage == expected
    got = sorted((p.url, p.sample_kind, res.data.templates[p.url]) for p in res.data.pages)
    want = sorted((p["url"], p["sample_kind"], p["template_key"]) for p in snap["pages"])
    assert got == want
    assert res.data.pages[0].url == f"{BASE}/"
    # the source's sitemap set: every <loc> of the demo sitemap, normalised; it agrees with the
    # snapshot's sitemap membership of the sampled pages
    body = build()[f"{BASE}/sitemap.xml"][2].decode()
    locs = sorted({n for loc in re.findall(r"<loc>([^<]+)</loc>", body) if (n := normalize_url(loc))})
    assert res.data.sitemap_urls == locs and len(locs) == snap["coverage"]["sitemap_urls"]
    in_map = {p["url"] for p in snap["links"]["pages"] if p["in_sitemap"]}
    assert in_map == {u for u in (p.url for p in res.data.pages) if u in locs}
    assert not res.data.partial_sample
    json.loads(res.to_json())


async def test_crawl_sitemap_urls_are_the_normalised_sitemap_locs() -> None:
    async with fakes.session(build(), config=Config(request_delay_s=0)) as s:
        res = await crawl(f"{BASE}/", seed=12345, session=s)
    assert res.data is not None
    assert res.data.sitemap_urls == sorted(res.data.sitemap_urls)
    assert len(res.data.sitemap_urls) == res.data.coverage["sitemap_urls"]
    assert all(u.startswith(BASE) for u in res.data.sitemap_urls)


async def test_crawl_emits_progress_and_reuses_given_robots_and_sitemap() -> None:
    events: list[Event] = []
    async with Session(Config(request_delay_s=0), http=fakes.FakeHttp(build()), on_event=events.append) as s:
        first = await crawl(f"{BASE}/", seed=12345, session=s)
        assert first.data is not None and first.findings
        from seoprobe.features.site import robots, sitemap

        rb = await robots(f"{BASE}/", session=s)
        sm = await sitemap(f"{BASE}/", robots_sitemaps=rb.data.sitemaps if rb.data else [], session=s)
        events.clear()
        again = await crawl(
            f"{BASE}/", entry=first.data.pages[0], robots=rb.data, sitemap=sm.data, seed=12345, session=s
        )
    assert again.data is not None and again.findings == [] and again.not_checked == []
    assert [p.url for p in again.data.pages] == [p.url for p in first.data.pages]
    stages = [e.data.get("stage") for e in events if e.kind == "progress" and e.operation == "crawl"]
    assert stages[:2] == ["discovery", "sample"] and set(stages[2:]) == {"fetch_pages"}
    last = [e.data for e in events if e.data.get("stage") == "fetch_pages"][-1]
    assert last["pages_fetched"] == last["pages_planned"] == len(again.data.pages)
    # no robots/sitemap/entry requests the second time: only operations that were not given run
    assert not any(e.operation in ("robots", "sitemap") for e in events)


async def test_crawl_includes_failed_fetches_and_reports_a_partial_sample() -> None:
    site = build()
    async with fakes.session(site, errors={f"{BASE}/gizlilik": "timeout"}, config=Config(request_delay_s=0)) as s:
        res = await crawl(f"{BASE}/", seed=12345, session=s)
    assert res.data is not None and res.data.partial_sample
    failed = next(p for p in res.data.pages if p.url == f"{BASE}/gizlilik")
    assert failed.error == "timeout"
    assert res.data.coverage["pages_fetched"] == res.data.coverage["pages_planned"] - 1


async def test_crawl_respects_max_pages_and_extra_urls() -> None:
    async with fakes.session(build(), config=Config(request_delay_s=0)) as s:
        res = await crawl(
            f"{BASE}/", seed=12345, max_pages=3, extra_urls=[f"{BASE}/gizlilik", "https://other.test/"], session=s
        )
    assert res.data is not None
    urls = [p.url for p in res.data.pages]
    assert len(urls) == 3 and urls[:2] == [f"{BASE}/", f"{BASE}/gizlilik"]
