"""check_urls and check_redirects on a redirect-following fake client."""

from __future__ import annotations

import json

from seoprobe.core.config import Config
from seoprobe.core.session import Session
from seoprobe.features.monitoring import check_redirects, check_urls
from seoprobe.features.monitoring.batch import _defaults
from tests import fakes

from .support import RedirectingHttp, redirect, robots_txt, session

H = "https://example.com"
OTHER = "https://other.test"
OK = fakes.html_page("<html><head><title>A</title></head><body><h1>A</h1><p>text</p></body></html>")
ROBOTS = robots_txt("User-agent: *\nDisallow: /private\n")


def _page(head: str = "", **kw: object) -> tuple[int, dict[str, str], bytes]:
    return fakes.html_page(f"<html><head><title>t</title>{head}</head><body><p>x</p></body></html>", **kw)  # type: ignore[arg-type]


def _by_url(res: object) -> dict[str, object]:
    return {row.url: row for row in res.data.items}  # type: ignore[attr-defined]


# ----------------------------------------------------------------------------- check_urls


async def test_check_urls_reports_each_signal_and_an_indexable_verdict() -> None:
    pages = {
        f"{H}/robots.txt": ROBOTS,
        f"{H}/ok": OK,
        f"{H}/noindex": _page('<meta name="robots" content="noindex, follow">'),
        f"{H}/header": _page(headers={"X-Robots-Tag": "googlebot: noindex"}),
        f"{H}/canon": _page(f'<link rel="canonical" href="{H}/ok">'),
        f"{H}/self": _page(f'<link rel="canonical" href="{H}/self/">'),
        f"{H}/old": redirect(f"{H}/ok"),
        f"{H}/private/x": OK,
        f"{H}/gone": fakes.html_page("gone", status=410),
    }
    urls = [f"{H}/{p}" for p in ("ok", "noindex", "header", "canon", "self", "old", "private/x", "gone")]
    async with session(pages) as s:
        res = await check_urls([*urls, "not a url"], session=s)
        requested = list(s.http.requested)  # type: ignore[attr-defined]
    assert res.status == "ok" and res.data is not None
    rows = _by_url(res)
    assert rows[f"{H}/ok"].indexable and rows[f"{H}/ok"].reasons == []  # type: ignore[attr-defined]
    assert rows[f"{H}/self"].indexable and not rows[f"{H}/self"].canonical_elsewhere  # type: ignore[attr-defined]
    assert rows[f"{H}/noindex"].reasons == ["noindex-meta"]  # type: ignore[attr-defined]
    assert rows[f"{H}/header"].reasons == ["noindex-header"]  # type: ignore[attr-defined]
    assert rows[f"{H}/header"].x_robots_tag == "googlebot: noindex"  # type: ignore[attr-defined]
    canon = rows[f"{H}/canon"]
    assert canon.canonical == f"{H}/ok" and canon.reasons == ["canonical-elsewhere"]  # type: ignore[attr-defined]
    old = rows[f"{H}/old"]
    assert old.final_url == f"{H}/ok" and old.hops == 1 and old.status == 200  # type: ignore[attr-defined]
    assert [h.status for h in old.redirect_chain] == [301] and old.reasons == ["redirected"]  # type: ignore[attr-defined]
    assert rows[f"{H}/private/x"].robots_allowed is False  # type: ignore[attr-defined]
    assert rows[f"{H}/private/x"].reasons == ["robots-disallowed"]  # type: ignore[attr-defined]
    assert rows[f"{H}/gone"].reasons == ["http-error"]  # type: ignore[attr-defined]
    assert rows["not a url"].error == "invalid-url" and rows["not a url"].reasons == ["invalid-url"]  # type: ignore[attr-defined]
    assert requested.count(f"{H}/robots.txt") == 1  # robots.txt fetched once per origin
    ids = {(f.id, f.url) for f in res.findings}
    assert ("url-check-noindex", f"{H}/noindex") in ids and ("url-check-noindex", f"{H}/header") in ids
    assert ("url-check-canonical-elsewhere", f"{H}/canon") in ids
    assert ("url-check-redirected", f"{H}/old") in ids
    assert ("url-check-robots-blocked", f"{H}/private/x") in ids
    assert ("url-check-broken", f"{H}/gone") in ids and ("url-check-broken", "not a url") in ids
    assert not [f for f in res.findings if f.url in (f"{H}/ok", f"{H}/self")]
    assert [r.url for r in res.data.items] == [*urls, "not a url"]  # input order


async def test_robots_is_fetched_once_per_origin_and_can_be_skipped() -> None:
    pages = {f"{H}/robots.txt": ROBOTS, f"{OTHER}/robots.txt": robots_txt("User-agent: *\nDisallow: /\n")}
    pages |= {f"{H}/{i}": OK for i in range(5)} | {f"{OTHER}/{i}": OK for i in range(5)}
    urls = [f"{H}/{i}" for i in range(5)] + [f"{OTHER}/{i}" for i in range(5)]
    async with session(pages) as s:
        res = await check_urls(urls, session=s)
        assert s.http.requested.count(f"{OTHER}/robots.txt") == 1  # type: ignore[attr-defined]
    assert res.data is not None
    assert [r.robots_allowed for r in res.data.items] == [True] * 5 + [False] * 5
    async with session(pages) as s:
        res = await check_urls(urls, check_robots=False, session=s)
        assert not [u for u in s.http.requested if u.endswith("/robots.txt")]  # type: ignore[attr-defined]
    assert res.data is not None and all(r.robots_allowed is None and r.indexable for r in res.data.items)


async def test_a_named_user_agent_group_is_judged() -> None:
    pages = {f"{H}/robots.txt": robots_txt("User-agent: Bingbot\nDisallow: /\n"), f"{H}/a": OK}
    async with session(pages) as s:
        google = await check_urls([f"{H}/a"], session=s)
        bing = await check_urls([f"{H}/a"], user_agent="Bingbot", session=s)
    assert google.data is not None and google.data.items[0].robots_allowed is True
    assert bing.data is not None and bing.data.items[0].robots_allowed is False and bing.data.user_agent == "Bingbot"


async def test_unreachable_robots_is_not_checked_rather_than_guessed() -> None:
    async with session({f"{H}/a": OK}, errors={f"{H}/robots.txt": "unreachable"}) as s:
        res = await check_urls([f"{H}/a"], session=s)
    assert res.status == "partial" and res.data is not None
    assert res.data.items[0].robots_allowed is None and res.data.items[0].indexable
    assert [(n.check_id, n.reason_code) for n in res.not_checked] == [
        ("url-check-robots-blocked", "robots-unreachable")
    ]


async def test_sitemap_membership_and_its_two_findings() -> None:
    pages = {
        f"{H}/robots.txt": ROBOTS,
        f"{H}/in": OK,
        f"{H}/out": OK,
        f"{H}/bad": _page('<meta name="robots" content="none">'),
    }
    sitemap = ["http://www.example.com/in/", f"{H}/bad"]
    async with session(pages) as s:
        res = await check_urls([f"{H}/in", f"{H}/out", f"{H}/bad"], sitemap_urls=sitemap, session=s)
    assert res.data is not None and res.data.sitemap_checked
    assert [r.in_sitemap for r in res.data.items] == [True, False, True]
    ids = {(f.id, f.url) for f in res.findings}
    assert ("url-check-not-in-sitemap", f"{H}/out") in ids
    assert ("url-check-sitemap-not-indexable", f"{H}/bad") in ids
    assert not [f for f in res.findings if f.url == f"{H}/in"]


async def test_check_urls_respects_the_batch_limit_and_dedupes() -> None:
    cfg = Config(max_batch_urls=3)
    urls = [f"{H}/{i}" for i in range(6)] + [f"{H}/0", "  "]
    async with session({f"{H}/robots.txt": ROBOTS}, config=cfg) as s:
        res = await check_urls(urls, session=s)
        fetched = [u for u in s.http.requested if not u.endswith((".txt",))]  # type: ignore[attr-defined]
    assert res.data is not None
    assert res.data.checked == 3 and res.data.skipped == 3 and res.data.duplicates == 1
    assert sorted(fetched) == [f"{H}/0", f"{H}/1", f"{H}/2"]
    assert res.status == "partial"


async def test_concurrency_is_bounded() -> None:
    import asyncio

    in_flight = peak = 0

    class Slow(RedirectingHttp):
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = None):  # type: ignore[no-untyped-def]
            nonlocal in_flight, peak
            if url.endswith("robots.txt") or url.endswith("llms.txt"):
                return await super().fetch_bytes(url, max_bytes=max_bytes)
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return await super().fetch_bytes(url, max_bytes=max_bytes)

    cfg = Config()
    pages = {f"{H}/{i}": OK for i in range(30)}
    async with Session(cfg, http=Slow(pages, None, cfg)) as s:  # type: ignore[arg-type]
        res = await check_urls(list(pages), concurrency=4, check_robots=False, session=s)
    assert res.data is not None and res.data.checked == 30 and res.data.concurrency == 4
    assert 1 < peak <= 4


async def test_internal_addresses_are_rejected_by_the_real_client() -> None:
    async with Session(Config()) as s:
        res = await check_urls(["http://127.0.0.1/admin", "http://10.0.0.1/"], session=s)
    assert res.data is not None
    assert [r.error for r in res.data.items] == ["target-rejected", "target-rejected"]
    assert all(not r.indexable and r.reasons == ["fetch-error"] for r in res.data.items)


async def test_check_urls_result_is_json_and_carries_no_key() -> None:
    cfg = Config(google_api_key="AIza-SECRET-KEY-123", bing_api_key="BING-SECRET")
    async with session({f"{H}/robots.txt": ROBOTS, f"{H}/a": OK}, config=cfg) as s:
        res = await check_urls([f"{H}/a"], session=s)
    text = res.to_json()
    assert json.loads(text)["data"]["items"][0]["indexable"] is True
    assert "SECRET" not in text and all("SECRET" not in r.url for r in res.meta.requests)
    assert f"{H}/a" in {r.url for r in res.meta.requests}  # robots.txt is logged by the nested robots op


# ----------------------------------------------------------------------------- check_redirects


async def test_check_redirects_classifies_each_row() -> None:
    pages = {
        f"{H}/a": redirect(f"{H}/new-a"),
        f"{H}/new-a": OK,
        f"{H}/b": redirect(f"{H}/"),
        f"{H}/": OK,
        f"{H}/c": OK,
        f"{H}/d": redirect(f"{H}/d1"),
        f"{H}/d1": redirect(f"{H}/d2", 308),
        f"{H}/d2": redirect(f"{H}/new-d"),
        f"{H}/new-d": OK,
        f"{H}/e": redirect(f"{H}/e1"),
        f"{H}/e1": redirect(f"{H}/e"),
        f"{H}/f": redirect(f"{H}/new-f"),
        f"{H}/new-f": fakes.html_page("x", status=404),
        f"{H}/g": redirect("/new-g", 302),
        f"{H}/new-g": OK,
        f"{H}/h": OK,
    }
    mapping = {
        f"{H}/a": f"{H}/new-a",
        f"{H}/b": f"{H}/new-b",
        f"{H}/c": f"{H}/new-c",
        f"{H}/d": f"{H}/new-d",
        f"{H}/e": f"{H}/new-e",
        f"{H}/f": f"{H}/new-f",
        f"{H}/g": "/new-g",
        f"{H}/h": f"{H}/h",
    }
    async with session(pages) as s:
        res = await check_redirects(mapping, session=s)
    assert res.ok and res.data is not None
    rows = {r.old: r for r in res.data.items}
    a = rows[f"{H}/a"]
    assert a.matches_expected and a.issues == [] and a.hops == 1 and a.statuses == [301, 200]
    assert a.final_url == f"{H}/new-a"
    assert rows[f"{H}/b"].issues == ["wrong-target"] and rows[f"{H}/b"].final_url == f"{H}/"
    assert rows[f"{H}/c"].issues == ["not-redirected"] and rows[f"{H}/c"].statuses == [200]
    d = rows[f"{H}/d"]
    assert d.matches_expected and d.hops == 3 and d.statuses == [301, 308, 301, 200] and d.issues == ["chain-too-long"]
    e = rows[f"{H}/e"]
    assert e.loop and e.issues == ["loop"] and e.final_url is None and not e.matches_expected
    assert rows[f"{H}/f"].issues == ["target-error"] and rows[f"{H}/f"].final_status == 404
    g = rows[f"{H}/g"]
    assert g.expected == f"{H}/new-g" and g.matches_expected and g.issues == ["temporary-redirect"]
    assert rows[f"{H}/h"].issues == [] and rows[f"{H}/h"].matches_expected  # an unchanged URL needs no redirect
    found = {(f.id, f.url) for f in res.findings}
    assert found == {
        ("redirect-wrong-target", f"{H}/b"),
        ("redirect-not-redirected", f"{H}/c"),
        ("redirect-too-many-hops", f"{H}/d"),
        ("redirect-loop", f"{H}/e"),
        ("redirect-target-error", f"{H}/f"),
        ("redirect-temporary", f"{H}/g"),
    }
    hops = next(f for f in res.findings if f.id == "redirect-too-many-hops")
    assert "max 1" in (hops.evidence or "")  # the threshold travels with the finding


async def test_check_redirects_accepts_pairs_and_thresholds() -> None:
    pages = {f"{H}/d": redirect(f"{H}/d1", 307), f"{H}/d1": redirect(f"{H}/x"), f"{H}/x": OK}
    async with session(pages) as s:
        res = await check_redirects([(f"{H}/d", f"{H}/x")], max_hops=2, temporary_statuses=[302], session=s)
    assert res.data is not None and res.data.items[0].issues == [] and res.data.max_hops == 2
    assert res.data.temporary_statuses == [302]


async def test_normalised_comparison_of_the_target() -> None:
    target = "https://EXAMPLE.com:443/new?utm_source=x"
    pages = {f"{H}/a": redirect(target), target: OK}
    async with session(pages) as s:
        res = await check_redirects({f"{H}/a": f"{H}/new"}, session=s)
    assert res.data is not None and res.data.items[0].matches_expected


async def test_unreachable_and_invalid_rows_and_the_batch_limit() -> None:
    cfg = Config(max_batch_urls=2)
    mapping = [("nope", f"{H}/x"), (f"{H}/down", f"{H}/x"), (f"{H}/later", f"{H}/x")]
    async with session({}, errors={f"{H}/down": "unreachable"}, config=cfg) as s:
        res = await check_redirects(mapping, session=s)
    assert res.data is not None and res.data.checked == 2 and res.data.skipped == 1 and res.status == "partial"
    invalid, down = res.data.items
    assert invalid.error == "invalid-url" and invalid.issues == ["target-error"]
    assert down.error == "unreachable" and down.issues == ["target-error"]


async def test_too_many_redirects_without_a_repeat_is_an_error_and_a_long_chain() -> None:
    pages = {f"{H}/r{i}": redirect(f"{H}/r{i + 1}") for i in range(10)}
    async with session(pages) as s:
        res = await check_redirects({f"{H}/r0": f"{H}/r9"}, session=s)
    row = res.data.items[0]  # type: ignore[union-attr]
    assert row.error == "redirect-loop" and not row.loop and row.hops == 6
    assert row.issues == ["wrong-target", "target-error", "chain-too-long"]


async def test_check_redirects_result_is_json() -> None:
    async with session({f"{H}/a": redirect(f"{H}/b"), f"{H}/b": OK}) as s:
        res = await check_redirects({f"{H}/a": f"{H}/b"}, session=s)
    data = json.loads(res.to_json())["data"]
    assert data["items"][0]["chain"] == [{"url": f"{H}/a", "status": 301}]


def test_defaults_come_from_the_data_file() -> None:
    d = _defaults()
    assert d["check_redirects"]["max_hops"] == 1 and d["check_redirects"]["temporary_statuses"] == [302, 307]
    assert d["check_urls"]["concurrency"] > 0
