"""RDAP, Wayback, Common Crawl and the shared HTTP helper, all offline (fake session)."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from seoprobe.core.config import Config
from seoprobe.core.net import api as http
from seoprobe.core.net.client import HttpClient
from seoprobe.core.net.safety import SafeResolver

rdap = importlib.import_module("seoprobe.features.offsite.visibility")
lookup_commoncrawl = rdap.lookup_commoncrawl
lookup_rdap = rdap.lookup_rdap
lookup_wayback = rdap.lookup_wayback
registrable_domain = rdap.registrable_domain


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        for i in range(0, len(self._body), n):
            yield self._body[i : i + n]


class _Resp:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status, self.content, self.headers = status, _Content(body), headers or {}


class FakeSession:
    """routes: url-prefix -> (status, body) | Exception | 'hang'. Longest prefix wins."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    @asynccontextmanager
    async def get(self, url: str, *, allow_redirects: bool = False) -> AsyncIterator[_Resp]:
        self.requested.append(url)
        key = max((k for k in self.routes if url.startswith(k)), key=len, default=None)
        if key is None:
            yield _Resp(404, b"")
            return
        r = self.routes[key]
        if r == "hang":
            await asyncio.sleep(60)
        if isinstance(r, Exception):
            raise r
        status, body, *rest = r
        yield _Resp(
            status,
            body if isinstance(body, bytes) else json.dumps(body).encode(),
            rest[0] if rest else None,
        )


@pytest.fixture(autouse=True)
def _clear_bootstrap() -> None:
    rdap._bootstrap_cache.clear()


BOOT = {
    "services": [
        [["com", "net"], ["https://rdap.verisign.com/com/v1/"]],
        [["org"], ["https://rdap.pir.example/"]],
    ]
}


def _rdap_doc(created: str = "2026-10-26T14:04:11Z") -> dict[str, Any]:
    return {
        "events": [
            {"eventAction": "last changed", "eventDate": "2026-11-01T00:00:00Z"},
            {"eventAction": "registration", "eventDate": created},
        ],
        "entities": [
            {
                "roles": ["registrar"],
                "vcardArray": [
                    "vcard",
                    [
                        ["version", {}, "text", "4.0"],
                        ["fn", {}, "text", "HOSTINGER operations, UAB"],
                    ],
                ],
            }
        ],
    }


# ------------------------------------------------------------------ helpers


def test_registrable_domain() -> None:
    assert registrable_domain("www.girisimcilikvakfi.org") == "girisimcilikvakfi.org"
    assert registrable_domain("https://Example.COM/path?x=1") == "example.com"
    assert registrable_domain("foo.bar.co.uk") == "bar.co.uk"
    assert registrable_domain("a.b.com.tr") == "b.com.tr"
    assert registrable_domain("localhost") == "localhost"


async def test_get_text_rejects_http_and_private_literals_without_a_request() -> None:
    s = FakeSession({})
    assert (await http.get_text(s, "http://example.com/x"))[2] == "target-rejected"
    assert (await http.get_text(s, "https://127.0.0.1/x"))[2] == "target-rejected"
    assert (await http.get_text(s, "https://169.254.169.254/latest"))[2] == "target-rejected"
    assert s.requested == []


async def test_redirect_hops_are_checked() -> None:
    class Redir(FakeSession):
        @asynccontextmanager
        async def get(self, url: str, *, allow_redirects: bool = False) -> AsyncIterator[_Resp]:
            self.requested.append(url)
            yield _Resp(302, b"", {"Location": "https://127.0.0.1/secret"})

    s = Redir({})
    assert (await http.get_text(s, "https://example.com/a"))[2] == "target-rejected"
    assert s.requested == ["https://example.com/a"]


async def test_body_is_read_fully_across_chunks_and_capped() -> None:
    big = b"x" * 200_000
    status, text, err = await http.get_text(FakeSession({"https://e.com/": (200, big)}), "https://e.com/")
    assert (status, err, len(text or "")) == (200, None, 200_000)
    huge = b"x" * (http.MAX_BODY + 10)
    assert (await http.get_text(FakeSession({"https://e.com/": (200, huge)}), "https://e.com/"))[2] == "too-large"


async def test_timeout_is_an_error_code_not_an_exception() -> None:
    assert (await http.get_text(FakeSession({"https://e.com/": "hang"}), "https://e.com/", timeout_s=0.05))[
        2
    ] == "timeout"


async def test_session_uses_safe_resolver_and_blocks_private_resolution() -> None:
    async def private_lookup(host: str, port: int) -> list[str]:
        return ["10.0.0.7"]

    client = HttpClient(Config())
    default = client.api_session()
    assert isinstance(default.connector._resolver, SafeResolver)  # type: ignore[union-attr]
    await client.close()
    client = HttpClient(Config(), resolver=SafeResolver(lookup=private_lookup))
    try:
        status, text, err = await http.get_text(client.api_session(), "https://rebinder.example/x", timeout_s=5)
    finally:
        await client.close()
    assert (status, text, err) == (None, None, "target-rejected")


# ------------------------------------------------------------------ RDAP


async def test_rdap_normal() -> None:
    s = FakeSession({http.__name__: (200, b"")})
    s.routes = {
        "https://data.iana.org/rdap/dns.json": (200, BOOT),
        "https://rdap.verisign.com/com/v1/domain/getlastcheck.com": (200, _rdap_doc()),
    }
    r = await lookup_rdap("www.getlastcheck.com", s)
    assert r["error"] is None and r["created"] == "2026-10-26" and r["registrar"] == "HOSTINGER operations, UAB"
    assert r["registry_host"] == "rdap.verisign.com" and isinstance(r["age_days"], int) and r["age_days"] >= 0


async def test_rdap_age_is_computed_from_the_registration_event() -> None:
    created = (datetime.now(UTC) - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = FakeSession(
        {
            "https://data.iana.org/rdap/dns.json": (200, BOOT),
            "https://rdap.verisign.com/": (200, _rdap_doc(created)),
        }
    )
    assert (await lookup_rdap("e.com", s))["age_days"] in (100, 99)


async def test_rdap_unsupported_tld_and_bootstrap_cached() -> None:
    s = FakeSession({"https://data.iana.org/rdap/dns.json": (200, BOOT)})
    assert (await lookup_rdap("kurum.com.tr", s))["error"] == "rdap-unsupported-tld"
    await lookup_rdap("other.com.tr", s)
    assert s.requested.count("https://data.iana.org/rdap/dns.json") == 1  # cached for the process


@pytest.mark.parametrize(
    "routes,expected",
    [
        ({"https://data.iana.org/rdap/dns.json": (503, b"")}, "bootstrap-http-503"),
        ({"https://data.iana.org/rdap/dns.json": (200, b"not json")}, "bootstrap-bad-json"),
        ({"https://data.iana.org/rdap/dns.json": TimeoutError()}, "bootstrap-timeout"),
        (
            {
                "https://data.iana.org/rdap/dns.json": (200, BOOT),
                "https://rdap.verisign.com/": (404, b""),
            },
            "http-404",
        ),
        (
            {
                "https://data.iana.org/rdap/dns.json": (200, BOOT),
                "https://rdap.verisign.com/": (200, b"[]"),
            },
            "bad-json",
        ),
        (
            {
                "https://data.iana.org/rdap/dns.json": (200, BOOT),
                "https://rdap.verisign.com/": (200, {"events": []}),
            },
            "no-registration-event",
        ),
    ],
)
async def test_rdap_errors(routes: dict[str, Any], expected: str) -> None:
    r = await lookup_rdap("e.com", FakeSession(routes))
    assert r["error"] == expected and r["created"] is None


async def test_rdap_rejects_a_bootstrap_url_that_resolves_to_a_private_host() -> None:
    boot = {"services": [[["com"], ["https://127.0.0.1/rdap/"]]]}
    r = await lookup_rdap("e.com", FakeSession({"https://data.iana.org/rdap/dns.json": (200, boot)}))
    assert r["error"] == "target-rejected"


# ------------------------------------------------------------------ Wayback

CDX = "https://web.archive.org/cdx/search/cdx"


HOST_Q = f"{CDX}?url=a.org&output=json&fl=timestamp"
SAMPLE_Q = f"{CDX}?url=a.org&matchType=domain&output=json&fl=timestamp,original"
SAMPLE = [["timestamp", "original"], ["20140420101010", "http://a.org/"], ["20150101000000", "http://a.org/x"]]


async def test_wayback_dates_come_from_the_host_and_the_sample_from_the_domain() -> None:
    s = FakeSession(
        {
            f"{HOST_Q}&limit=1": (200, [["timestamp"], ["20130101000000"]]),
            f"{HOST_Q}&limit=-1": (200, [["timestamp"], ["20261101000000"]]),
            SAMPLE_Q: (200, SAMPLE),
        }
    )
    r = await lookup_wayback("https://a.org/page", s)
    assert r == {
        "first_capture": "2013-01-01",
        "last_capture": "2026-11-01",
        "captures": 2,
        "urls_archived_sample": 2,
        "error": None,
    }
    assert len(s.requested) == 3 and not any("sort=reverse" in u for u in s.requested)  # the slow query is gone


async def test_wayback_empty_is_zero_captures_not_an_error() -> None:
    for body in (b"", b"[]", b'[["timestamp","original"]]'):
        r = await lookup_wayback("new.example", FakeSession({CDX: (200, body)}))
        assert r["error"] is None and r["urls_archived_sample"] == 0 and r["first_capture"] is None


async def test_wayback_first_capture_falls_back_to_the_sample() -> None:
    s = FakeSession({f"{HOST_Q}&limit=1": (200, b""), f"{HOST_Q}&limit=-1": (200, b""), SAMPLE_Q: (200, SAMPLE)})
    r = await lookup_wayback("a.org", s)
    assert r["first_capture"] == "2014-04-20" and r["urls_archived_sample"] == 2 and r["error"] is None


async def test_wayback_sample_is_capped_at_100() -> None:
    rows = [["timestamp", "original"]] + [[f"2015010100000{i % 10}", f"http://a.org/{i}"] for i in range(300)]
    r = await lookup_wayback(
        "a.org", FakeSession({SAMPLE_Q: (200, rows), CDX: (200, [["timestamp"], ["20260101000000"]])})
    )
    assert r["urls_archived_sample"] == 100


async def test_a_slow_sample_keeps_the_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rdap, "WAYBACK_SAMPLE_TIMEOUT_S", 0.1)
    s = FakeSession(
        {
            f"{HOST_Q}&limit=1": (200, [["timestamp"], ["20130101000000"]]),
            f"{HOST_Q}&limit=-1": (200, [["timestamp"], ["20261101000000"]]),
            SAMPLE_Q: "hang",
        }
    )
    r = await lookup_wayback("a.org", s)
    assert (r["first_capture"], r["last_capture"]) == ("2013-01-01", "2026-11-01")
    assert r["urls_archived_sample"] is None and r["error"] == "sample-timeout"


async def test_wayback_last_capture_failure_keeps_the_first_part() -> None:
    s = FakeSession(
        {
            f"{HOST_Q}&limit=1": (200, [["timestamp"], ["20140420101010"]]),
            f"{HOST_Q}&limit=-1": (503, b""),
            SAMPLE_Q: (200, SAMPLE),
        }
    )
    r = await lookup_wayback("a.org", s)
    assert r["first_capture"] == "2014-04-20" and r["last_capture"] is None and r["error"] == "last-capture-http-503"


@pytest.mark.parametrize(
    "route,expected",
    [((504, b""), "http-504"), ((200, b"{oops"), "bad-json"), (TimeoutError(), "timeout")],
)
async def test_wayback_errors(route: Any, expected: str) -> None:
    r = await lookup_wayback("a.org", FakeSession({CDX: route}))
    assert r["error"] == expected and r["urls_archived_sample"] == 0


# ------------------------------------------------------------------ Common Crawl

COLL = [
    {"id": "CC-MAIN-2026-43", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-43-index"},
    {"id": "CC-MAIN-2026-40", "cdx-api": "https://index.commoncrawl.org/CC-MAIN-2026-40-index"},
]


async def test_commoncrawl_present_uses_the_newest_collection() -> None:
    ndjson = b'{"url": "https://a.org/"}\n{"url": "https://a.org/x"}\nnot-json\n'
    s = FakeSession(
        {
            "https://index.commoncrawl.org/collinfo.json": (200, COLL),
            "https://index.commoncrawl.org/CC-MAIN-2026-43-index?url=a.org/*": (200, ndjson),
        }
    )
    r = await lookup_commoncrawl("a.org", s)
    assert r == {
        "collection": "CC-MAIN-2026-43",
        "present": True,
        "pages_seen_sample": 2,
        "error": None,
    }


@pytest.mark.parametrize("route", [(404, b'{"message": "No Captures found for: a.org/*"}'), (200, b"")])
async def test_commoncrawl_absent_is_present_false(route: Any) -> None:
    s = FakeSession(
        {
            "https://index.commoncrawl.org/collinfo.json": (200, COLL),
            "https://index.commoncrawl.org/CC-MAIN-2026-43-index": route,
        }
    )
    r = await lookup_commoncrawl("a.org", s)
    assert r["present"] is False and r["error"] is None


@pytest.mark.parametrize(
    "routes,expected",
    [
        ({"https://index.commoncrawl.org/collinfo.json": (500, b"")}, "collinfo-http-500"),
        ({"https://index.commoncrawl.org/collinfo.json": (200, b"{")}, "collinfo-bad-json"),
        ({"https://index.commoncrawl.org/collinfo.json": (200, [])}, "bad-json"),
        (
            {
                "https://index.commoncrawl.org/collinfo.json": (200, COLL),
                "https://index.commoncrawl.org/CC-MAIN-2026-43-index": (502, b""),
            },
            "http-502",
        ),
        (
            {
                "https://index.commoncrawl.org/collinfo.json": (200, COLL),
                "https://index.commoncrawl.org/CC-MAIN-2026-43-index": TimeoutError(),
            },
            "timeout",
        ),
    ],
)
async def test_commoncrawl_errors(routes: dict[str, Any], expected: str) -> None:
    r = await lookup_commoncrawl("a.org", FakeSession(routes))
    assert r["error"] == expected and r["present"] is None
