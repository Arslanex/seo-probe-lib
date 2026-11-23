"""Link analysis: categorisation, link health, depth, orphans, limits, SSRF, hostile input."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from seoprobe.core.config import Config
from seoprobe.core.models import AuditMode, Hop
from seoprobe.core.net.client import MAX_PAGE_BYTES, HttpClient, RawResponse
from seoprobe.core.page import Page
from seoprobe.core.session import Session
from seoprobe.features.crawl import links
from seoprobe.features.crawl.links import analyze_links
from seoprobe.features.site import parse_robots
from tests import fakes
from tests.features.crawl.support import HOST, page


class Scripted:
    """table: url -> (status, final_url, hops, error). Unknown URLs answer 200."""

    def __init__(self, table: dict[str, tuple[int | None, str | None, int, str | None]] | None = None):
        self.table = table or {}
        self.requested: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES) -> RawResponse:
        self.requested.append(url)
        status, final, hops, err = self.table.get(url, (200, url, 0, None))
        if err:
            return RawResponse(url, final or url, None, {}, b"", False, f"{err}: scripted", [], None, 1)
        chain = [Hop(url=url, status=301) for _ in range(hops)]
        return RawResponse(url, final or url, status, {}, b"", False, None, chain, 1, 1)

    async def fetch(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES):  # type: ignore[no-untyped-def]  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None


def doc(main: list[str] | None = None, nav: list[str] | None = None) -> str:
    """main/nav entries are ready-made ``<a ...>`` tags."""
    navs = f"<nav>{''.join(nav or [])}</nav>" if nav else ""
    return f'<html lang="en"><body>{navs}<main>{"".join(main or [])}</main></body></html>'


def a(href: str, text: str = "link", rel: str = "") -> str:
    return f'<a href="{href}"' + (f' rel="{rel}"' if rel else "") + f">{text}</a>"


def pg(
    path: str,
    html: str = "",
    *,
    status: int | None = 200,
    kind: str = "core",
    error: str | None = None,
) -> Page:
    return page(path, html, status=status, kind=kind, error=error)


async def run(pages: list[Page], fetcher: Scripted | HttpClient | None = None, **kw):  # type: ignore[no-untyped-def]
    kw.setdefault("entry_url", f"{HOST}/")
    kw.setdefault("sitemap_urls", set())
    return await analyze_links(pages, fetcher=fetcher or Scripted(), **kw)  # type: ignore[arg-type]


def ids(res) -> list[str]:  # type: ignore[no-untyped-def]
    return [f.id for f in res.output.findings]


# ----------------------------------------------------------------------------- categorisation


async def test_internal_external_split_skips_non_web_links_and_cleans_anchors() -> None:
    html = doc(
        main=[
            a("/a", "x " * 200),
            a("https://www.example.com/b"),
            a("https://blog.example.com/x"),  # a subdomain is another site
            a("https://other.org/y", rel="nofollow"),
            a("mailto:me@example.com"),
            a("tel:+900000"),
            a("#frag"),
            a("javascript:void(0)"),
        ],
        nav=[a("/home")],
    )
    res = await run([pg("/", html)])
    s = res.summary
    assert (s.internal_total, s.external_total, s.nofollow) == (3, 2, 1)
    assert s.external_unique == 2 and s.internal_unique == 3
    assert {d["domain"] for d in s.external_domains} == {"other.org", "blog.example.com"}
    by_target = {r.target_url: r for r in s.links}
    assert len(by_target[f"{HOST}/a"].anchor) <= 120
    assert by_target[f"{HOST}/home"].zone == "nav" and by_target[f"{HOST}/a"].zone == "main"
    assert by_target["https://other.org/y"].rel == ["nofollow"]
    assert "mailto" not in " ".join(by_target)


async def test_targets_that_are_sampled_pages_need_no_network() -> None:
    home = pg("/", doc(main=[a("/a")]))
    pa = pg("/a", doc(main=[a("/")]))
    fx = Scripted()
    res = await run([home, pa], fx)
    assert fx.requested == []
    assert all(r.checked and r.target_status == 200 for r in res.summary.links)
    assert res.summary.checked == 2 and res.summary.skipped == 0


async def test_sampled_404_target_is_broken_without_network() -> None:
    home = pg("/", doc(main=[a("/gone")]))
    gone = pg("/gone", "", status=404)
    fx = Scripted()
    res = await run([home, gone], fx)
    f = next(f for f in res.output.findings if f.id == "broken-internal-link")
    assert f.url == f"{HOST}/" and "404" in (f.evidence or "") and fx.requested == []
    assert res.summary.broken == 1


async def test_sampled_page_that_failed_to_fetch_is_broken() -> None:
    home = pg("/", doc(main=[a("/down")]))
    down = pg("/down", "", status=None, error="timeout")
    res = await run([home, down])
    assert "broken-internal-link" in ids(res)


# ----------------------------------------------------------------------------- link health


async def test_broken_internal_and_external_links_are_found_by_probing() -> None:
    html = doc(
        main=[
            a("/gone"),
            a("https://ext.example/dead"),
            a("https://ext.example/down"),
            a("https://ext.example/slow"),
        ]
    )
    fx = Scripted(
        {
            f"{HOST}/gone": (404, f"{HOST}/gone", 0, None),
            "https://ext.example/dead": (410, "https://ext.example/dead", 0, None),
            "https://ext.example/down": (None, None, 0, "unreachable"),
            "https://ext.example/slow": (None, None, 0, "timeout"),
        }
    )
    res = await run([pg("/", html)], fx)
    assert ids(res).count("broken-external-link") == 3 and ids(res).count("broken-internal-link") == 1
    ev = {f.id: f.evidence for f in res.output.findings if f.id == "broken-internal-link"}
    assert f"{HOST}/gone" in (ev["broken-internal-link"] or "") and "404" in (ev["broken-internal-link"] or "")
    assert res.summary.broken == 4 and res.summary.checked == 4


async def test_internal_redirect_link_is_reported_and_counted() -> None:
    fx = Scripted({f"{HOST}/old": (200, f"{HOST}/new", 1, None)})
    res = await run([pg("/", doc(main=[a("/old")]))], fx)
    f = next(f for f in res.output.findings if f.id == "internal-redirect-link")
    assert "/old" in (f.evidence or "") and "/new" in (f.evidence or "")
    assert res.summary.redirecting == 1 and res.summary.broken == 0


async def test_redirecting_sample_page_counts_as_redirect() -> None:
    home = pg("/", doc(main=[a("/old")]))
    moved = Page(url=f"{HOST}/old", final_url=f"{HOST}/new/", status=200, html="<html></html>")
    res = await run([home, moved], Scripted())
    assert "internal-redirect-link" in ids(res)


@pytest.mark.parametrize("status", [401, 403, 429, 999])
async def test_blocked_statuses_are_not_broken(status: int) -> None:
    fx = Scripted({"https://ext.example/p": (status, "https://ext.example/p", 0, None)})
    res = await run([pg("/", doc(main=[a("https://ext.example/p")]))], fx, mode="page")
    assert res.output.findings == [] and res.summary.broken == 0
    rec = res.summary.links[0]
    assert rec.error == "blocked" and rec.target_status == status and rec.checked


async def test_circuit_open_is_unchecked_not_broken() -> None:
    fx = Scripted({"https://ext.example/p": (None, None, 0, "circuit-open")})
    res = await run([pg("/", doc(main=[a("https://ext.example/p")]))], fx, mode="page")
    assert res.output.findings == [] and res.summary.skipped == 1 and not res.summary.links[0].checked


async def test_probe_exception_is_contained() -> None:
    class Boom(Scripted):
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES) -> RawResponse:
            raise RuntimeError("boom")

    res = await run([pg("/", doc(main=[a("https://ext.example/p")]))], Boom(), mode="page")
    assert ids(res) == ["broken-external-link"]


async def test_ssrf_targets_are_rejected_by_the_real_fetcher_and_never_counted_as_broken() -> None:
    html = doc(
        main=[
            a("http://10.0.0.1/admin"),
            a("http://169.254.169.254/latest/meta-data/"),
            a("http://localhost:9/x"),
        ]
    )
    async with HttpClient(Config()) as f:
        res = await run([pg("/", html)], f, mode="page")
    assert res.output.findings == [] and res.summary.broken == 0
    assert {r.error for r in res.summary.links} == {"target-rejected"}
    assert not any(r.checked for r in res.summary.links) and res.summary.skipped == 3


async def test_robots_disallowed_internal_targets_are_not_probed() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /private/\n")
    fx = Scripted()
    res = await run(
        [pg("/", doc(main=[a("/private/x"), a("/open")]))],
        fx,
        robots_rules=rules,
        bot_name="SEOAnalyzerBot/0.1",
    )
    assert fx.requested == [f"{HOST}/open"] and res.summary.skipped == 1


# ----------------------------------------------------------------------------- graph


async def test_click_depth_bfs_and_deep_page_in_site_mode() -> None:
    pages = [
        pg("/", doc(main=[a("/a")])),
        pg("/a", doc(main=[a("/b")])),
        pg("/b", doc(main=[a("/c")])),
        pg("/c", doc(main=[a("/d")])),
        pg("/d", doc(main=[a("/")])),
    ]
    res = await run(pages)
    depth = {s.url.removeprefix(HOST): s.depth for s in res.summary.pages}
    assert depth == {"/": 0, "/a": 1, "/b": 2, "/c": 3, "/d": 4} and res.summary.max_depth == 4
    deep = [f for f in res.output.findings if f.id == "deep-page"]
    assert [f.url for f in deep] == [f"{HOST}/d"] and "depth 4" in (deep[0].evidence or "")
    page_mode = await run(pages, mode="page")
    assert "deep-page" not in ids(page_mode)


async def test_unreachable_sampled_page_has_no_depth() -> None:
    res = await run([pg("/", doc(main=[a("/a")])), pg("/a", ""), pg("/island", doc(main=[a("/a")]))])
    assert {s.url.removeprefix(HOST): s.depth for s in res.summary.pages}["/island"] is None


async def test_inlinks_and_sitemap_membership() -> None:
    pages = [
        pg("/", doc(main=[a("/b"), a("/c")])),
        pg("/b", doc(main=[a("/c")])),
        pg("/c", doc(main=[a("/c"), a("/b")])),  # the self link must not count
    ]
    res = await run(pages, sitemap_urls={f"{HOST}/", f"{HOST}/b"})
    st = {s.url.removeprefix(HOST): s for s in res.summary.pages}
    assert st["/c"].inlinks == 2 and st["/b"].inlinks == 2 and st["/"].inlinks == 0
    assert st["/b"].in_sitemap is True and st["/c"].in_sitemap is False
    none_map = await run(pages)
    assert all(s.in_sitemap is None for s in none_map.summary.pages)


async def test_page_stats_count_outlinks_by_kind() -> None:
    res = await run([pg("/", doc(main=[a("/a"), a("/b"), a("https://x.org/")]))])
    s = res.summary.pages[0]
    assert (s.outlinks_internal, s.outlinks_external) == (2, 1)


# ----------------------------------------------------------------------------- orphans


async def test_orphan_pages_when_the_sample_covers_the_sitemap() -> None:
    pages = [pg("/", doc(main=[a("/a")])), pg("/a", doc(main=[a("/")])), pg("/x", doc(main=[]))]
    sm = {f"{HOST}/", f"{HOST}/a", f"{HOST}/x"}
    res = await run(pages, sitemap_urls=sm)
    assert res.summary.orphans == [f"{HOST}/x"]
    assert [f.url for f in res.output.findings if f.id == "orphan-page"] == [f"{HOST}/x"]


async def test_orphan_detection_is_not_attempted_with_a_small_sample() -> None:
    pages = [pg("/", doc(main=[a("/a")])), pg("/a", doc(main=[]))]
    sm = {f"{HOST}/", f"{HOST}/a"} | {f"{HOST}/u{i}" for i in range(20)}
    res = await run(pages, sitemap_urls=sm)
    assert res.summary.orphans == [] and "orphan-page" not in ids(res)
    assert [(n.check_id, n.reason_code) for n in res.output.not_checked] == [("orphan-page", "sample-too-small")]


async def test_orphans_need_a_sitemap_and_site_mode() -> None:
    res = await run([pg("/", doc(main=[]))])
    assert [(n.check_id, n.reason_code) for n in res.output.not_checked] == [("orphan-page", "sitemap-missing")]
    res = await run([pg("/", doc(main=[]))], mode="page", sitemap_urls={f"{HOST}/x"})
    assert res.output.not_checked == [] and "orphan-page" not in ids(res)


async def test_sitemap_urls_of_other_hosts_are_ignored_for_orphans() -> None:
    pages = [pg("/", doc(main=[a("/a")])), pg("/a", doc(main=[]))]
    res = await run(pages, sitemap_urls={f"{HOST}/", f"{HOST}/a", "https://elsewhere.org/z"})
    assert res.summary.orphans == []


# ----------------------------------------------------------------------------- page rules


async def test_too_many_links_threshold() -> None:
    many = doc(main=[a(f"/l{i}") for i in range(151)])
    few = doc(main=[a(f"/l{i}") for i in range(150)])
    assert "too-many-links" in ids(await run([pg("/", many)]))
    assert "too-many-links" not in ids(await run([pg("/", few)]))
    f = next(f for f in (await run([pg("/", many)])).output.findings if f.id == "too-many-links")
    assert f.count == 151


async def test_no_internal_links_evidence_says_a_link_to_itself_is_not_counted() -> None:
    page = doc(main=[a("/"), a("https://x.org/")])
    f = next(f for f in (await run([pg("/", page)])).output.findings if f.id == "no-internal-links")
    assert f.evidence == "0 links to other pages of the site (1 to the page itself)"


async def test_placeholder_links_flag_several_dead_links_with_visible_text() -> None:
    legal = [a("#", "Gizlilik"), a("#", "Koşullar"), a("javascript:void(0)", "İletişim")]
    res = await run([pg("/", doc(nav=legal, main=[a("/x")]))], mode="page")
    f = next(f for f in res.output.findings if f.id == "placeholder-links")
    assert f.count == 3 and "Gizlilik (#)" in (f.evidence or "")


async def test_placeholder_links_ignore_a_single_trigger_icon_only_and_real_links() -> None:
    one = doc(main=[a("#", "Menu"), a("/x")])
    icon_only = doc(main=[a("#", ""), a("#!", ""), a("/x")])
    real = doc(main=[a("/privacy", "Gizlilik"), a("/terms", "Koşullar")])
    for html in (one, icon_only, real):
        assert "placeholder-links" not in ids(await run([pg("/", html)], mode="page"))


async def test_placeholder_links_count_each_text_once() -> None:
    twice = doc(main=[a("#", "Top"), a("#", "Top"), a("#", "Top")])
    assert "placeholder-links" not in ids(await run([pg("/", twice)], mode="page"))


async def test_no_internal_links_only_in_site_mode_and_only_for_healthy_pages() -> None:
    only_ext = doc(main=[a("https://x.org/")])
    assert "no-internal-links" in ids(await run([pg("/", only_ext)]))
    assert "no-internal-links" not in ids(await run([pg("/", only_ext)], mode="page"))
    assert "no-internal-links" not in ids(await run([pg("/", only_ext, status=404)]))
    self_only = doc(main=[a("/")])
    assert "no-internal-links" in ids(await run([pg("/", self_only)]))


async def test_nofollow_internal_share() -> None:
    nf = doc(main=[a(f"/n{i}", rel="nofollow") for i in range(3)] + [a(f"/f{i}") for i in range(7)])
    ok = doc(main=[a("/n0", rel="nofollow")] + [a(f"/f{i}") for i in range(9)])
    tiny = doc(main=[a("/n0", rel="nofollow"), a("/n1", rel="nofollow")])
    assert "nofollow-internal-links" in ids(await run([pg("/", nf)]))
    assert "nofollow-internal-links" not in ids(await run([pg("/", ok)]))
    assert "nofollow-internal-links" not in ids(await run([pg("/", tiny)]))  # fewer than 5 links


async def test_findings_per_page_and_rule_are_capped() -> None:
    html = doc(main=[a(f"/dead{i}") for i in range(40)])
    fx = Scripted({f"{HOST}/dead{i}": (404, f"{HOST}/dead{i}", 0, None) for i in range(40)})
    res = await run([pg("/", html)], fx, max_internal_checks=100)
    assert ids(res).count("broken-internal-link") == 25 and res.summary.broken == 40


# ----------------------------------------------------------------------------- limits & hostile


async def test_internal_probe_limit_marks_the_rest_skipped() -> None:
    fx = Scripted()
    html = doc(main=[a(f"/i{i:03d}") for i in range(62)])
    res = await run([pg("/", html)], fx)
    assert len(fx.requested) == 60 and res.summary.skipped == 2 and res.summary.checked == 60


async def test_external_probe_limit_prefers_main_content_links() -> None:
    fx = Scripted()
    main = [a(f"https://m{i}.example/p") for i in range(3)]
    nav = [a(f"https://n{i}.example/p") for i in range(9)]
    res = await run([pg("/", doc(main=main, nav=nav))], fx, max_external_checks=5)
    assert len(fx.requested) == 5 and res.summary.skipped == 7
    assert {u for u in fx.requested if "//m" in u} == {f"https://m{i}.example/p" for i in range(3)}


async def test_probes_run_concurrently_but_within_the_limit() -> None:
    peak = {"now": 0, "max": 0}

    class Slow(Scripted):
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES) -> RawResponse:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.01)
            peak["now"] -= 1
            return await super().fetch_bytes(url, max_bytes=max_bytes)

    res = await run([pg("/", doc(main=[a(f"/s{i}") for i in range(20)]))], Slow(), concurrency=3)
    assert 1 < peak["max"] <= 3 and res.summary.checked == 20


async def test_record_cap_keeps_problematic_links_first() -> None:
    html = doc(main=[a(f"/ok{i}") for i in range(2990)] + [a("/dead")])
    fx = Scripted({f"{HOST}/dead": (404, f"{HOST}/dead", 0, None)})
    res = await run([pg("/", html)], fx, max_internal_checks=100)
    assert res.summary.internal_total == 2991 and len(res.summary.links) == 2000 and res.summary.truncated
    assert any(r.target_url == f"{HOST}/dead" for r in res.summary.links)


async def test_hostile_page_with_ten_thousand_links_is_fast_and_bounded() -> None:
    html = doc(main=[a(f"/h{i}") for i in range(6000)] + [a(f"https://e{i}.example/") for i in range(4000)])
    fx = Scripted()
    t0 = time.monotonic()
    res = await run([pg("/", html)], fx)
    assert time.monotonic() - t0 < 10
    assert res.summary.internal_total + res.summary.external_total == 10_000
    assert len(fx.requested) == 100 and len(res.summary.links) == 2000
    assert "too-many-links" in ids(res)


async def test_pages_without_html_are_ignored() -> None:
    res = await run([pg("/", ""), pg("/b", doc(main=[a("/c")]))])
    assert res.summary.internal_total == 1


async def test_malformed_hosts_do_not_crash() -> None:
    html = doc(main=[a("http://" + "a" * 70 + ".example/"), a("http://exa mple.com/"), a("http://[::1")])
    res = await run([pg("/", html)])
    assert res.summary.internal_total == 0


# ----------------------------------------------------------------------------- the operation


async def test_links_operation_matches_analyze_links_and_serialises() -> None:
    home = pg("/", doc(main=[a("/a"), a("/gone"), a("https://ext.example/p")]))
    pa = pg("/a", doc(main=[a("/")]))
    table = {
        f"{HOST}/gone": fakes.html_page("gone", status=404),
        "https://ext.example/p": fakes.html_page("ok"),
    }
    async with fakes.session(table, config=Config(request_delay_s=0)) as s:
        res = await links([home, pa], entry_url=f"{HOST}/", sitemap_urls=[f"{HOST}/", f"{HOST}/a"], session=s)
        direct = await analyze_links(
            [home, pa],
            entry_url=f"{HOST}/",
            sitemap_urls={f"{HOST}/", f"{HOST}/a"},
            fetcher=s.http,
            bot_name=s.config.bot_name,
        )
    assert res.ok and res.data is not None
    assert res.data == direct.summary
    assert [f.id for f in res.findings] == [f.id for f in direct.output.findings]
    assert "broken-internal-link" in [f.id for f in res.findings]
    json.loads(res.to_json())


async def test_links_operation_page_mode_and_robots() -> None:
    from seoprobe.features.site import RobotsReport

    rb = RobotsReport(status="ok", rules=parse_robots("User-agent: *\nDisallow: /private/\n"))
    fx = fakes.FakeHttp({}, config=Config(request_delay_s=0))
    async with Session(Config(request_delay_s=0), http=fx) as s:  # type: ignore[arg-type]
        res = await links(
            [pg("/", doc(main=[a("/private/x")]))], entry_url=f"{HOST}/", robots=rb, mode=AuditMode.PAGE, session=s
        )
    assert res.data is not None and res.data.skipped == 1 and fx.requested == []
    assert res.not_checked == [] and res.status == "ok"
