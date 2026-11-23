"""Shared fakes for the Google / Search Console tests (offline)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from seoprobe.core.config import Config, GoogleApp
from seoprobe.core.session import Session
from seoprobe.features.search import oauth as oauth_mod
from seoprobe.features.search.oauth import GoogleOAuth, HttpResult, MemoryTokenStore
from tests import fakes

SECRET = "client-secret-XYZ-123"
REFRESH = "refresh-token-ABC-456"
ACCESS = "access-token-DEF-789"
APP = GoogleApp("cid.apps.example", SECRET)
CONFIG = Config(google_app=APP)


class FakeGoogle:
    """Records requests; ``handler(method, url, kw) -> HttpResult`` answers them."""

    def __init__(self, handler: Callable[[str, str, dict[str, Any]], HttpResult]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kw: Any) -> HttpResult:
        self.calls.append((method, url, kw))
        return self.handler(method, url, kw)


def connected_store(account: str = "default") -> MemoryTokenStore:
    store = MemoryTokenStore()
    store._d[account] = (  # seeded directly: a sync test fixture
        {
            "refresh_token": REFRESH,
            "access_token": ACCESS,
            "expires_at": 9e12,
            "email": "me@example.com",
        }
    )
    return store


def make_oauth(
    http: FakeGoogle | None = None,
    *,
    with_client: bool = True,
    connected: bool = False,
    now: Callable[[], float] | None = None,
) -> GoogleOAuth:
    store = connected_store() if connected else MemoryTokenStore()
    kw: dict[str, Any] = {"now": now} if now else {}
    return GoogleOAuth(
        APP if with_client else None,
        store,
        http or FakeGoogle(lambda *_: HttpResult(500)),
        **kw,
    )


def use_google(monkeypatch: pytest.MonkeyPatch, google: FakeGoogle) -> FakeGoogle:
    """Route every Google call of the operations to ``google``."""
    monkeypatch.setattr(oauth_mod, "transport", lambda ctx: google)
    return google


def gsc_session(*, store: Any = None, with_client: bool = True) -> Session:
    """A fake session with the Google app configured and ``store`` as its token store."""
    return fakes.session(config=CONFIG if with_client else Config(), token_store=store)


# --------------------------------------------------------------------------- keyed APIs (Bing, IndexNow)


class _Content:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def read(self, n: int = -1) -> bytes:
        return self._raw


class _Resp:
    def __init__(self, status: int, data: Any) -> None:
        raw = data if isinstance(data, bytes) else (json.dumps(data).encode() if data is not None else b"")
        self.status, self.headers = status, {}
        self.content = _Content(raw)

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


class FakeApi:
    """aiohttp-shaped API session: ``handler(method, url, kw) -> (status, body)``; records calls."""

    def __init__(self, handler: Callable[[str, str, dict[str, Any]], tuple[int, Any]]) -> None:
        self.handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kw: Any) -> _Resp:
        self.calls.append((method, url, kw))
        return _Resp(*self.handler(method, url, kw))


class ApiHttp(fakes.FakeHttp):
    def __init__(self, api: FakeApi, config: Config, pages: Any = None, errors: Any = None) -> None:
        super().__init__(pages, errors, config=config)
        self.api = api

    def api_session(self) -> Any:
        return self.api


def api_session(
    api: FakeApi, *, config: Config | None = None, pages: Any = None, errors: Any = None, store: Any = None
) -> Session:
    cfg = config or Config()
    return Session(cfg, http=ApiHttp(api, cfg, pages, errors), token_store=store)  # type: ignore[arg-type]
