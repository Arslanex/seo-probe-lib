"""Builders for the crawl tests: sample pages and scripted fetchers."""

from __future__ import annotations

from typing import Any

from seoprobe.core.config import Config
from seoprobe.core.models import FetchResult, Hop
from seoprobe.core.net.client import MAX_PAGE_BYTES, RawResponse
from seoprobe.core.page import Page
from seoprobe.core.parse.html import parse_html
from seoprobe.core.session import Session

HOST = "https://example.com"


def page(
    path: str,
    html: str,
    *,
    status: int | None = 200,
    headers: dict[str, str] | None = None,
    kind: str = "core",
    final: str | None = None,
    error: str | None = None,
) -> Page:
    url = path if path.startswith("http") else f"{HOST}{path}"
    return Page(
        url=url,
        final_url=final or url,
        status=status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        content_type="text/html",
        html=html if status is not None else None,
        sample_kind=kind,  # type: ignore[arg-type]
        error=error,
    )


def with_parsed(p: Page, *, extract_main_text: bool) -> Page:
    """``p`` with its parse made with or without main-text extraction (the source tests chose)."""
    p._parsed = parse_html(p.html or "", p.effective_url, extract_main_text=extract_main_text)
    p._parsed_done = True
    return p


class Scripted:
    """
    url -> (status, headers, body). Unknown URLs answer 404. Records every request.

    A ``"redirect:<target>"`` body models a redirect that was followed (``fetch`` only).
    """

    def __init__(self, table: dict[str, tuple[int, dict[str, str], str]] | None = None):
        self.table = table or {}
        self.requested: list[str] = []

    async def fetch_bytes(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES) -> RawResponse:
        self.requested.append(url)
        status, headers, body = self.table.get(url, (404, {}, ""))
        return RawResponse(
            url, url, status, {k.lower(): v for k, v in headers.items()}, body.encode(), False, None, [], 1, 1
        )

    async def fetch(self, url: str, *, max_bytes: int | None = MAX_PAGE_BYTES) -> FetchResult:
        self.requested.append(url)
        status, headers, body = self.table.get(url, (404, {}, ""))
        hdrs = {k.lower(): v for k, v in headers.items()}
        final = url
        if body.startswith("redirect:"):
            final = body.split(":", 1)[1]
            status, body = 200, ""
        return FetchResult(
            url=url,
            final_url=final,
            status=status,
            headers=hdrs,
            html=body or None,
            html_bytes=len(body),
            redirect_chain=[Hop(url=url, status=301)] if final != url else [],
        )

    async def close(self) -> None:
        return None


def scripted_session(http: Any, config: Config | None = None) -> Session:
    return Session(config or Config(request_delay_s=0), http=http)
