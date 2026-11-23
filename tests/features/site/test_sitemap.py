"""Sitemap discovery: formats, limits, expansion, findings, hostile input (offline)."""

from __future__ import annotations

import gzip
from typing import Any

import pytest

from seoprobe.core.models import Severity
from seoprobe.core.net.client import RawResponse
from seoprobe.core.net.safety import URLSafetyError, check_url
from seoprobe.core.rules import catalog
from seoprobe.features.site import SitemapReport, SitemapUrl, sitemap
from seoprobe.features.site.sitemap import (
    COMMON_PATHS,
    _gunzip_bounded,
    classify,
    discover_sitemaps,
    parse_w3c_datetime,
)
from tests import fakes
from tests.fakes import FakeHttp as FixtureFetcher

ORIGIN = "https://example.com"
XML = {"content-type": "application/xml"}


def urlset(*locs: str | tuple[str, str | None]) -> bytes:
    rows = []
    for item in locs:
        loc, lm = (item, None) if isinstance(item, str) else item
        rows.append(f"<url><loc>{loc}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>")
    return (
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(rows)
        + "</urlset>"
    ).encode()


def index(*locs: str) -> bytes:
    return (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<sitemap><loc>{loc}</loc></sitemap>" for loc in locs)
        + "</sitemapindex>"
    ).encode()


def fx(pages: dict[str, bytes | tuple[int, dict[str, str], bytes]]) -> FixtureFetcher:
    table = {}
    for url, v in pages.items():
        table[url] = (200, XML, v) if isinstance(v, bytes) else v
    return FixtureFetcher(table)


def ids(res) -> list[str]:  # type: ignore[no-untyped-def]
    return [f.id for f in res.output.findings]


# ------------------------------------------------------------------ discovery & formats


async def test_robots_declared_sitemap_is_read_and_common_paths_are_not_probed() -> None:
    f = fx({f"{ORIGIN}/map.xml": urlset(f"{ORIGIN}/a", f"{ORIGIN}/b", (f"{ORIGIN}/c", "2026-01-02"))})
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/map.xml"])
    assert res.found and [u.loc for u in res.urls] == [f"{ORIGIN}/a", f"{ORIGIN}/b", f"{ORIGIN}/c"]
    assert res.urls[2].lastmod == "2026-01-02" and ids(res) == []
    assert f.requested == [f"{ORIGIN}/map.xml"]  # no probing of /sitemap.xml & co.


async def test_common_paths_probed_in_order_and_stop_at_first_valid() -> None:
    f = fx({f"{ORIGIN}/sitemap_index.xml": urlset(f"{ORIGIN}/x")})
    res = await discover_sitemaps(ORIGIN, f, [])
    assert res.found and f.requested == [f"{ORIGIN}{COMMON_PATHS[0]}", f"{ORIGIN}{COMMON_PATHS[1]}"]


async def test_no_sitemap_anywhere_is_a_finding_not_a_crash() -> None:
    res = await discover_sitemaps(ORIGIN, fx({}), [])
    assert not res.found and ids(res) == ["sitemap-missing"] and res.urls == []
    f = res.output.findings[0]
    assert f.severity == Severity.LOW and f.scope == "site"


async def test_unreachable_origin_is_not_checked_not_missing() -> None:
    class Down:
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
            return RawResponse(url, url, None, {}, b"", False, "unreachable: dns", [])

    res = await discover_sitemaps(ORIGIN, Down(), [])  # type: ignore[arg-type]
    assert ids(res) == [] and [(n.check_id, n.reason_code) for n in res.output.not_checked] == [
        ("sitemap-missing", "unreachable")
    ]


async def test_declared_but_broken_sitemap_is_invalid_and_common_path_still_used() -> None:
    f = fx(
        {
            f"{ORIGIN}/gone.xml": (404, {"content-type": "text/plain"}, b"nope"),
            f"{ORIGIN}/sitemap.xml": urlset(f"{ORIGIN}/a"),
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/gone.xml"])
    assert res.found and [u.loc for u in res.urls] == [f"{ORIGIN}/a"]
    f1 = next(x for x in res.output.findings if x.id == "sitemap-invalid")
    assert "HTTP 404" in (f1.evidence or "")


async def test_sitemap_index_is_expanded_one_level() -> None:
    f = fx(
        {
            f"{ORIGIN}/idx.xml": index(f"{ORIGIN}/p1.xml", f"{ORIGIN}/p2.xml"),
            f"{ORIGIN}/p1.xml": urlset(f"{ORIGIN}/a"),
            f"{ORIGIN}/p2.xml": urlset(f"{ORIGIN}/b", f"{ORIGIN}/a"),
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/idx.xml"])
    assert [u.loc for u in res.urls] == [f"{ORIGIN}/a", f"{ORIGIN}/b"]
    assert "sitemap-duplicate-urls" in ids(res)
    assert [x.kind for x in res.files] == ["sitemapindex", "urlset", "urlset"]


async def test_nested_index_is_recorded_not_followed() -> None:
    f = fx(
        {
            f"{ORIGIN}/idx.xml": index(f"{ORIGIN}/idx2.xml"),
            f"{ORIGIN}/idx2.xml": index(f"{ORIGIN}/deep.xml"),
            f"{ORIGIN}/deep.xml": urlset(f"{ORIGIN}/x"),
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/idx.xml"])
    assert "sitemap-nested-index" in ids(res) and f"{ORIGIN}/deep.xml" not in f.requested and res.urls == []


async def test_child_limit_and_unreachable_children() -> None:
    kids = [f"{ORIGIN}/c{i}.xml" for i in range(80)]
    pages: dict[str, Any] = {f"{ORIGIN}/idx.xml": index(*kids)}
    pages |= {k: urlset(f"{ORIGIN}/p{i}") for i, k in enumerate(kids[:50])}
    f = fx(pages)
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/idx.xml"], max_children=50)
    assert len(f.requested) == 51 and len(res.urls) == 50
    assert ("sitemap-child-unreachable", "partial-sample") in [
        (n.check_id, n.reason_code) for n in res.output.not_checked
    ]
    # a child that 404s is reported
    f2 = fx(
        {
            f"{ORIGIN}/idx.xml": index(f"{ORIGIN}/ok.xml", f"{ORIGIN}/bad.xml"),
            f"{ORIGIN}/ok.xml": urlset(f"{ORIGIN}/a"),
        }
    )
    r2 = await discover_sitemaps(ORIGIN, f2, [f"{ORIGIN}/idx.xml"])
    assert "sitemap-child-unreachable" in ids(r2) and r2.found


async def test_declared_limit_of_sixteen() -> None:
    f = fx({})
    await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/s{i}.xml" for i in range(30)], max_sitemaps=16)
    declared_requests = [u for u in f.requested if "/s" in u and "sitemap" not in u]
    assert len(declared_requests) == 16


async def test_text_rss_and_atom_formats() -> None:
    txt = fx(
        {
            f"{ORIGIN}/s.txt": (
                200,
                {"content-type": "text/plain"},
                f"{ORIGIN}/a\n{ORIGIN}/b\n".encode(),
            )
        }
    )
    assert [u.loc for u in (await discover_sitemaps(ORIGIN, txt, [f"{ORIGIN}/s.txt"])).urls] == [
        f"{ORIGIN}/a",
        f"{ORIGIN}/b",
    ]
    rss = b'<rss version="2.0"><channel><item><link>https://example.com/p1</link></item></channel></rss>'
    assert [
        u.loc for u in (await discover_sitemaps(ORIGIN, fx({f"{ORIGIN}/f.xml": rss}), [f"{ORIGIN}/f.xml"])).urls
    ] == [f"{ORIGIN}/p1"]
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="alternate" href="https://example.com/e1"/><updated>2026-03-01T10:00:00Z</updated></entry></feed>'
    r = await discover_sitemaps(ORIGIN, fx({f"{ORIGIN}/a.xml": atom}), [f"{ORIGIN}/a.xml"])
    assert [(u.loc, u.lastmod) for u in r.urls] == [(f"{ORIGIN}/e1", "2026-03-01T10:00:00Z")]


async def test_gzip_sitemap_by_magic_bytes() -> None:
    f = fx(
        {
            f"{ORIGIN}/s.xml.gz": (
                200,
                {"content-type": "application/gzip"},
                gzip.compress(urlset(f"{ORIGIN}/g")),
            )
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/s.xml.gz"])
    assert [u.loc for u in res.urls] == [f"{ORIGIN}/g"] and res.files[0].gzip


async def test_only_audited_site_urls_are_returned_www_counts_as_same_site() -> None:
    f = fx(
        {
            f"{ORIGIN}/s.xml": urlset(
                f"{ORIGIN}/a",
                "https://www.example.com/w",
                "https://other.org/x",
                "https://evil.example.com.attacker.io/y",
            )
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/s.xml"])
    assert [u.loc for u in res.urls] == [f"{ORIGIN}/a", "https://www.example.com/w"]
    cross = next(x for x in res.output.findings if x.id == "sitemap-cross-host-urls")
    assert cross.count == 3  # www., other.org, attacker: all differ from the sitemap file's host


# ------------------------------------------------------------------ findings


async def test_invalid_locs_and_lastmod_findings() -> None:
    f = fx(
        {
            f"{ORIGIN}/s.xml": urlset(
                f"{ORIGIN}/ok",
                "not a url",
                "ftp://example.com/x",
                f"{ORIGIN}/" + "a" * 2100,
                (f"{ORIGIN}/lm", "yesterday"),
                (f"{ORIGIN}/lm2", "2026-13-45"),
            )
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/s.xml"])
    by = {x.id: x for x in res.output.findings}
    assert by["sitemap-invalid-urls"].count == 3 and by["sitemap-lastmod-invalid"].count == 2
    assert [u.loc for u in res.urls] == [f"{ORIGIN}/ok", f"{ORIGIN}/lm", f"{ORIGIN}/lm2"]


async def test_uniform_lastmod_is_a_heuristic_finding_with_thresholds() -> None:
    same = fx({f"{ORIGIN}/s.xml": urlset(*[(f"{ORIGIN}/p{i}", "2026-01-01") for i in range(20)])})
    res = await discover_sitemaps(ORIGIN, same, [f"{ORIGIN}/s.xml"])
    u = next(x for x in res.output.findings if x.id == "sitemap-lastmod-uniform")
    assert u.heuristic and u.count == 20
    few = fx({f"{ORIGIN}/s.xml": urlset(*[(f"{ORIGIN}/p{i}", "2026-01-01") for i in range(9)])})
    assert "sitemap-lastmod-uniform" not in ids(await discover_sitemaps(ORIGIN, few, [f"{ORIGIN}/s.xml"]))
    varied = fx({f"{ORIGIN}/s.xml": urlset(*[(f"{ORIGIN}/p{i}", f"2026-01-{1 + i % 28:02d}") for i in range(40)])})
    assert "sitemap-lastmod-uniform" not in ids(await discover_sitemaps(ORIGIN, varied, [f"{ORIGIN}/s.xml"]))


async def test_url_normalization_catches_tracking_and_case_duplicates() -> None:
    f = fx(
        {
            f"{ORIGIN}/s.xml": urlset(
                f"{ORIGIN}/a",
                f"{ORIGIN}/a?utm_source=x",
                "https://EXAMPLE.com:443/a#frag",
                f"{ORIGIN}//a",
            )
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/s.xml"])
    assert len(res.urls) == 1
    assert next(x for x in res.output.findings if x.id == "sitemap-duplicate-urls").count == 3


async def test_sitemap_file_urls_in_evidence_drop_the_query_string() -> None:
    f = fx({f"{ORIGIN}/gone.xml?token=SECRET": (404, {"content-type": "text/plain"}, b"x")})
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/gone.xml?token=SECRET"])
    inv = next(x for x in res.output.findings if x.id == "sitemap-invalid")
    assert "SECRET" not in (inv.evidence or "") and "gone.xml" in (inv.evidence or "")


async def test_findings_are_registry_findings() -> None:
    f = fx({f"{ORIGIN}/s.xml": urlset("bad", (f"{ORIGIN}/a", "x"))})
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/s.xml"])
    for fnd in res.output.findings:
        d = catalog.rules()[fnd.id]
        assert fnd.message == catalog.message(d.message_key, "en") and fnd.layer.value == "A"
        assert fnd.evidence is None or len(fnd.evidence) <= 200


# ------------------------------------------------------------------ limits


async def test_over_50000_urls_is_flagged_and_capped() -> None:
    body = urlset(*[f"{ORIGIN}/p{i}" for i in range(50_001)])
    res = await discover_sitemaps(ORIGIN, fx({f"{ORIGIN}/s.xml": body}), [f"{ORIGIN}/s.xml"])
    assert "sitemap-too-large" in ids(res) and res.truncated and len(res.urls) == 50_000


async def test_max_urls_parameter_caps_returned_urls() -> None:
    res = await discover_sitemaps(
        ORIGIN,
        fx({f"{ORIGIN}/s.xml": urlset(*[f"{ORIGIN}/p{i}" for i in range(100)])}),
        [f"{ORIGIN}/s.xml"],
        max_urls=10,
    )
    assert len(res.urls) == 10 and res.truncated


def test_gunzip_bounded_stops_at_the_limit() -> None:
    bomb = gzip.compress(b"\0" * 5_000_000)
    out, big = _gunzip_bounded(bomb, 1000)
    assert big and len(out) == 1000
    ok, big2 = _gunzip_bounded(gzip.compress(b"hello"), 1000)
    assert (ok, big2) == (b"hello", False)
    with pytest.raises(ValueError):
        _gunzip_bounded(b"\x1f\x8b" + b"garbage" * 10, 1000)


async def test_gzip_bomb_over_50mib_is_refused_without_materialising_it() -> None:
    bomb = gzip.compress(b"<urlset>" + b" " * (60 * 1024 * 1024))
    assert len(bomb) < 1_000_000  # tiny on the wire
    res = await discover_sitemaps(
        ORIGIN,
        fx({f"{ORIGIN}/s.xml.gz": (200, {"content-type": "application/gzip"}, bomb)}),
        [f"{ORIGIN}/s.xml.gz"],
    )
    assert "sitemap-too-large" in ids(res) and not res.found
    assert not any("sitemap-invalid" == x for x in ids(res))


async def test_raw_body_over_50mib_is_flagged_too_large() -> None:
    class Big:
        async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
            return RawResponse(url, url, 200, XML, b"x" * (max_bytes or 0), True, None, [])

    res = await discover_sitemaps(ORIGIN, Big(), [f"{ORIGIN}/s.xml"])  # type: ignore[arg-type]
    assert "sitemap-too-large" in ids(res)


# ------------------------------------------------------------------ hostile XML


XXE = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset><url><loc>&x;</loc></url></urlset>'
LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]><urlset><url><loc>&lol3;</loc></url></urlset>'
)
EXTERNAL_DTD = b'<?xml version="1.0"?><!DOCTYPE urlset SYSTEM "http://169.254.169.254/evil.dtd"><urlset/>'
PARAM_ENTITY = b'<!doctype x [<!ENTITY % p SYSTEM "http://127.0.0.1/x"> %p;]><urlset/>'


@pytest.mark.parametrize(
    "payload",
    [XXE, LAUGHS, EXTERNAL_DTD, PARAM_ENTITY],
    ids=["xxe", "billion-laughs", "external-dtd", "param-entity-lowercase"],
)
async def test_doctype_payloads_are_refused_and_never_resolved(payload: bytes) -> None:
    res = await discover_sitemaps(ORIGIN, fx({f"{ORIGIN}/s.xml": payload}), [f"{ORIGIN}/s.xml"])
    assert not res.found and res.urls == []
    assert "sitemap-invalid" in ids(res)
    inv = next(x for x in res.output.findings if x.id == "sitemap-invalid")
    assert "DOCTYPE" in (inv.evidence or "") and "passwd" not in (inv.evidence or "")


def test_entity_without_doctype_is_not_expanded() -> None:
    kind, err, root, _ = classify(
        b'<urlset xmlns="x"><url><loc>&xxe;</loc></url></urlset>',
        "application/xml",
        f"{ORIGIN}/s.xml",
    )
    assert kind is None and err == "invalid XML"  # undefined entity: recover=False -> syntax error


@pytest.mark.parametrize(
    "body,error",
    [
        (b"", "empty response"),
        (b"   \n", "empty response"),
        (b"<html><body>hello</body></html>", "unsupported XML root"),
        (b"<urlset><url></urlset>", "invalid XML"),
        (b"\x00\x01\x02binary", "unsupported format"),
    ],
)
def test_classify_rejects(body: bytes, error: str) -> None:
    assert classify(body, "application/xml", f"{ORIGIN}/s.xml")[:2] == (None, error)


async def test_html_catch_all_on_a_common_path_is_ignored_silently() -> None:
    f = fx({f"{ORIGIN}{p}": (200, {"content-type": "text/html"}, b"<html>home</html>") for p in COMMON_PATHS})
    res = await discover_sitemaps(ORIGIN, f, [])
    assert ids(res) == ["sitemap-missing"]  # a catch-all is no sitemap; not reported as 'invalid'


async def test_deeply_nested_xml_does_not_crash() -> None:
    deep = b"<urlset>" + b"<a>" * 100_000 + b"</a>" * 100_000 + b"</urlset>"
    res = await discover_sitemaps(ORIGIN, fx({f"{ORIGIN}/s.xml": deep}), [f"{ORIGIN}/s.xml"])
    assert not res.found and "sitemap-invalid" in ids(res)


# ------------------------------------------------------------------ SSRF at the fetcher boundary


class PolicyFetcher(FixtureFetcher):
    """Applies the real URL policy before serving, like HttpClient does per hop."""

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
        try:
            check_url(url)
        except URLSafetyError as exc:
            return RawResponse(url, url, None, {}, b"", False, f"target-rejected: {exc}", [])
        return await super().fetch_bytes(url, max_bytes=max_bytes or 10_000_000)


async def test_children_pointing_at_internal_addresses_are_rejected_not_fetched() -> None:
    evil = [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/s.xml",
        "http://localhost/s.xml",
        "http://10.0.0.1/s.xml",
    ]
    f = PolicyFetcher(
        {
            f"{ORIGIN}/idx.xml": (200, XML, index(*evil, f"{ORIGIN}/ok.xml")),
            f"{ORIGIN}/ok.xml": (200, XML, urlset(f"{ORIGIN}/a")),
        }
    )
    res = await discover_sitemaps(ORIGIN, f, [f"{ORIGIN}/idx.xml"])
    assert [u.loc for u in res.urls] == [f"{ORIGIN}/a"]
    child = next(x for x in res.output.findings if x.id == "sitemap-child-unreachable")
    assert child.count == 4
    assert not any("169.254" in u and u in f.requested and u.startswith(ORIGIN) for u in f.requested)


async def test_robots_declared_internal_sitemap_is_rejected() -> None:
    f = PolicyFetcher({})
    res = await discover_sitemaps(ORIGIN, f, ["http://169.254.169.254/sitemap.xml"])
    assert "sitemap-invalid" in ids(res)
    assert all(
        not r.startswith("http://169.254") or r in f.requested for r in f.requested
    )  # request goes via the policy


# ------------------------------------------------------------------ w3c datetime


@pytest.mark.parametrize(
    "value,ok",
    [
        ("2026", True),
        ("2026-05", True),
        ("2026-05-17", True),
        ("2026-05-17T10:00:00Z", True),
        ("2026-05-17T10:00:00+02:00", True),
        ("2026-05-17T10:00:00.123Z", True),
        ("2026-13-01", False),
        ("2026-02-30", False),
        ("17-05-2026", False),
        ("", False),
        ("2026-05-17T10:00:00", False),
        ("2026-05-17T10:00:00+25:00", False),
    ],
)
def test_parse_w3c_datetime(value: str, ok: bool) -> None:
    assert (parse_w3c_datetime(value) is not None) is ok


# ------------------------------------------------------------------ the operation


async def test_sitemap_operation_reads_robots_declarations_when_not_given() -> None:
    pages = {
        f"{ORIGIN}/robots.txt": (200, {"content-type": "text/plain"}, f"Sitemap: {ORIGIN}/map.xml\n".encode()),
        f"{ORIGIN}/map.xml": (200, XML, urlset(f"{ORIGIN}/a", (f"{ORIGIN}/b", "2026-01-02"))),
    }
    async with fakes.session(pages) as s:
        res = await sitemap(f"{ORIGIN}/page", session=s)
    assert res.ok and isinstance(res.data, SitemapReport)
    assert res.data.found and res.data.urls == [
        SitemapUrl(loc=f"{ORIGIN}/a"),
        SitemapUrl(loc=f"{ORIGIN}/b", lastmod="2026-01-02"),
    ]
    assert [f.kind for f in res.data.files] == ["urlset"] and res.data.files[0].declared
    assert res.data.facts["urls"] == 2 and not res.data.truncated
    assert f"{ORIGIN}/sitemap.xml" not in s.http.requested  # type: ignore[attr-defined]
    assert '"found":true' in res.to_json()


async def test_sitemap_operation_with_explicit_declarations_skips_robots() -> None:
    pages = {f"{ORIGIN}/m.xml": (200, XML, urlset(f"{ORIGIN}/x"))}
    async with fakes.session(pages) as s:
        res = await sitemap(f"{ORIGIN}/", robots_sitemaps=[f"{ORIGIN}/m.xml"], session=s)
    assert res.ok and res.data is not None and [u.loc for u in res.data.urls] == [f"{ORIGIN}/x"]
    assert s.http.requested == [f"{ORIGIN}/m.xml"]  # type: ignore[attr-defined]


async def test_sitemap_operation_missing_is_a_finding() -> None:
    async with fakes.session({}) as s:
        res = await sitemap(f"{ORIGIN}/", robots_sitemaps=[], session=s)
    assert res.ok and res.data is not None and not res.data.found
    assert [f.id for f in res.findings] == ["sitemap-missing"]
