"""Builders for the content tests: a Page from HTML (replaces the source ``tests.support.modules``)."""

from __future__ import annotations

from typing import Literal

from seoprobe.core.page import Page

HOST = "https://example.com"


def page(
    path: str,
    html: str,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    kind: Literal["core", "exploration"] = "core",
) -> Page:
    url = path if path.startswith("http") else f"{HOST}{path}"
    return Page(
        url=url,
        final_url=url,
        status=status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        content_type="text/html; charset=utf-8",
        html=html,
        html_bytes=len(html.encode()),
        sample_kind=kind,
    )
