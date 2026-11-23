"""External API calls: retry policy, key masking, address policy."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from seoprobe.core.net import api
from seoprobe.core.net.client import recording


class _Content:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, n: int = -1) -> bytes:
        return self._body


class _Resp:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _Content(b"" if body is None else json.dumps(body).encode())


class FakeApiSession:
    """Answers with the scripted responses in order; records every call."""

    def __init__(self, *responses: _Resp) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def request(self, method: str, url: str, **kw: Any) -> AsyncIterator[_Resp]:
        self.calls.append({"method": method, "url": url, **kw})
        yield self.responses.pop(0)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)
    return slept


async def test_success_returns_parsed_json() -> None:
    s = FakeApiSession(_Resp(200, {"ok": 1}))
    assert await api.request_json(s, "GET", "https://api.example/x") == (200, {"ok": 1}, None)


async def test_429_is_retried_honouring_retry_after(_no_sleep: list[float]) -> None:
    s = FakeApiSession(_Resp(429, headers={"Retry-After": "2"}), _Resp(200, {"ok": 1}))
    status, body, err = await api.request_json(s, "GET", "https://api.example/x")
    assert (status, body, err) == (200, {"ok": 1}, None)
    assert len(s.calls) == 2 and _no_sleep == [2.0]


async def test_5xx_is_retried_with_capped_backoff_then_reported(_no_sleep: list[float]) -> None:
    s = FakeApiSession(*(_Resp(503) for _ in range(api.RETRIES + 1)))
    status, body, err = await api.request_json(s, "GET", "https://api.example/x")
    assert status == 503 and err == "http-503"
    assert len(s.calls) == api.RETRIES + 1
    assert all(0 <= d <= api.BACKOFF_CAP_S for d in _no_sleep) and len(_no_sleep) == api.RETRIES


async def test_final_429_is_rate_limited() -> None:
    s = FakeApiSession(*(_Resp(429) for _ in range(api.RETRIES + 1)))
    assert (await api.request_json(s, "GET", "https://api.example/x"))[2] == "rate-limited"


async def test_client_errors_are_not_retried_and_keep_the_error_body() -> None:
    s = FakeApiSession(_Resp(400, {"error": {"message": "bad"}}))
    status, body, err = await api.request_json(s, "GET", "https://api.example/x")
    assert (status, err) == (400, "http-400") and body == {"error": {"message": "bad"}}
    assert len(s.calls) == 1


async def test_keys_in_params_are_masked_in_the_request_log() -> None:
    s = FakeApiSession(_Resp(200, {}))
    with recording(10**9) as rec:
        await api.request_json(s, "GET", "https://api.example/x", params={"key": "SECRET", "q": "1"})
    [entry] = rec.requests
    assert "SECRET" not in entry.url and "key=***" in entry.url


async def test_header_keys_never_reach_the_log() -> None:
    s = FakeApiSession(_Resp(200, {}))
    with recording(10**9) as rec:
        await api.request_json(s, "GET", "https://api.example/x", headers={"X-Goog-Api-Key": "SECRET"})
    assert "SECRET" not in rec.requests[0].model_dump_json()
    assert s.calls[0]["headers"] == {"X-Goog-Api-Key": "SECRET"}


@pytest.mark.parametrize("url", ["http://api.example/x", "https://127.0.0.1/x", "https://169.254.169.254/"])
async def test_non_https_and_private_targets_are_rejected_without_a_call(url: str) -> None:
    s = FakeApiSession()
    assert await api.request_json(s, "GET", url) == (None, None, "target-rejected")
    assert s.calls == []


async def test_budget_exhaustion_stops_calls() -> None:
    s = FakeApiSession(_Resp(200, {}))
    with recording(0):
        assert await api.request_json(s, "GET", "https://api.example/x") == (None, None, "budget-exceeded")
    assert s.calls == []


def test_host_of() -> None:
    assert api.host_of("https://WWW.Example.com:8443/a") == "www.example.com"
    assert api.host_of(".example.com") == "example.com"
