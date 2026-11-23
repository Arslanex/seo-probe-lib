"""Site setup module: redirect matrix, soft-404, duplicate variants, headers, well-known files."""

from __future__ import annotations

import json
from typing import Any

import pytest

from seoprobe.core.config import Config
from seoprobe.core.models import FetchResult, Hop
from seoprobe.core.net.client import RawResponse
from seoprobe.core.net.safety import URLSafetyError, check_url
from seoprobe.core.page import Page as SitePage
from seoprobe.core.result import Result
from seoprobe.core.rules import catalog
from seoprobe.core.session import Session
from seoprobe.features.site import FEATURE, SiteSetup, site_setup
from seoprobe.features.site.setup import MAX_REQUESTS

ORIGIN = "https://example.com"
HOME_HTML = (
    "<html><head><title>Home</title><link rel='icon' href='/f.png'></head>"
    "<body><a href='/about'>About</a>" + "x" * 3000 + "</body></html>"
)
ABOUT_HTML = "<html><head><title>About us</title></head><body><a href='/'>Home</a></body></html>"
GOOD_404 = "<html><head><title>Not found</title></head><body><a href='/'>Home</a>" + "y" * 800 + "</body></html>"
Page = tuple[int, dict[str, str], bytes]


class Sim:
    """Serves ``site``; ('redirect', status, location) entries are followed like the real fetcher."""

    def __init__(self, site: dict[str, object]) -> None:
        self.site = site
        self.requested: list[str] = []
        self.bare_404 = False

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = 10_000_000) -> RawResponse:
        max_bytes = 10_000_000 if max_bytes is None else max_bytes
        self.requested.append(url)
        chain: list[Hop] = []
        cur = url
        for _ in range(6):
            try:
                check_url(cur)
            except URLSafetyError as exc:
                return RawResponse(url, cur, None, {}, b"", False, f"target-rejected: {exc}", chain)
            default: Page = (404, {"content-type": "text/plain"}, b"nope")
            if "seoprobe-probe" in cur and not self.bare_404:
                default = (404, {"content-type": "text/html"}, GOOD_404.encode())
            entry = self.site.get(cur, default)
            if entry[0] == "redirect":  # type: ignore[index]
                chain.append(Hop(url=cur, status=entry[1]))  # type: ignore[index]
                cur = entry[2]  # type: ignore[index]
                continue
            status, headers, body = entry  # type: ignore[misc]
            return RawResponse(
                url,
                cur,
                status,
                {k.lower(): v for k, v in headers.items()},
                body[:max_bytes],
                False,
                None,
                chain,
            )
        return RawResponse(url, cur, None, {}, b"", False, "redirect-loop", chain)

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:
        return None


def html_page(body: str, **headers: str) -> Page:
    return (
        200,
        {"content-type": "text/html", "content-encoding": "br", "etag": "x", **headers},
        body.encode(),
    )


def good_site() -> dict[str, object]:
    return {
        f"{ORIGIN}/": html_page(HOME_HTML),
        f"{ORIGIN}/about": html_page(ABOUT_HTML),
        "http://example.com/": ("redirect", 301, f"{ORIGIN}/"),
        "https://www.example.com/": ("redirect", 301, f"{ORIGIN}/"),
        f"{ORIGIN}/about/": ("redirect", 301, f"{ORIGIN}/about"),
        f"{ORIGIN}/ABOUT": (404, {"content-type": "text/html"}, GOOD_404.encode()),
        f"{ORIGIN}/index.html": ("redirect", 301, f"{ORIGIN}/"),
        f"{ORIGIN}/index.php": ("redirect", 301, f"{ORIGIN}/"),
        f"{ORIGIN}/.well-known/security.txt": (
            200,
            {"content-type": "text/plain"},
            b"Contact: mailto:s@example.com\n",
        ),
        f"{ORIGIN}/ads.txt": (200, {"content-type": "text/plain"}, b"google.com, pub-1, DIRECT"),
        f"{ORIGIN}/favicon.ico": (200, {"content-type": "image/x-icon"}, b"\x00\x00\x01"),
    }


class Inputs:
    """What the source module read from AnalysisInputs: the entry page, the sample, the fetcher."""

    def __init__(self, entry: SitePage, pages: list[SitePage], fetcher: Any) -> None:
        self.entry = entry
        self.pages = pages
        self.fetcher = fetcher


def make_inputs(
    site: dict[str, object],
    *,
    html: str = HOME_HTML,
    headers: dict[str, str] | None = None,
    status: int = 200,
) -> tuple[Inputs, Sim]:
    sim = Sim(site)
    h = {"content-type": "text/html", "content-encoding": "br", "etag": "x"} if headers is None else headers
    entry = SitePage(
        url=f"{ORIGIN}/",
        final_url=f"{ORIGIN}/",
        status=status,
        headers=h,
        content_type="text/html",
        html=html,
        html_bytes=len(html),
    )
    about = SitePage(url=f"{ORIGIN}/about", final_url=f"{ORIGIN}/about", status=200, html=ABOUT_HTML)
    return Inputs(entry, [about], sim), sim


async def run_inputs(inp: Inputs) -> Result[SiteSetup]:
    async with Session(Config(), http=inp.fetcher) as s:
        return await site_setup(inp.entry, pages=inp.pages, session=s)


async def run(site: dict[str, object], **kw):  # type: ignore[no-untyped-def]
    inp, sim = make_inputs(site, **kw)
    res = await run_inputs(inp)
    return res, sim


def ids(res) -> set[str]:  # type: ignore[no-untyped-def]
    return {f.id for f in res.findings}


async def test_clean_site_has_no_findings() -> None:
    res, sim = await run(good_site())
    assert ids(res) == set(), ids(res)
    data = res.data
    assert data is not None
    by = {r.name: r for r in data.redirects}  # type: ignore[attr-defined]
    assert by["http"].permanent is True and by["http"].final_url == f"{ORIGIN}/" and by["http"].hops == 1
    assert by["www-counterpart"].status == 301
    assert data.http_version == "unknown"  # type: ignore[attr-defined]
    assert data.files["security_txt"].present  # type: ignore[attr-defined]
    assert data.soft_404["detected"] is False  # type: ignore[attr-defined]
    assert data.custom_404 is True  # type: ignore[attr-defined]
    assert len(sim.requested) <= MAX_REQUESTS


async def test_request_budget_with_everything_enabled() -> None:
    html = HOME_HTML.replace("<link rel='icon' href='/f.png'>", "<link rel='manifest' href='/m.json'>")
    site = good_site()
    site[f"{ORIGIN}/m.json"] = (200, {"content-type": "application/json"}, b'{"name":"x"}')
    res, sim = await run(site, html=html)
    assert len(sim.requested) <= MAX_REQUESTS
    assert res.data.requests_made == len(sim.requested)  # type: ignore[attr-defined]


async def test_soft_404_detected_and_duplicates_suppressed() -> None:
    site = good_site()

    class All200(Sim):
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = 10_000_000) -> RawResponse:
            if "seoprobe-probe" in url:
                return RawResponse(
                    url,
                    url,
                    200,
                    {"content-type": "text/html"},
                    HOME_HTML.encode(),
                    False,
                    None,
                    [],
                )
            return await super().fetch_bytes(url, max_bytes=max_bytes)

    inp, _ = make_inputs(site)
    inp.fetcher = All200(site)
    res = await run_inputs(inp)
    assert "soft-404" in ids(res)
    assert {n.check_id for n in res.not_checked} >= {
        "trailing-slash-duplicate",
        "index-file-duplicate",
    }


async def test_soft_404_via_redirect_to_home() -> None:
    class Probe(Sim):
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = 10_000_000) -> RawResponse:
            if "seoprobe-probe" in url:
                return RawResponse(
                    url,
                    f"{ORIGIN}/",
                    200,
                    {"content-type": "text/html"},
                    HOME_HTML.encode(),
                    False,
                    None,
                    [Hop(url=url, status=302)],
                )
            return await super().fetch_bytes(url, max_bytes=max_bytes)

    inp, _ = make_inputs(good_site())
    inp.fetcher = Probe(good_site())
    res = await run_inputs(inp)
    f = next(f for f in res.findings if f.id == "soft-404")
    assert "redirect" in (f.evidence or "")


async def test_bare_404_page() -> None:
    inp, sim = make_inputs(good_site())
    sim.bare_404 = True
    res = await run_inputs(inp)
    assert res.data.custom_404 is False  # type: ignore[attr-defined]
    assert "no-custom-404" in ids(res)


async def test_duplicate_variants_found() -> None:
    site = good_site()
    site[f"{ORIGIN}/about/"] = html_page(ABOUT_HTML)  # no redirect, no canonical
    site[f"{ORIGIN}/ABOUT"] = html_page(ABOUT_HTML)
    site[f"{ORIGIN}/index.html"] = html_page(HOME_HTML)
    res, _ = await run(site)
    assert {"trailing-slash-duplicate", "uppercase-path-duplicate", "index-file-duplicate"} <= ids(res)


async def test_canonical_on_variant_silences_duplicate() -> None:
    site = good_site()
    canon = ABOUT_HTML.replace("<title>", f"<link rel='canonical' href='{ORIGIN}/about'><title>")
    site[f"{ORIGIN}/about/"] = html_page(canon)
    res, _ = await run(site)
    assert "trailing-slash-duplicate" not in ids(res)


async def test_headers_findings() -> None:
    res, _ = await run(
        good_site(),
        headers={"content-type": "text/html", "server": "nginx/1.18.0", "x-powered-by": "PHP/7.4"},
    )
    assert {"html-not-compressed", "no-cache-validators", "server-version-disclosed"} <= ids(res)
    res2, _ = await run(
        good_site(),
        headers={
            "content-type": "text/html",
            "content-encoding": "gzip",
            "last-modified": "x",
            "server": "nginx",
        },
    )
    assert not ({"html-not-compressed", "no-cache-validators", "server-version-disclosed"} & ids(res2))


async def test_small_html_not_flagged_for_compression() -> None:
    res, _ = await run(good_site(), html="<title>Home</title>", headers={"content-type": "text/html", "etag": "1"})
    assert "html-not-compressed" not in ids(res)


async def test_hsts_parsed() -> None:
    res, _ = await run(
        good_site(),
        headers={
            "content-type": "text/html",
            "strict-transport-security": "max-age=63072000; includeSubDomains; preload",
        },
    )
    assert res.data.hsts == {
        "present": True,
        "max_age": 63072000,
        "include_subdomains": True,
        "preload": True,
    }  # type: ignore[attr-defined]


async def test_favicon_security_txt_and_manifest() -> None:
    html = "<html><head><title>Home</title><link rel='manifest' href='/m.json'></head></html>"
    site = good_site()
    del site[f"{ORIGIN}/favicon.ico"]
    del site[f"{ORIGIN}/.well-known/security.txt"]
    site[f"{ORIGIN}/m.json"] = (200, {"content-type": "application/json"}, b"{not json")
    res, _ = await run(site, html=html)
    assert {"favicon-missing", "security-txt-missing", "manifest-invalid"} <= ids(res)
    site[f"{ORIGIN}/m.json"] = (
        200,
        {"content-type": "application/json"},
        json.dumps({"name": "x"}).encode(),
    )
    res2, _ = await run(site, html=html)
    assert "manifest-invalid" not in ids(res2)
    # a security.txt without Contact does not count
    site[f"{ORIGIN}/.well-known/security.txt"] = (
        200,
        {"content-type": "text/plain"},
        b"Expires: 2030",
    )
    res3, _ = await run(site, html=html)
    assert "security-txt-missing" in ids(res3)


async def test_redirect_loop_and_error_do_not_crash() -> None:
    site = good_site()
    site["http://example.com/"] = ("redirect", 302, "http://example.com/")
    site[f"{ORIGIN}/index.php"] = ("redirect", 301, f"{ORIGIN}/index.php")
    res, _ = await run(site)
    by = {r.name: r for r in res.data.redirects}  # type: ignore[attr-defined]
    assert by["http"].error == "redirect-loop" and by["http"].final_url is None
    assert by["index-php"].error == "redirect-loop"


async def test_ssrf_redirect_to_private_ip_is_recorded() -> None:
    site = good_site()
    site["http://example.com/"] = ("redirect", 301, "http://169.254.169.254/latest")
    res, _ = await run(site)
    by = {r.name: r for r in res.data.redirects}  # type: ignore[attr-defined]
    assert by["http"].error == "target-rejected"


async def test_hostile_huge_response() -> None:
    site = good_site()
    site[f"{ORIGIN}/about/"] = (
        200,
        {"content-type": "text/html"},
        b"<title>About us</title>" + b"<a " * 3_000_000,
    )
    res, _ = await run(site)
    assert res.data is not None


async def test_root_only_site_skips_slash_and_case_variants() -> None:
    site = good_site()
    inp, sim = make_inputs(site)
    inp.pages = []
    res = await run_inputs(inp)
    assert res.data.skipped["trailing-slash"] == "no-non-root-page"  # type: ignore[attr-defined]
    assert not any("/ABOUT" in u for u in sim.requested)


def test_no_overlap_with_transport_rules() -> None:
    RULES = catalog.rules()
    mine = {
        "trailing-slash-duplicate", "uppercase-path-duplicate", "index-file-duplicate", "soft-404",
        "no-custom-404", "html-not-compressed", "no-cache-validators", "server-version-disclosed",
        "favicon-missing", "manifest-invalid", "security-txt-missing",
    }  # fmt: skip
    assert mine <= set(RULES)
    # the redirect rules that already exist are reused, not duplicated
    assert {
        "http-no-https-redirect",
        "http-https-redirect-temporary",
        "www-not-consolidated",
    } <= set(RULES)
    assert "www-apex-not-redirected" not in RULES and "redirect-not-permanent" not in RULES
    for rid in mine:
        assert RULES[rid].scope == "site" and RULES[rid].category.value == "technical"


@pytest.mark.parametrize("rid", ["soft-404", "favicon-missing", "html-not-compressed"])
def test_messages_exist_in_both_languages(rid: str) -> None:
    messages = FEATURE.catalog.messages
    for key in (f"finding.{rid}", f"rec.{rid}", f"fail.{rid}", f"lead.{rid}"):
        assert messages["en"][key] and messages["tr"][key]


async def test_site_setup_from_a_url_is_json_serializable() -> None:
    site = good_site()

    class WithFetch(Sim):
        async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
            raw = await self.fetch_bytes(url)
            return FetchResult(
                url=url, final_url=raw.final_url, status=raw.status, headers=raw.headers,
                content_type=raw.headers.get("content-type"), html=raw.body.decode(), html_bytes=len(raw.body),
            )  # fmt: skip

    sim = WithFetch(site)
    async with Session(Config(), http=sim) as s:  # type: ignore[arg-type]
        res = await site_setup(f"{ORIGIN}/", session=s)
    assert res.ok and isinstance(res.data, SiteSetup) and res.findings == []
    assert res.data.requests_made == len(sim.requested) - 1  # the entry fetch is not in the budget
    assert '"operation":"site_setup"' in res.to_json()
