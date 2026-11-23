"""Page: the shared input of every feature, and the two operations that make one."""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, PrivateAttr

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import FetchResult, Requirements
from seoprobe.core.net.safety import URLSafetyError, check_url
from seoprobe.core.parse.html import ParsedPage, parse_html
from seoprobe.core.parse.urls import normalize_url, origin_of


class Page(FetchResult):
    """One fetched page. ``parsed`` is computed on first access and cached."""

    sample_kind: Literal["core", "exploration"] = "core"
    rendered_html: str | None = None  # set when the page was rendered in the browser

    _parsed: ParsedPage | None = PrivateAttr(default=None)
    _parsed_done: bool = PrivateAttr(default=False)

    @classmethod
    def from_fetch(cls, fr: FetchResult, *, sample_kind: Literal["core", "exploration"] = "core") -> Page:
        return cls(**fr.model_dump(), sample_kind=sample_kind)

    @property
    def effective_url(self) -> str:
        return self.final_url or self.url

    @property
    def content_html(self) -> str:
        """The rendered HTML when there is one, else the raw HTML."""
        return self.rendered_html or self.html or ""

    @property
    def html_ok(self) -> bool:
        """A response with an HTML body arrived (any status)."""
        return self.error is None and self.status is not None and self.html is not None

    @property
    def parsed(self) -> ParsedPage | None:
        """
        ``content_html`` parsed; None unless the page is an HTML page with a status below 400
        (the pages the analyses run on).
        """
        if not self._parsed_done:
            ok = self.html_ok and self.status is not None and self.status < 400 and bool(self.content_html)
            self._parsed = parse_html(self.content_html, self.effective_url) if ok else None
            self._parsed_done = True
        return self._parsed

    async def parse(self) -> ParsedPage | None:
        """
        ``parsed``, computed on a worker thread: parsing a large page takes hundreds of
        milliseconds and must not block the caller's event loop.
        """
        if self._parsed_done:
            return self._parsed
        return await asyncio.to_thread(lambda: self.parsed)

    def with_rendered(self, html: str) -> Page:
        """A copy carrying the rendered HTML (its ``parsed`` is computed from that HTML)."""
        copy = self.model_copy(update={"rendered_html": html})
        copy._parsed, copy._parsed_done = None, False
        return copy


class NormalizedUrl(BaseModel):
    input: str
    url: str | None  # normalised absolute URL, None when the input is not a usable web URL
    origin: str | None = None
    host: str | None = None
    allowed: bool = False  # passes the SSRF policy (no DNS lookup)
    reason: str | None = None  # why it is not allowed


@operation("fetch", output=Page, requires=Requirements(network=True), timeout_s=60.0)
async def fetch(ctx: Context, url: str) -> Outcome[Page]:
    """Fetch and parse one URL (SSRF-safe, redirects validated hop by hop)."""
    return Outcome(data=await ctx.page(url))


@operation("normalize", output=NormalizedUrl, requires=Requirements(network=False), timeout_s=5.0)
async def normalize(ctx: Context, url: str) -> Outcome[NormalizedUrl]:
    """Normalise a URL and say whether it may be fetched (no network access)."""
    raw = url.strip()
    candidate = raw if "://" in raw else f"https://{raw}"
    norm = normalize_url(candidate)
    if norm is None:
        return Outcome(data=NormalizedUrl(input=url, url=None, reason="invalid-url"))
    try:
        target = check_url(norm)
    except URLSafetyError as exc:
        return Outcome(data=NormalizedUrl(input=url, url=norm, origin=origin_of(norm), reason=exc.code.value))
    return Outcome(data=NormalizedUrl(input=url, url=norm, origin=origin_of(norm), host=target.hostname, allowed=True))
