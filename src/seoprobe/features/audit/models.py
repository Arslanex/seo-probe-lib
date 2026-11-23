"""Output models of the audit feature."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from seoprobe.core.errors import ErrorCode
from seoprobe.core.models import AuditMode, Finding, NotChecked
from seoprobe.core.rules.scoring import PageRecord, Scores
from seoprobe.features.crawl import IndexabilityRow, LinkSummary
from seoprobe.features.offsite import VisibilitySignals
from seoprobe.features.performance import FieldData, PagePerf
from seoprobe.features.search import GscData


class AuditStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class Stage(StrEnum):
    VALIDATE = "validate"
    FETCH_ENTRY = "fetch_entry"
    ROBOTS = "robots"
    DISCOVERY = "discovery"
    SAMPLE = "sample"
    FETCH_PAGES = "fetch_pages"
    RENDER = "render"
    MEASURE = "measure"
    ANALYZE = "analyze"
    SCORE = "score"
    REPORT = "report"


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class StageRecord(BaseModel):
    stage: Stage
    status: StageStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str | None = None


class AuditOptions(BaseModel):
    """The caller's options of an audit, normalised; ``rerun`` repeats them."""

    max_pages: int | None = None
    extra_urls: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    seed: int | None = None  # the seed the caller asked for (None: a fresh one per audit)
    link_check_budget_s: float | None = None  # None: Config.link_check_budget_s
    visibility_budget_s: float | None = None  # None: Config.visibility_budget_s
    account: str = "default"  # the token-store account whose Search Console data the audit reads


class Audit(BaseModel):
    """Everything one audit produced: the input of ``seoprobe.report``."""

    id: str
    mode: AuditMode
    target_url: str
    final_origin: str | None = None
    status: AuditStatus
    business_type: str | None = None
    lang: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
    rule_version: str = "0.1.0"
    score_version: str = "mvp-0.1"
    coverage: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    cost: dict[str, Any] = Field(default_factory=dict)
    stages: list[StageRecord] = Field(default_factory=list)
    pages: list[PageRecord] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    not_checked: list[NotChecked] = Field(default_factory=list)
    scores: Scores | None = None
    links: LinkSummary | None = None
    perf: list[PagePerf] = Field(default_factory=list)
    indexability: list[IndexabilityRow] = Field(default_factory=list)
    visibility: VisibilitySignals | None = None
    gsc: GscData | None = None
    field: FieldData | None = None
    extras: dict[str, Any] = Field(default_factory=dict)  # blocks of pluggable analysis modules, by kind
    error: str | None = None
    error_code: ErrorCode | None = None  # the stable code of ``error`` when the audit failed
    options: AuditOptions = Field(default_factory=AuditOptions)
    seed: int | None = None  # sample seed applied (site mode)
    partial_reasons: list[str] = Field(default_factory=list)
