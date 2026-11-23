"""``seoprobe.indexnow``: key file, verification and the (only) write operation ``submit``."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import seoprobe
from seoprobe.core.config import Config
from seoprobe.core.errors import ErrorCode
from seoprobe.core.result import Status
from seoprobe.features.search import indexnow
from seoprobe.features.search.indexnow import ENDPOINT
from tests.features.search.support import FakeApi, api_session

KEY = "a1b2c3d4-e5f6"
HOST = "example.com"
KEY_URL = f"https://{HOST}/{KEY}.txt"


def _text(body: str, status: int = 200) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "text/plain"}, body.encode()


def _ok_api(status: int = 200) -> FakeApi:
    return FakeApi(lambda m, u, kw: (status, None))


# ------------------------------------------------------------------ key_file


async def test_key_file() -> None:
    res = await indexnow.key_file(KEY)
    d = res.unwrap()
    assert (d.key, d.path, d.content, d.content_type) == (KEY, f"/{KEY}.txt", KEY, "text/plain; charset=utf-8")
    assert json.loads(res.to_json())["data"]["path"] == f"/{KEY}.txt"
    assert seoprobe.requirements("indexnow.key_file").network is False


@pytest.mark.parametrize("bad", ["short", "x" * 129, "has space1", "dot.ted.key", "under_score1"])
async def test_key_format(bad: str) -> None:
    res = await indexnow.key_file(bad)
    assert res.error is not None and res.error.code is ErrorCode.INVALID_INPUT
    assert res.error.details == {"service": "indexnow", "reason": "invalid-key"}
    assert indexnow.valid_key("12345678") and indexnow.valid_key("A" * 128)


# ------------------------------------------------------------------ verify


async def test_verify_ok_and_mismatch() -> None:
    async with api_session(_ok_api(), pages={KEY_URL: _text(f"﻿{KEY}\n")}) as s:
        res = await indexnow.verify("Example.COM", KEY, session=s)
    d = res.unwrap()
    assert (d.host, d.key_url, d.status, d.found, d.matches) == (HOST, KEY_URL, 200, True, True)
    assert res.findings == []
    async with api_session(_ok_api(), pages={KEY_URL: _text("other-key-123")}) as s:
        res = await indexnow.verify(HOST, KEY, session=s)
    assert res.unwrap().found and not res.unwrap().matches
    [f] = res.findings
    assert f.id == "indexnow-key-not-verified" and "content differs" in (f.evidence or "")
    async with api_session(_ok_api()) as s:  # unknown URL: the fake answers 404
        res = await indexnow.verify(HOST, KEY, session=s)
    assert not res.unwrap().found and "HTTP 404" in (res.findings[0].evidence or "")


async def test_verify_failures() -> None:
    async with api_session(_ok_api(), errors={KEY_URL: "dns-failed"}) as s:
        res = await indexnow.verify(HOST, KEY, session=s)
        bad = await indexnow.verify("https://example.com/x", KEY, session=s)
    assert res.error is not None and res.error.code is ErrorCode.DNS_FAILED
    assert bad.error is not None and bad.error.details["reason"] == "invalid-host"


# ------------------------------------------------------------------ submit


async def test_submit_filters_and_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexnow, "MAX_URLS_PER_REQUEST", 2)
    fake = _ok_api(202)
    urls = [
        f"https://{HOST}/a",
        f"https://{HOST}/a",  # duplicate
        f"http://{HOST}/b",
        f"https://www.{HOST}/c",  # another host
        "ftp://example.com/d",
        f"https://{HOST}/e",
        "not a url",
    ]
    async with api_session(fake) as s:
        res = await indexnow.submit(HOST, KEY, urls, key_location=f"https://{HOST}/{KEY}.txt", session=s)
    d = res.unwrap()
    assert res.status is Status.OK
    assert (d.submitted, d.accepted, d.duplicates, d.rejected_count, d.skipped) == (3, 3, 1, 3, 0)
    assert d.rejected_urls == [f"https://www.{HOST}/c", "ftp://example.com/d", "not a url"]
    assert [(b.index, b.size, b.status, b.result, b.accepted) for b in d.batches] == [
        (0, 2, 202, "accepted", True),
        (1, 1, 202, "accepted", True),
    ]
    assert [c[0:2] for c in fake.calls] == [("POST", ENDPOINT), ("POST", ENDPOINT)]
    body = fake.calls[0][2]["json"]
    assert body == {
        "host": HOST,
        "key": KEY,
        "keyLocation": f"https://{HOST}/{KEY}.txt",
        "urlList": [f"https://{HOST}/a", f"http://{HOST}/b"],
    }
    assert json.loads(res.to_json())["data"]["submitted"] == 3


async def test_submit_respects_max_batch_urls_and_reports_failed_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indexnow, "MAX_URLS_PER_REQUEST", 2)
    answers = [(200, None), (422, {"message": "urls not on host"})]
    fake = FakeApi(lambda m, u, kw: answers.pop(0))
    urls = [f"https://{HOST}/p{i}" for i in range(10)]
    async with api_session(fake, config=Config(max_batch_urls=4)) as s:
        res = await indexnow.submit(HOST, KEY, urls, session=s)
    d = res.unwrap()
    assert res.status is Status.PARTIAL
    assert (d.submitted, d.accepted, d.skipped) == (4, 2, 6)
    assert [(b.result, b.accepted, b.error) for b in d.batches] == [
        ("ok", True, None),
        ("unprocessable", False, "http-422"),
    ]
    assert len(fake.calls) == 2  # no retries: a submission is not idempotent


async def test_submit_input_errors() -> None:
    fake = _ok_api()
    async with api_session(fake) as s:
        bad_key = await indexnow.submit(HOST, "nope", [f"https://{HOST}/"], session=s)
        bad_loc = await indexnow.submit(
            HOST, KEY, [f"https://{HOST}/"], key_location="https://evil.org/k.txt", session=s
        )
        nothing = await indexnow.submit(HOST, KEY, ["https://other.org/"], session=s)
    assert bad_key.error is not None and bad_key.error.details["reason"] == "invalid-key"
    assert bad_loc.error is not None and bad_loc.error.details["reason"] == "invalid-key-location"
    assert nothing.unwrap().batches == [] and nothing.unwrap().rejected_count == 1
    assert fake.calls == []


def test_submit_is_never_called_by_another_operation() -> None:
    src = Path(seoprobe.__file__).parent
    callers = [
        p
        for p in src.rglob("*.py")
        if re.search(r"\bsubmit\s*\(", p.read_text(encoding="utf-8"))
        and p.name != "indexnow.py"
        and "indexnow" in p.read_text(encoding="utf-8")
    ]
    assert callers == []
    assert "only write operation" in (indexnow.submit.__doc__ or "").lower()


def test_rules_have_texts_and_are_not_scored() -> None:
    from seoprobe.core.rules import catalog

    for rid in ("indexnow-key-not-verified", "bing-crawl-issues"):
        d = catalog.get(rid)
        assert d.scored is False
        for lang in ("en", "tr"):
            assert catalog.message(d.message_key, lang) != d.message_key
            assert catalog.message(d.recommendation_key, lang) != d.recommendation_key
