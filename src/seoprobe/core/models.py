"""Types shared by core and by two or more features. Feature-specific models live in the feature."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Severity(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    INFO = "Info"


SEVERITY_ORDER = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)


class Category(StrEnum):
    TECHNICAL = "technical"
    ONPAGE = "onpage"
    LINKS = "links"
    CONTENT = "content"
    SCHEMA = "schema"
    PERFORMANCE = "performance"
    AI_READINESS = "ai_readiness"
    IMAGES = "images"


class Layer(StrEnum):
    A = "A"
    B = "B"


class AuditMode(StrEnum):
    SITE = "site"
    PAGE = "page"


class Hop(BaseModel):
    url: str
    status: int


class FetchResult(BaseModel):
    """Result of one validated HTTP fetch."""

    url: str
    final_url: str
    status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)  # lower-cased names
    redirect_chain: list[Hop] = Field(default_factory=list)
    content_type: str | None = None
    html: str | None = None  # decoded text body (None for non-HTML / failure)
    html_bytes: int = 0
    truncated: bool = False
    ttfb_ms: int | None = None
    total_ms: int | None = None
    error: str | None = None  # stable code, e.g. "unreachable", "dns-failed", "target-rejected", "timeout"
    error_detail: str | None = Field(default=None, exclude=True)  # internal; never serialised
    fetched_at: datetime | None = None


class Finding(BaseModel):
    """One deterministic finding. Immutable once produced."""

    id: str
    rule_version: str = "0.1.0"
    category: Category
    layer: Layer = Layer.A
    severity: Severity
    severity_original: Severity | None = None
    severity_reason: str | None = None
    scope: Literal["page", "site"] = "page"
    url: str | None = None
    message_key: str
    message: str
    evidence: str | None = None  # <= 200 chars, untrusted (comes from the page)
    count: int | None = None
    heuristic: bool = False
    confidence: Literal["high", "medium", "low"] = "high"
    recommendation_key: str | None = None
    recommendation: str | None = None
    effort: Literal["S", "M", "L"] | None = None
    impact: Literal["low", "medium", "high"] | None = None


class NotChecked(BaseModel):
    check_id: str
    reason_code: str  # stable code, e.g. "headers-unavailable", "missing-credential:google_api_key"
    url: str | None = None


class CheckOutput(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    not_checked: list[NotChecked] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)


class Requirements(BaseModel):
    network: bool = True
    browser: bool = False
    extras: list[str] = Field(default_factory=list)
    credentials: list[str] = Field(default_factory=list)  # Config field names
    oauth: bool = False
    note: str | None = None


class Preflight(BaseModel):
    operation: str
    ready: bool
    missing: list[str] = Field(default_factory=list)
