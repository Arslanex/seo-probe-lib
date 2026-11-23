"""Output models of the report feature: the ``AuditReport`` schema, the action plan, the comparison."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from seoprobe.core.models import AuditMode, Category, Layer, Severity
from seoprobe.features.audit import AuditStatus

SCHEMA_VERSION = "1.6.0"
ENGINE_VERSION = "0.1.0"

Confidence = Literal["high", "medium", "low"]
Score = Annotated[float, Field(ge=0, le=100)]


class Evidence(BaseModel):
    """Page-derived text. ALWAYS untrusted; consumers must render it as text."""

    value: Annotated[str, Field(max_length=200)]
    truncated: bool = False


class Gate(BaseModel):
    id: str
    cap: int
    finding_id: str | None = None
    url: str | None = None
    message: str = ""


class OverallScore(BaseModel):
    value: Score | None
    display: Literal["number", "insufficient_data"]
    raw_value: Score | None = None
    coverage_pct: Score
    confidence: Confidence
    band: Literal["good", "attention", "poor"] | None = None
    gate: Gate | None = None

    @model_validator(mode="after")
    def _consistent(self) -> OverallScore:
        if self.display == "insufficient_data" and self.value is not None:
            raise ValueError("insufficient data: no number may be shown")
        if self.display == "number" and self.value is None:
            raise ValueError("display=number requires a value")
        if self.gate and self.value is not None and self.value > self.gate.cap + 1e-9:
            raise ValueError("score exceeds the gate cap")
        return self


class Penalty(BaseModel):
    finding_id: str | None = None
    check_id: str
    severity: Severity
    points: float


class CheckRow(BaseModel):
    """One applicable check behind a category score."""

    check_id: str
    message: str
    severity: Severity
    weight: float
    pass_ratio: Annotated[float, Field(ge=0, le=1)]
    pages_affected: int = 0
    pages_assessed: int = 1
    status: Literal["pass", "warn", "fail"] = "pass"


class CategoryReport(BaseModel):
    id: Category
    label: str
    weight_default: float
    weight_effective: float | None = None
    status: Literal["measured", "partial", "not_measured"]
    score: Score | None = None
    confidence: Confidence | None = None
    penalties: list[Penalty] = Field(default_factory=list)
    not_measured_reason: str | None = None
    not_measured_text: str | None = None
    finding_ids: list[str] = Field(default_factory=list)
    checks: list[CheckRow] = Field(default_factory=list)
    checks_total: int = 0
    checks_passed: int = 0
    checks_failed: int = 0

    @model_validator(mode="after")
    def _consistent(self) -> CategoryReport:
        if self.status == "not_measured" and self.score is not None:
            raise ValueError("a not_measured category cannot carry a score")
        if self.status != "not_measured" and self.score is None:
            raise ValueError("a measured category needs a score")
        return self


class Scores(BaseModel):
    kind: Literal["health", "page_card"]
    overall: OverallScore
    categories: list[CategoryReport]
    p25: Score | None = None
    core_overall: Score | None = None


class Affected(BaseModel):
    count: int
    share_of_sampled_pages: float | None = None
    examples: list[str] = Field(default_factory=list)


class FindingReport(BaseModel):
    id: str
    check_id: str
    rule_version: str
    category: Category
    layer: Layer
    severity: Severity
    severity_original: Severity | None = None
    severity_reason: str | None = None
    scope: Literal["page", "site"]
    url: str | None = None
    urls: list[str] = Field(default_factory=list)
    affected: Affected
    message_key: str
    message: str
    heuristic: bool = False
    evidence: Evidence | None = None
    count: int | None = None
    confidence: Confidence = "high"
    recommendation_ids: list[str] = Field(default_factory=list)
    is_gate: bool = False


class NotCheckedReport(BaseModel):
    check_id: str
    reason_code: str
    reason: str
    url: str | None = None


class Effort(BaseModel):
    level: Literal["S", "M", "L"]
    hours_min: float
    hours_max: float


class Impact(BaseModel):
    level: Literal["low", "medium", "high"]


class Recommendation(BaseModel):
    id: str
    finding_ids: list[str]
    check_ids: list[str]
    title: str
    body: str
    severity: Severity
    effort: Effort
    impact: Impact
    confidence: Confidence = "high"
    depends_on: list[str] = Field(default_factory=list)
    priority_score: float
    failure_check: str | None = None
    leading_indicator: str | None = None
    phase: int = 4


class PhaseItem(BaseModel):
    recommendation_id: str
    order: int


class Phase(BaseModel):
    id: str
    number: int
    name_key: str
    name: str
    timeframe_key: str
    timeframe: str
    items: list[PhaseItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ActionPlan(BaseModel):
    phases: list[Phase]


class PageSummary(BaseModel):
    url: str
    final_url: str | None = None
    status: int | None = None
    role: Literal["core", "exploration"] = "core"
    template_key: str | None = None
    title: str | None = None
    page_score: Score | None = None
    rendered: bool = False
    error: str | None = None


class CrawlInfo(BaseModel):
    limits: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    templates: list[dict[str, Any]] = Field(default_factory=list)
    sample_seed: str | None = None


class Limitation(BaseModel):
    code: str
    message: str


class DataSource(BaseModel):
    id: Literal["fetch", "render", "lab", "psi", "gsc", "crux"]
    label: str
    status: Literal["ok", "missing", "failed"]


class Methodology(BaseModel):
    notes: list[str]


class AuditInfo(BaseModel):
    id: str
    mode: AuditMode
    status: AuditStatus
    locale: Literal["tr", "en"]
    requested_url: str
    final_url: str | None = None
    business_type: str | None = None
    language: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    error_text: str | None = None


class Versions(BaseModel):
    score_version: str
    rule_version: str
    engine_version: str = ENGINE_VERSION


class Trend(BaseModel):
    basis: Literal["core_sample"] = "core_sample"
    comparable: bool = False
    reason_if_not: str | None = "first_audit"
    core_overall: Score | None = None


# --------------------------------------------------------------------------- deep analysis sections


class SectionState(BaseModel):
    """Every deep section is either measured (with data) or not measured (with a reason): never an empty table."""

    status: Literal["measured", "not_measured"] = "not_measured"
    not_measured_reason: str | None = None
    not_measured_text: str | None = None
    truncated: bool = False


class ScopeNote(BaseModel):
    """ "What this score does not measure"."""

    title: str
    intro: str
    items: list[str]
    next_step: str
    model_note: str


class LinkRow(BaseModel):
    source_url: str
    target_url: str
    anchor: str = ""
    kind: Literal["internal", "external"]
    rel: list[str] = Field(default_factory=list)
    zone: Literal["nav", "footer", "main", "other"] = "other"
    target_status: int | None = None
    final_url: str | None = None
    redirect_hops: int = 0
    error: str | None = None
    error_text: str | None = None
    state: Literal["ok", "broken", "redirect", "blocked", "unchecked"] = "unchecked"


class LinkPageRow(BaseModel):
    url: str
    inlinks: int = 0
    outlinks_internal: int = 0
    outlinks_external: int = 0
    depth: int | None = None
    in_sitemap: bool | None = None


class DomainCount(BaseModel):
    domain: str
    count: int


class LinksSection(SectionState):
    summary: dict[str, Any] = Field(default_factory=dict)
    broken: list[LinkRow] = Field(default_factory=list)
    redirecting: list[LinkRow] = Field(default_factory=list)
    external_domains: list[DomainCount] = Field(default_factory=list)
    pages: list[LinkPageRow] = Field(default_factory=list)
    pages_total: int = 0
    orphans: list[str] = Field(default_factory=list)
    links: list[LinkRow] = Field(default_factory=list)
    links_total: int = 0


class TypeShare(BaseModel):
    type: str
    count: int
    kb: float


class PerfRow(BaseModel):
    url: str
    device: Literal["desktop", "mobile"]
    ttfb_ms: int | None = None
    fcp_ms: int | None = None
    lcp_ms: int | None = None
    cls: float | None = None
    tbt_ms: int | None = None
    dom_content_loaded_ms: int | None = None
    load_ms: int | None = None
    ratings: dict[str, str] = Field(default_factory=dict)  # metric -> good | needs-improvement | poor
    requests: int = 0
    total_kb: float = 0.0
    html_kb: float = 0.0
    render_blocking: int = 0
    by_type: list[TypeShare] = Field(default_factory=list)
    mobile: dict[str, Any] | None = None
    error: str | None = None
    error_text: str | None = None


class HostShare(BaseModel):
    host: str
    requests: int
    kb: float


class PerfSection(SectionState):
    rows: list[PerfRow] = Field(default_factory=list)
    rows_total: int = 0
    by_type: list[TypeShare] = Field(default_factory=list)  # aggregated over measured pages
    third_party: list[HostShare] = Field(default_factory=list)
    uncompressed: list[str] = Field(default_factory=list)
    no_cache: list[str] = Field(default_factory=list)
    lab_note: str = ""


class ImageRow(BaseModel):
    page_url: str
    src: str
    format: str | None = None
    kb: float | None = None
    natural: str | None = None  # "1200x800"
    displayed: str | None = None
    category: Literal["thumbnail", "content", "hero"] = "content"
    above_fold: bool = False
    lazy: bool = False
    has_alt: bool = True
    issues: list[str] = Field(default_factory=list)
    issue_texts: list[str] = Field(default_factory=list)


class FormatShare(BaseModel):
    format: str
    count: int
    kb: float


class ImagesSection(SectionState):
    rows: list[ImageRow] = Field(default_factory=list)
    rows_total: int = 0
    total_kb: float = 0.0
    by_format: list[FormatShare] = Field(default_factory=list)
    thresholds_note: str = ""


class IndexRow(BaseModel):
    url: str
    status: int | None = None
    indexable: bool = True
    reasons: list[str] = Field(default_factory=list)
    reason_texts: list[str] = Field(default_factory=list)
    canonical: str | None = None
    in_sitemap: bool | None = None
    inlinks: int | None = None
    depth: int | None = None


class IndexabilitySection(SectionState):
    rows: list[IndexRow] = Field(default_factory=list)
    rows_total: int = 0
    indexable_count: int = 0
    blocked_count: int = 0
    note: str = ""


class VisItem(BaseModel):
    id: Literal["rdap", "wayback", "common_crawl", "search_console"]
    label: str
    value: str | None = None
    detail: str | None = None
    available: bool = True
    error_text: str | None = None


class VisibilitySection(SectionState):
    domain: str | None = None
    items: list[VisItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    disclaimer: str = ""
    counts_toward_overall: Literal[False] = False


class GscMetricRow(BaseModel):
    """One query or page of the Search Console performance report."""

    key: str
    clicks: int = 0
    impressions: int = 0
    ctr_pct: float = 0.0
    position: float = 0.0


class GscInspectionRow(BaseModel):
    """Google's own view of one URL (URL Inspection API)."""

    url: str
    indexed: bool | None = None
    coverage_state: str | None = None
    verdict: str | None = None
    google_canonical: str | None = None
    user_canonical: str | None = None
    canonical_mismatch: bool = False
    last_crawl: str | None = None
    crawled_as: str | None = None
    robots_state: str | None = None
    fetch_state: str | None = None
    error: str | None = None
    error_text: str | None = None


class GscSitemapRow(BaseModel):
    path: str
    last_submitted: str | None = None
    last_downloaded: str | None = None
    pending: bool = False
    errors: int = 0
    warnings: int = 0


class GscSection(SectionState):
    """Search Console data of the audited property: informational, indexing findings reach the technical score elsewhere."""

    gsc_status: str = "not-connected"  # the stored GscData.status
    can_connect: bool = False  # not-connected: the interactive page offers the connect link
    account: str | None = None
    property: str | None = None
    permission: str | None = None
    period_days: int = 28
    totals: dict[str, float] = Field(default_factory=dict)
    top_queries: list[GscMetricRow] = Field(default_factory=list)
    top_pages: list[GscMetricRow] = Field(default_factory=list)
    inspections: list[GscInspectionRow] = Field(default_factory=list)
    inspections_total: int = 0
    indexed_count: int = 0
    not_indexed_count: int = 0
    mismatch_count: int = 0
    sitemaps: list[GscSitemapRow] = Field(default_factory=list)
    note: str = ""
    delay_note: str = ""


class FieldMetricRow(BaseModel):
    metric: Literal["lcp", "inp", "cls", "fcp", "ttfb"]
    label: str
    p75: float | None = None
    p75_text: str = "—"
    rating: Literal["good", "needs-improvement", "poor"] | None = None
    good_pct: float | None = None
    needs_improvement_pct: float | None = None
    poor_pct: float | None = None


class FieldSection(SectionState):
    """Real-user (CrUX) p75 values. Distinct from the LAB measurement of the performance section."""

    source: str = "crux"
    scope: Literal["url", "origin"] | None = None
    form_factor: str | None = None
    period_first: str | None = None
    period_last: str | None = None
    metrics: list[FieldMetricRow] = Field(default_factory=list)
    note: str = ""
    counts_toward_overall: Literal[False] = False


# --------------------------------------------------------------------------- suite


class KV(BaseModel):
    label: str
    value: str = ""


class TechRow(BaseModel):
    name: str
    version: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"
    evidence: str = ""
    website: str | None = None


class TechGroup(BaseModel):
    category: str
    label: str
    items: list[TechRow] = Field(default_factory=list)


class TechSection(SectionState):
    groups: list[TechGroup] = Field(default_factory=list)
    total: int = 0
    note: str = ""


class RedirectRow(BaseModel):
    name: str
    label: str
    variant_url: str
    status: int | None = None
    final_url: str | None = None
    hops: int = 0
    permanent: bool | None = None
    note: str | None = None


class FileRow(BaseModel):
    id: str
    label: str
    url: str
    present: bool | None = None
    status: int | None = None
    note: str | None = None


class SiteSetupSection(SectionState):
    redirects: list[RedirectRow] = Field(default_factory=list)
    soft_404: Literal["ok", "problem", "unknown"] = "unknown"
    soft_404_text: str = ""
    custom_404_text: str = ""
    headers: list[KV] = Field(default_factory=list)
    hsts_text: str = ""
    http_version_text: str = ""
    files: list[FileRow] = Field(default_factory=list)
    skipped: list[KV] = Field(default_factory=list)
    requests_made: int = 0


class SerpRow(BaseModel):
    device: str
    device_label: str
    display_url: str
    title_shown: str
    title_truncated: bool
    description_shown: str
    description_truncated: bool
    description_missing: bool


class PreviewsSection(SectionState):
    serp: list[SerpRow] = Field(default_factory=list)
    social: list[KV] = Field(default_factory=list)
    social_image: list[KV] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ShotRow(BaseModel):
    device: str
    device_label: str
    name: str
    width: int
    height: int
    kb: float
    data_base64: str | None = None  # the JPEG itself, when the render feature kept it


class FoldRow(BaseModel):
    device: str
    device_label: str
    h1_present: bool
    h1_visible: bool
    cta_visible: bool
    text_chars: int
    overflow: bool
    overlay_pct: float


class ScreenshotsSection(SectionState):
    shots: list[ShotRow] = Field(default_factory=list)
    above_fold: list[FoldRow] = Field(default_factory=list)
    errors: list[KV] = Field(default_factory=list)


class KeywordRow(BaseModel):
    keyword: str
    in_title: bool
    in_h1: bool
    in_description: bool
    in_url: bool
    in_first_100_words: bool
    in_subheadings: bool
    occurrences: int
    density_percent: float


class ReadabilitySection(SectionState):
    language: str | None = None
    scores: list[KV] = Field(default_factory=list)
    terms: list[KV] = Field(default_factory=list)
    phrases: list[KV] = Field(default_factory=list)
    keywords: list[KeywordRow] = Field(default_factory=list)
    keywords_not_measured_text: str | None = None
    suggested_targets: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class HreflangRow(BaseModel):
    url: str
    html_lang: str = ""
    canonical: str | None = None
    entries: list[KV] = Field(default_factory=list)  # label = language code, value = href
    issues: list[str] = Field(default_factory=list)


class HreflangSection(SectionState):
    languages: list[str] = Field(default_factory=list)
    has_x_default: bool = False
    rows: list[HreflangRow] = Field(default_factory=list)
    rows_total: int = 0
    unchecked: int = 0
    notes: list[str] = Field(default_factory=list)


class SchemaItem(BaseModel):
    url: str
    type: str
    reason: str
    json_ld: str
    complete: bool
    missing_fields: list[str] = Field(default_factory=list)
    rich_result_note: str = ""
    warning: str = ""


class SchemaSuggestSection(SectionState):
    items: list[SchemaItem] = Field(default_factory=list)
    items_total: int = 0
    notes: list[str] = Field(default_factory=list)


class AgenticRow(BaseModel):
    id: str
    label: str
    status: Literal["pass", "fail", "unknown", "na"]
    detail: str = ""


class AgenticSection(SectionState):
    rows: list[AgenticRow] = Field(default_factory=list)
    fraction: str = ""
    note: str = ""
    notes: list[str] = Field(default_factory=list)


class GlanceSection(SectionState):
    """One-look summary assembled from the other sections; shows no new data."""

    items: list[KV] = Field(default_factory=list)


class QualityCheck(BaseModel):
    id: str
    status: Literal["PASS", "WARN", "FAIL"]
    detail: str = ""


class Quality(BaseModel):
    status: Literal["PASS", "WARN", "FAIL"] = "PASS"
    checks: list[QualityCheck] = Field(default_factory=list)


class ExecutiveSummary(BaseModel):
    counts: dict[str, int]
    top_finding_ids: list[str]
    quick_win_recommendation_ids: list[str]
    zero_findings: bool = False


# --------------------------------------------------------------------------- history and competitors


class HistoryRow(BaseModel):
    id: str
    url: str
    created_at: datetime | None = None
    mode: str
    status: str
    overall: float | None = None
    coverage_pct: float | None = None
    is_current: bool = False


class HistoryChange(BaseModel):
    label: str
    old: str = "—"
    new: str = "—"
    delta: str = "—"
    direction: Literal["better", "worse", "same", "none"] = "none"


class HistoryFinding(BaseModel):
    rule_id: str
    severity: str
    title: str
    url: str | None = None


class HistorySection(SectionState):
    rows: list[HistoryRow] = Field(default_factory=list)
    rows_total: int = 0
    previous_id: str | None = None
    previous_at: datetime | None = None
    warning_text: str | None = None
    scores: list[HistoryChange] = Field(default_factory=list)
    metrics: list[HistoryChange] = Field(default_factory=list)
    new_findings: list[HistoryFinding] = Field(default_factory=list)
    resolved_findings: list[HistoryFinding] = Field(default_factory=list)
    new_total: int = 0
    resolved_total: int = 0
    persisting: int = 0
    note: str = ""
    counts_toward_overall: Literal[False] = False


class CompetitorColumn(BaseModel):
    host: str
    url: str
    is_you: bool = False
    note: str | None = None  # why there are no numbers (unreachable, rejected, ...)
    values: list[str] = Field(default_factory=list)  # one per ``CompetitorsSection.metrics``


class CompetitorsSection(SectionState):
    metrics: list[str] = Field(default_factory=list)
    columns: list[CompetitorColumn] = Field(default_factory=list)
    skipped: list[KV] = Field(default_factory=list)
    note: str = ""
    counts_toward_overall: Literal[False] = False


# --------------------------------------------------------------------------- local, sitemap, authority


class LocalPhoneRow(BaseModel):
    number: str
    formats: str = ""
    sources: str = ""
    pages: int = 0


class LocalAddressRow(BaseModel):
    text: str
    source: str = ""
    pages: int = 0


class LocalIssueRow(BaseModel):
    label: str
    values: str = ""
    pages: str = ""


class LocalSection(SectionState):
    facts: list[KV] = Field(default_factory=list)
    phones: list[LocalPhoneRow] = Field(default_factory=list)
    addresses: list[LocalAddressRow] = Field(default_factory=list)
    issues: list[LocalIssueRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    note: str = ""


class SitemapGenSection(SectionState):
    facts: list[KV] = Field(default_factory=list)
    xml_preview: str = ""
    urls_total: int = 0
    urls_shown: int = 0
    missing: list[str] = Field(default_factory=list)
    missing_total: int = 0
    not_indexable: list[KV] = Field(default_factory=list)
    not_indexable_total: int = 0
    excluded: list[KV] = Field(default_factory=list)
    excluded_total: int = 0
    note: str = ""
    counts_toward_overall: Literal[False] = False


class AuthoritySection(SectionState):
    items: list[KV] = Field(default_factory=list)
    note: str = ""
    counts_toward_overall: Literal[False] = False


# --------------------------------------------------------------------------- rankings


class RankingRow(BaseModel):
    keyword: str
    matched_query: str | None = None
    impressions: int = 0
    clicks: int = 0
    ctr_pct: float = 0.0
    position: float | None = None
    top_page: str | None = None
    status: Literal["ok", "no-data", "error"] = "no-data"
    status_text: str = ""


class RankingQueryRow(BaseModel):
    query: str
    impressions: int = 0
    clicks: int = 0
    ctr_pct: float = 0.0
    position: float = 0.0


class RankingsSection(SectionState):
    """Keyword rankings from Search Console only (the owner's property). Informational, no score."""

    period_days: int = 28
    keywords_requested: int = 0
    rows: list[RankingRow] = Field(default_factory=list)
    opportunities: list[RankingQueryRow] = Field(default_factory=list)
    low_ctr: list[RankingQueryRow] = Field(default_factory=list)
    keywords_note: str = ""
    list_error: str = ""
    note: str = ""
    rules_note: str = ""
    counts_toward_overall: Literal[False] = False


class AuditReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    audit: AuditInfo
    versions: Versions
    crawl: CrawlInfo
    scores: Scores
    scope_note: ScopeNote | None = None
    trend: Trend | None = None
    summary: ExecutiveSummary
    findings: list[FindingReport] = Field(default_factory=list)
    not_checked: list[NotCheckedReport] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    action_plan: ActionPlan
    pages: list[PageSummary] = Field(default_factory=list)
    links: LinksSection = Field(default_factory=LinksSection)
    performance: PerfSection = Field(default_factory=PerfSection)
    images: ImagesSection = Field(default_factory=ImagesSection)
    indexability: IndexabilitySection = Field(default_factory=IndexabilitySection)
    visibility: VisibilitySection = Field(default_factory=VisibilitySection)
    gsc: GscSection = Field(default_factory=GscSection)
    field: FieldSection = Field(default_factory=FieldSection)
    glance: GlanceSection = Field(default_factory=GlanceSection)
    techstack: TechSection = Field(default_factory=TechSection)
    site_setup: SiteSetupSection = Field(default_factory=SiteSetupSection)
    previews: PreviewsSection = Field(default_factory=PreviewsSection)
    screenshots: ScreenshotsSection = Field(default_factory=ScreenshotsSection)
    readability: ReadabilitySection = Field(default_factory=ReadabilitySection)
    hreflang: HreflangSection = Field(default_factory=HreflangSection)
    schema_suggestions: SchemaSuggestSection = Field(default_factory=SchemaSuggestSection)
    agentic: AgenticSection = Field(default_factory=AgenticSection)
    history: HistorySection = Field(default_factory=HistorySection)
    competitors: CompetitorsSection = Field(default_factory=CompetitorsSection)
    local: LocalSection = Field(default_factory=LocalSection)
    sitemap_gen: SitemapGenSection = Field(default_factory=SitemapGenSection)
    authority: AuthoritySection = Field(default_factory=AuthoritySection)
    rankings: RankingsSection = Field(default_factory=RankingsSection)
    data_sources: list[DataSource] = Field(default_factory=list)
    limitations: list[Limitation] = Field(default_factory=list)
    methodology: Methodology
    usage: dict[str, Any] = Field(default_factory=dict)
    quality: Quality = Field(default_factory=Quality)

    def finding(self, finding_id: str) -> FindingReport | None:
        return next((f for f in self.findings if f.id == finding_id), None)


class FindingList(BaseModel):
    """Findings of a report, filtered and sorted by ``report.findings``."""

    items: list[FindingReport] = Field(default_factory=list)


# --------------------------------------------------------------------------- comparison of two audits


class Delta(BaseModel):
    old: float | None = None
    new: float | None = None
    delta: float | None = None


class CategoryDelta(Delta):
    category: str


class MetricDelta(Delta):
    key: str  # stable code: lcp_ms, ttfb_ms, total_bytes, requests, ...
    improved: bool | None = None  # None: no direction (or unchanged / unknown)


class FindingRef(BaseModel):
    rule_id: str
    severity: str
    category: str
    url: str | None = None


class Comparison(BaseModel):
    old_id: str
    new_id: str
    comparable: bool
    warnings: list[str] = Field(default_factory=list)  # score-version-differs | mode-differs
    overall: Delta | None = None
    core_overall: Delta | None = None
    categories: list[CategoryDelta] = Field(default_factory=list)
    new_findings: list[FindingRef] = Field(default_factory=list)
    resolved_findings: list[FindingRef] = Field(default_factory=list)
    persisting_count: int = 0
    new_total: int = 0
    resolved_total: int = 0
    metrics: list[MetricDelta] = Field(default_factory=list)


class AuditSummary(BaseModel):
    """One other audit of the same site, as the history section lists it."""

    id: str
    target_url: str
    mode: AuditMode
    status: AuditStatus
    overall: float | None = None
    coverage_pct: float | None = None
    created_at: datetime
