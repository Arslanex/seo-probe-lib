"""Output models of the offsite feature."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class VisibilitySignals(BaseModel):
    """Public, keyless, informational signals. Never part of the score; never proof of indexing."""

    domain: str
    rdap: dict[str, Any] = Field(default_factory=dict)  # {created, age_days, registrar, error}
    wayback: dict[str, Any] = Field(default_factory=dict)  # {first_capture, last_capture, captures, error}
    common_crawl: dict[str, Any] = Field(default_factory=dict)  # {collection, present, pages_seen, error}
    search_console: dict[str, Any] = Field(default_factory=lambda: {"connected": False})
    notes: list[str] = Field(default_factory=list)


# --- from authority.py


class AuthoritySignal(BaseModel):
    domain: str = ""
    page_rank_decimal: float | None = None  # 0..10, one decimal place
    page_rank_integer: int | None = None
    rank: int | None = None  # global rank; larger numbers are less authoritative
    error: str | None = None


# --- from competitors.py


class CompetitorRow(BaseModel):
    url: str
    host: str = ""
    is_you: bool = False
    final_url: str | None = None
    status: int | None = None
    error: str | None = None  # stable code: target-rejected, timeout, unreachable, not-html, ...
    score: float | None = None
    title_len: int | None = None
    meta_len: int | None = None
    h1_count: int | None = None
    words: int | None = None
    schema_types: list[str] = Field(default_factory=list)
    images: int | None = None
    images_alt_pct: float | None = None  # share of images with an alt attribute
    canonical: Literal["self", "other", "none"] | None = None
    noindex: bool | None = None
    ttfb_ms: int | None = None
    html_bytes: int | None = None
    internal_links: int | None = None
    external_links: int | None = None
    tech: list[str] = Field(default_factory=list)
    readability_formula: str | None = None  # flesch | atesman
    readability_score: float | None = None


class SkippedCompetitor(BaseModel):
    url: str
    reason: str  # invalid-url | same-site | duplicate | over-limit


class CompetitorComparison(BaseModel):
    you: CompetitorRow | None = None
    rows: list[CompetitorRow] = Field(default_factory=list)
    skipped: list[SkippedCompetitor] = Field(default_factory=list)
