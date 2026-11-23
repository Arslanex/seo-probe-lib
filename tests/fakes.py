"""Test doubles shared by every test package. Never used in production."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from seoprobe.core.config import Config
from seoprobe.core.models import FetchResult
from seoprobe.core.net.client import MAX_PAGE_BYTES, RawResponse
from seoprobe.core.net.politeness import HostLimiter
from seoprobe.core.parse.text import decode_body
from seoprobe.core.session import Session


class FakeHttp:
    """
    Serves canned responses with the HttpClient interface.

    pages: url -> (status, headers, body bytes). Unknown URLs -> 404. errors: url -> error code
    (e.g. "unreachable", "tls-handshake-failed").
    """

    def __init__(
        self,
        pages: dict[str, tuple[int, dict[str, str], bytes]] | None = None,
        errors: dict[str, str] | None = None,
        config: Config | None = None,
    ) -> None:
        self.pages = pages or {}
        self.errors = errors or {}
        self.requested: list[str] = []
        self.config = config or Config()
        self.limiter = HostLimiter(self.config.per_host_concurrency, 0.0)
        self.user_agent = self.config.user_agent

    async def __aenter__(self) -> FakeHttp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
        limit = MAX_PAGE_BYTES if max_bytes is None else max_bytes
        self.requested.append(url)
        if url in self.errors:
            return RawResponse(url, url, None, {}, b"", False, f"{self.errors[url]}: fixture", [], None, 5)
        status, headers, body = self.pages.get(url, (404, {"content-type": "text/plain"}, b"not found"))
        return RawResponse(
            url,
            url,
            status,
            {k.lower(): v for k, v in headers.items()},
            body[:limit],
            len(body) > limit,
            None,
            [],
            5,
            10,
        )

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
        raw = await self.fetch_bytes(url, max_bytes=max_bytes)
        if raw.error:
            code, _, detail = raw.error.partition(": ")
            return FetchResult(
                url=url, final_url=raw.final_url, error=code, error_detail=detail, fetched_at=datetime.now(UTC)
            )
        ctype = raw.headers.get("content-type")
        html = (
            decode_body(raw.body, ctype)
            if raw.body and (ctype or "").startswith(("text/", "application/xhtml", "application/xml"))
            else None
        )
        return FetchResult(
            url=url,
            final_url=raw.final_url,
            status=raw.status,
            headers=raw.headers,
            content_type=ctype,
            html=html,
            html_bytes=len(raw.body),
            truncated=raw.truncated,
            ttfb_ms=5,
            total_ms=10,
            fetched_at=datetime.now(UTC),
        )

    def api_session(self) -> Any:
        raise AssertionError("FakeHttp has no API session; pass a fake session to the API call")

    async def close(self) -> None:
        return None


def html_page(body: str, status: int = 200, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "text/html; charset=utf-8", **(headers or {})}, body.encode()


def session(
    pages: dict[str, tuple[int, dict[str, str], bytes]] | None = None,
    *,
    errors: dict[str, str] | None = None,
    config: Config | None = None,
    browser: Any = None,
    **kwargs: Any,
) -> Session:
    """A Session over a FakeHttp (use it with ``async with``)."""
    cfg = config or Config()
    return Session(cfg, http=FakeHttp(pages, errors, cfg), browser=browser, **kwargs)  # type: ignore[arg-type]
