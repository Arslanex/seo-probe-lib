"""Builders for the site tests: sample pages (the source tests.support.modules ``page``)."""

from __future__ import annotations

from seoprobe.core.page import Page

HOST = "https://example.com"


def page(
    path: str,
    html: str,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    kind: str = "core",
) -> Page:
    url = path if path.startswith("http") else f"{HOST}{path}"
    return Page(
        url=url,
        final_url=url,
        status=status,
        headers={k.lower(): v for k, v in (headers or {}).items()},
        content_type="text/html",
        html=html,
        sample_kind=kind,  # type: ignore[arg-type]
    )
