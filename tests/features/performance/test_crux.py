from __future__ import annotations

import json
from typing import Any

import pytest

from seoprobe.core.net import api
from seoprobe.features.performance.google import fetch_crux, field_checks, parse_crux

DATA = {
    "record": {
        "metrics": {
            "largest_contentful_paint": {
                "histogram": [{"density": 0.5}, {"density": 0.3}, {"density": 0.2}],
                "percentiles": {"p75": 3100},
            },
            "interaction_to_next_paint": {
                "histogram": [{"density": 0.9}, {"density": 0.07}, {"density": 0.03}],
                "percentiles": {"p75": 150},
            },
            "cumulative_layout_shift": {
                "histogram": [{"density": 0.8}, {"density": 0.1}, {"density": 0.1}],
                "percentiles": {"p75": "0.31"},
            },
        },
        "collectionPeriod": {
            "firstDate": {"year": 2026, "month": 10, "day": 1},
            "lastDate": {"year": 2026, "month": 10, "day": 28},
        },
    }
}


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw


class _Resp:
    """The part of an aiohttp response ``request_json`` reads."""

    def __init__(self, status: int, data: dict[str, Any]) -> None:
        self.status, self.headers = status, {"Content-Type": "application/json"}
        self.content = _Content(json.dumps(data).encode() if data else b"")

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class _Session:
    def __init__(self, answers: list[tuple[int, dict[str, Any]]]) -> None:
        self.answers, self.bodies = list(answers), []
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kw: Any) -> _Resp:
        self.calls.append({"method": method, "url": url, **kw})
        self.bodies.append(kw.get("json"))
        return _Resp(*self.answers.pop(0))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "_retry_delay", lambda attempt, retry_after: 0.0)


def test_parse_ratings_and_period() -> None:
    fd = parse_crux(DATA, "url", None)
    by = {m.metric: m for m in fd.metrics}
    assert by["lcp"].rating == "needs-improvement" and by["inp"].rating == "good" and by["cls"].rating == "poor"
    assert by["cls"].p75 == 0.31 and by["lcp"].poor_share == 0.2
    assert fd.period == {"first": "2026-10-01", "last": "2026-10-28"}


def test_findings_use_cwv_rules_and_mark_field_evidence() -> None:
    out = field_checks(parse_crux(DATA, "origin", None), "https://e.com/")
    ids = {f.id for f in out.findings}
    assert ids == {"cwv-lcp-needs-improvement", "cwv-cls-poor"}  # INP is good: no finding
    assert "field" in (out.findings[0].evidence or "")


async def test_url_level_record_wins() -> None:
    s = _Session([(200, DATA)])
    fd = await fetch_crux("https://e.com/a", "https://e.com", session=s, key="k")
    assert fd.scope == "url" and s.bodies == [{"url": "https://e.com/a"}]


async def test_falls_back_to_origin_when_the_page_has_no_record() -> None:
    s = _Session([(404, {}), (200, DATA)])
    fd = await fetch_crux("https://e.com/a", "https://e.com", session=s, key="k")
    assert fd.scope == "origin" and s.bodies[1] == {"origin": "https://e.com"}


async def test_no_traffic_is_no_data_not_an_error() -> None:
    fd = await fetch_crux("https://e.com/", "https://e.com", session=_Session([(404, {}), (404, {})]), key="k")
    assert fd.error == "no-data" and fd.metrics == []


async def test_missing_key_and_bad_key_are_reported_with_codes() -> None:
    assert (await fetch_crux("https://e.com/", "https://e.com", session=_Session([]), key="")).error == "no-api-key"
    assert (
        await fetch_crux("https://e.com/", "https://e.com", session=_Session([(403, {})]), key="k")
    ).error == "http-403"
    assert (
        await fetch_crux("https://e.com/", "https://e.com", session=_Session([(429, {})] * 4), key="k")
    ).error == "rate-limited"


async def test_the_key_travels_in_a_header_never_in_the_url() -> None:
    s = _Session([(200, DATA)])
    await fetch_crux("https://e.com/a", "https://e.com", session=s, key="SECRET-KEY")
    call = s.calls[0]
    assert call["method"] == "POST" and call["headers"] == {"X-Goog-Api-Key": "SECRET-KEY"}
    assert "SECRET-KEY" not in call["url"] and not call.get("params")
