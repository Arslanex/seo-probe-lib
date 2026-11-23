"""A FakeHttp that follows redirects like the real client (hop by hop, ``redirect_chain``)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from urllib.parse import urljoin

from seoprobe.core.config import Config
from seoprobe.core.models import FetchResult, Hop
from seoprobe.core.net.client import MAX_REDIRECTS, RawResponse, record
from seoprobe.core.parse.text import decode_body
from seoprobe.core.session import Session
from tests import fakes

Pages = dict[str, tuple[int, dict[str, str], bytes]]


class RedirectingHttp(fakes.FakeHttp):
    async def fetch_bytes(self, url: str, *, max_bytes: int | None = None) -> RawResponse:
        chain: list[Hop] = []
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self.requested.append(current)
            record(current, status=self.pages.get(current, (404,))[0], started=time.monotonic(), size=0, error=None)
            if current in self.errors:
                return RawResponse(url, current, None, {}, b"", False, f"{self.errors[current]}: fixture", chain)
            status, headers, body = self.pages.get(current, (404, {"content-type": "text/plain"}, b"not found"))
            headers = {k.lower(): v for k, v in headers.items()}
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                chain.append(Hop(url=current, status=status))
                current = urljoin(current, headers["location"])
                continue
            return RawResponse(url, current, status, headers, body, False, None, chain, 5, 10)
        return RawResponse(url, current, None, {}, b"", False, "redirect-loop", chain)

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
        raw = await self.fetch_bytes(url, max_bytes=max_bytes)
        code, _, detail = (raw.error or "").partition(": ")
        ctype = raw.headers.get("content-type")
        html = decode_body(raw.body, ctype) if raw.body and (ctype or "").startswith("text/") else None
        return FetchResult(
            url=url,
            final_url=raw.final_url,
            status=raw.status,
            headers=raw.headers,
            redirect_chain=raw.redirect_chain or [],
            content_type=ctype,
            html=html,
            html_bytes=len(raw.body),
            error=code or None,
            error_detail=detail or None,
            fetched_at=datetime.now(UTC),
        )


def session(
    pages: Pages | None = None, *, errors: dict[str, str] | None = None, config: Config | None = None
) -> Session:
    cfg = config or Config()
    return Session(cfg, http=RedirectingHttp(pages, errors, cfg))  # type: ignore[arg-type]


def redirect(to: str, status: int = 301) -> tuple[int, dict[str, str], bytes]:
    return status, {"location": to}, b""


def robots_txt(body: str) -> tuple[int, dict[str, str], bytes]:
    return 200, {"content-type": "text/plain"}, body.encode()
