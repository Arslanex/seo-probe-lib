"""OpenPageRank integration and the authority operation. No real network."""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from seoprobe.core.config import Config
from seoprobe.core.page import Page
from seoprobe.features.offsite import FEATURE, AuthoritySignal, authority
from tests import fakes

opr = importlib.import_module("seoprobe.features.offsite.authority")
fetch_authority = opr.fetch_authority
parse_response = opr.parse_response

KEY = "opr-secret-key-123"
OK = {
    "status_code": 200,
    "response": [
        {
            "status_code": 200,
            "page_rank_integer": 5,
            "page_rank_decimal": 5.43,
            "rank": "123456",
            "domain": "example.com",
        }
    ],
}
ENTRY = Page(url="https://example.com/", final_url="https://example.com/", status=200, html="<html></html>")


class _Body:
    def __init__(self, data: bytes) -> None:
        self._d = data

    async def iter_chunked(self, n: int) -> AsyncIterator[bytes]:
        for i in range(0, len(self._d), n):
            yield self._d[i : i + n]


class _Resp:
    def __init__(self, status: int, data: bytes) -> None:
        self.status = status
        self.content = _Body(data)

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


class FakeSession:
    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.status, self.body = status, body
        self.calls: list[tuple[str, dict[str, str], bool]] = []

    def get(self, url: str, *, headers: dict[str, str], allow_redirects: bool) -> _Resp:
        self.calls.append((url, headers, allow_redirects))
        return _Resp(self.status, self.body)

    async def close(self) -> None:
        return None


def _session(key: str | None, api: Any = None) -> Any:
    s = fakes.session(config=Config(openpagerank_key=key))
    if api is not None:
        s.http.api_session = lambda: api
    return s


def test_registered() -> None:
    assert authority in FEATURE.operations and "openpagerank" in FEATURE.access_checks


def test_parse_response() -> None:
    sig = parse_response(OK, "example.com")
    assert (sig.page_rank_decimal, sig.page_rank_integer, sig.rank) == (5.43, 5, 123456)
    assert parse_response({"response": [{"status_code": 404}]}, "x.com").error == "no-data"
    assert parse_response("nope", "x.com").error == "bad-json"
    assert parse_response({"response": []}, "x.com").error == "bad-json"


async def test_key_goes_in_the_header_only_and_domain_is_registrable() -> None:
    sess = FakeSession(200, json.dumps(OK).encode())
    sig = await fetch_authority("www.shop.example.com.tr", session=sess, key=KEY)
    (url, headers, redirects) = sess.calls[0]
    assert headers["API-OPR"] == KEY and KEY not in url and redirects is False
    assert "domains%5B%5D=example.com.tr" in url
    assert KEY not in sig.model_dump_json() and sig.page_rank_integer == 5


async def test_error_codes_never_carry_the_key() -> None:
    for status, code in (
        (401, "unauthorized"),
        (403, "unauthorized"),
        (429, "rate-limited"),
        (500, "http-500"),
    ):
        sig = await fetch_authority("example.com", session=FakeSession(status), key=KEY)
        assert sig.error == code and KEY not in sig.model_dump_json()
    big = await fetch_authority("example.com", session=FakeSession(200, b"x" * 200_000), key=KEY)
    assert big.error == "too-large"
    bad = await fetch_authority("example.com", session=FakeSession(200, b"{not json"), key=KEY)
    assert bad.error == "bad-json"


async def test_no_key_means_no_request() -> None:
    sess = FakeSession()
    sig = await fetch_authority("example.com", session=sess, key="")
    assert sess.calls == [] and sig.error == "no-api-key"


async def test_module_without_key_is_not_checked() -> None:
    async with _session(None) as s:
        res = await authority(ENTRY, session=s)
    assert res.ok and res.findings == []
    assert res.data is not None and res.data.page_rank_decimal is None and res.data.rank is None
    assert [(n.check_id, n.reason_code) for n in res.not_checked] == [("authority", "no-api-key")]


async def test_module_without_key_does_not_fetch_the_target() -> None:
    async with _session(None) as s:
        res = await authority("https://example.com/", session=s)
        assert s.http.requested == []
    assert res.not_checked[0].reason_code == "no-api-key"
    assert res.data is not None and res.data.domain == "example.com"  # known without any request


async def test_module_with_key_stores_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(host: str, *, session: Any, key: str, timeout_s: float) -> AuthoritySignal:
        assert host == "example.com" and key == KEY
        return AuthoritySignal(domain=host, page_rank_decimal=4.2, page_rank_integer=4, rank=99)

    monkeypatch.setattr(opr, "fetch_authority", fake)
    async with _session(KEY, FakeSession()) as s:
        res = await authority(ENTRY, session=s)
    assert res.findings == [] and res.not_checked == [] and res.status == "ok"
    assert isinstance(res.data, AuthoritySignal) and res.data.rank == 99


async def test_module_error_becomes_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(host: str, **kw: Any) -> AuthoritySignal:
        return AuthoritySignal(domain=host, error="rate-limited")

    monkeypatch.setattr(opr, "fetch_authority", fake)
    async with _session(KEY, FakeSession()) as s:
        res = await authority(ENTRY, session=s)
    assert res.ok and res.status == "partial"
    assert res.not_checked[0].reason_code == "authority-rate-limited"


async def test_key_never_reaches_the_request_log_or_the_json() -> None:
    api = FakeSession(200, json.dumps(OK).encode())
    async with _session(KEY, api) as s:
        res = await authority(ENTRY, session=s)
    assert res.data is not None and res.data.page_rank_integer == 5
    assert api.calls[0][1]["API-OPR"] == KEY
    assert res.meta.requests and all(KEY not in r.url for r in res.meta.requests)
    assert "openpagerank.keywordseverywhere.com" in res.meta.requests[0].url
    assert KEY not in res.to_json()
    for status in (401, 429):
        async with _session(KEY, FakeSession(status)) as s:
            err = await authority(ENTRY, session=s)
        assert KEY not in err.to_json() and all(KEY not in r.url for r in err.meta.requests)


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, json.dumps(OK).encode(), (True, "ok")),
        (200, b'{"response": [{"status_code": 404}]}', (True, "ok")),
        (401, b"", (False, "rejected.opr")),
        (429, b"", (False, "rate-limited")),
        (500, b"", (False, "other")),
    ],
)
async def test_access_check(status: int, body: bytes, expected: tuple[bool, str]) -> None:
    api = FakeSession(status, body)
    async with _session(KEY, api) as s:
        assert await FEATURE.access_checks["openpagerank"](s) == expected
    assert KEY not in api.calls[0][0]


async def test_access_check_without_key() -> None:
    async with _session(None) as s:
        assert await FEATURE.access_checks["openpagerank"](s) == (False, "missing")


async def test_result_is_json_serializable() -> None:
    async with _session(None) as s:
        res = await authority(ENTRY, session=s)
    assert json.loads(res.to_json())["operation"] == "authority"
