"""Output models of the page feature."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OnPageReport(BaseModel):
    """The single-page checks of one page; ``checked`` is False when the page had no parseable HTML."""

    url: str
    checked: bool
    title: str | None = None
    word_count: int | None = None
    facts: dict[str, Any] = Field(default_factory=dict)


class Classification(BaseModel):
    """Detected primary language and business type of one page (labels only)."""

    url: str
    lang: str
    business_type: str
