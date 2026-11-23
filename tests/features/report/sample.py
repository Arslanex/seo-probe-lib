"""make_sample_record: realistic Audits without a pipeline (test support only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from seoprobe.core.models import AuditMode, Category, Finding, Layer, NotChecked, Severity
from seoprobe.core.rules.scoring import CategoryScore, CheckResult, Gate, PageRecord, Penalty, Scores
from seoprobe.features.audit import Audit, AuditStatus, Stage, StageRecord, StageStatus
from seoprobe.features.crawl import IndexabilityRow, LinkRecord, LinkSummary, PageLinkStats
from seoprobe.features.offsite import VisibilitySignals
from seoprobe.features.performance import FieldData, FieldMetric, ImageRecord, PagePerf
from seoprobe.features.search import (
    GscData,
    GscInspection,
    GscRow,
    GscSitemap,
    KeywordRankings,
    KeywordRankRow,
    RankingQuery,
)

Variant = Literal["gated", "clean", "insufficient", "partial", "failed", "page", "canceled", "no-deep-analysis"]

# id -> (category, severity, effort, impact, en, tr, rec_en, rec_tr)
_SAMPLE: dict[str, tuple[Category, Severity, str, str, str, str, str, str]] = {
    "noindex": (
        Category.TECHNICAL,
        Severity.CRITICAL,
        "S",
        "high",
        "A robots meta tag asks search engines not to index this page.",
        "Bir robots meta etiketi arama motorlarından bu sayfayı indekslememelerini istiyor.",
        "Remove the noindex directive if the page should appear in search results.",
        "Sayfa arama sonuçlarında görünmeliyse noindex yönergesini kaldırın.",
    ),
    "canonical-points-elsewhere": (
        Category.TECHNICAL,
        Severity.HIGH,
        "S",
        "high",
        "The canonical names a different URL, so this page asks to be replaced by that URL.",
        "Canonical başka bir URL'yi gösteriyor; bu sayfa o URL ile değiştirilmesini istiyor.",
        "Point the canonical at the page itself unless the redirect is intended.",
        "Yönlendirme bilinçli değilse canonical'ı sayfanın kendisine yöneltin.",
    ),
    "mixed-content": (
        Category.TECHNICAL,
        Severity.HIGH,
        "M",
        "medium",
        "This HTTPS page loads resources over plain HTTP.",
        "Bu HTTPS sayfası kaynakları düz HTTP ile yüklüyor.",
        "Serve every resource over HTTPS.",
        "Tüm kaynakları HTTPS ile sunun.",
    ),
    "hsts-missing": (
        Category.TECHNICAL,
        Severity.MEDIUM,
        "S",
        "medium",
        "The Strict-Transport-Security header is missing.",
        "Strict-Transport-Security başlığı yok.",
        "Add an HSTS header with a long max-age.",
        "Uzun max-age ile bir HSTS başlığı ekleyin.",
    ),
    "missing-meta-description": (
        Category.ONPAGE,
        Severity.MEDIUM,
        "S",
        "medium",
        "The page has no meta description.",
        "Sayfanın meta açıklaması yok.",
        "Write a unique meta description for the page.",
        "Sayfa için benzersiz bir meta açıklaması yazın.",
    ),
    "title-too-long": (
        Category.ONPAGE,
        Severity.MEDIUM,
        "S",
        "low",
        "The title is longer than 60 characters and may be truncated in results.",
        "Başlık 60 karakterden uzun ve sonuçlarda kısaltılabilir.",
        "Shorten the title.",
        "Başlığı kısaltın.",
    ),
    "multiple-h1": (
        Category.ONPAGE,
        Severity.LOW,
        "S",
        "low",
        "The page has more than one H1.",
        "Sayfada birden fazla H1 var.",
        "Check that the most prominent heading names the page.",
        "En belirgin başlığın sayfayı adlandırdığını kontrol edin.",
    ),
    "url-underscores": (
        Category.ONPAGE,
        Severity.LOW,
        "M",
        "low",
        "The URL path uses underscores.",
        "URL yolu alt çizgi kullanıyor.",
        "Prefer hyphens as word separators.",
        "Sözcük ayırıcı olarak tire tercih edin.",
    ),
    "thin-content": (
        Category.CONTENT,
        Severity.MEDIUM,
        "L",
        "medium",
        "The page has fewer than 200 words of visible text.",
        "Sayfada 200'den az görünür kelime var.",
        "Check that the page fully answers what a visitor came for.",
        "Sayfanın ziyaretçinin aradığını tam yanıtladığını kontrol edin.",
    ),
    "invalid-jsonld": (
        Category.SCHEMA,
        Severity.MEDIUM,
        "M",
        "medium",
        "A JSON-LD block is not valid JSON.",
        "Bir JSON-LD bloğu geçerli JSON değil.",
        "Fix the JSON syntax of the structured data block.",
        "Yapılandırılmış veri bloğunun JSON sözdizimini düzeltin.",
    ),
    "images-missing-alt": (
        Category.IMAGES,
        Severity.MEDIUM,
        "S",
        "medium",
        "Some images have no alt attribute.",
        "Bazı görsellerde alt özniteliği yok.",
        "Add descriptive alt text; use an empty alt for decorative images.",
        "Açıklayıcı alt metni ekleyin; dekoratif görsellerde boş alt kullanın.",
    ),
    "missing-og-image": (
        Category.ONPAGE,
        Severity.INFO,
        "S",
        "low",
        "The page has no og:image.",
        "Sayfada og:image yok.",
        "Add an og:image for social previews.",
        "Sosyal önizleme için og:image ekleyin.",
    ),
}

XSS_EVIDENCE = '<script>alert("xss1")</script>'
XSS_EVIDENCE_2 = '"><img src=x onerror=alert("xss2")>'


def _finding(cid: str, url: str | None, evidence: str | None, count: int | None = None) -> Finding:
    cat, sev, effort, impact, en, *_ = _SAMPLE[cid]
    return Finding(
        id=cid,
        category=cat,
        layer=Layer.B if cid in ("thin-content", "title-too-long", "url-underscores") else Layer.A,
        severity=sev,
        scope="site" if cid == "hsts-missing" else "page",
        url=url,
        message_key=f"finding.{cid}",
        message=en,
        evidence=evidence,
        count=count,
        heuristic=cid in ("thin-content", "title-too-long", "url-underscores", "multiple-h1"),
        recommendation_key=f"rec.{cid}",
        effort=effort,  # type: ignore[arg-type]
        impact=impact,  # type: ignore[arg-type]
    )


_NOW = datetime(2026, 11, 13, 10, 0, 0, tzinfo=UTC)


def make_sample_record(
    variant: Variant = "gated",
    *,
    audit_id: str = "aud_sample0001",
    xss: bool = False,
    mode: AuditMode | None = None,
    gsc: str | None = None,
    field: str | None = None,
) -> Audit:
    """
    A realistic Audit. ``xss=True`` puts markup sentinels into every page-derived field.

    ``gsc``: ok | not-connected | property-not-found | no-permission | error | not-configured;
    ``field``: ok | no-data | no-api-key | error (None: the stage never ran, the stored blob is absent).
    """
    base = "https://example.com"
    amode = mode or (AuditMode.PAGE if variant == "page" else AuditMode.SITE)
    pages = [
        PageRecord(
            id=f"p{i}",
            url=f"{base}/" if i == 0 else f"{base}/blog/post-{i}" if i < 7 else f"{base}/urun/{i}",
            final_url=None,
            status=200,
            sample_kind="core" if i < 4 else "exploration",
            template_key="/" if i == 0 else "/blog/*" if i < 7 else "/urun/*",
            html_bytes=40000 + i * 1000,
            ttfb_ms=180 + i * 10,
            rendered=i == 0,
            is_spa=False,
            title=(XSS_EVIDENCE if (xss and i == 1) else f"Page {i}"),
            page_score=60.0 + i,
        )
        for i in range(1 if amode == AuditMode.PAGE else 10)
    ]
    ev1 = XSS_EVIDENCE if xss else "noindex, nofollow"
    ev2 = XSS_EVIDENCE_2 if xss else "https://example.com/other"
    findings: list[Finding] = [
        _finding("canonical-points-elsewhere", f"{base}/blog/post-2", ev2),
        _finding("mixed-content", f"{base}/urun/8", "http://cdn.example.com/a.js", 2),
        _finding("hsts-missing", None, None),
        _finding(
            "title-too-long",
            f"{base}/blog/post-1",
            "A very long title that goes past the sixty character display heuristic",
        ),
        _finding(
            "title-too-long",
            f"{base}/blog/post-3",
            "Another very long title that goes past the sixty character display heuristic",
        ),
        _finding("missing-meta-description", f"{base}/blog/post-1", None),
        _finding("missing-meta-description", f"{base}/blog/post-2", None),
        _finding("missing-meta-description", f"{base}/urun/7", None),
        _finding("multiple-h1", f"{base}/blog/post-4", "3 h1 elements", 3),
        _finding("url-underscores", f"{base}/urun/9", "/urun/9_a"),
        _finding("thin-content", f"{base}/urun/8", "112 words", 112),
        _finding("invalid-jsonld", f"{base}/", "Expecting ',' delimiter"),
        _finding("images-missing-alt", f"{base}/blog/post-5", "4 of 9 images", 4),
        _finding("missing-og-image", f"{base}/", None),
    ]
    gate = None
    overall: float | None = 71.0
    raw: float | None = 71.0
    coverage = 90.0
    insufficient = False
    if variant in ("gated", "canceled"):
        findings.insert(0, _finding("noindex", f"{base}/", ev1))
        gate = Gate(check_id="noindex", cap=40, reason="A noindex directive was found on the entry page")
        overall = 40.0
    if variant == "clean":
        findings = []
        overall, raw = 96.0, 96.0
    if variant == "insufficient":
        overall, raw, coverage, insufficient = None, None, 40.0, True
    status = {
        "partial": AuditStatus.PARTIAL,
        "failed": AuditStatus.FAILED,
        "canceled": AuditStatus.CANCELED,
    }.get(variant, AuditStatus.COMPLETED)

    cats = _categories(variant, gate is not None)
    if amode == AuditMode.PAGE:
        cats = [c for c in cats if c.category not in (Category.PERFORMANCE, Category.AI_READINESS)]
    scores: Scores | None = Scores(
        score_version="mvp-0.2",
        overall=overall,
        raw_overall=raw,
        coverage_pct=coverage,
        insufficient_data=insufficient,
        gate=gate,
        confidence="medium",
        categories=cats,
        core_overall=overall,
        p25=58.0 if amode == AuditMode.SITE else None,
    )
    record = Audit(
        id=audit_id,
        mode=amode,
        target_url="example.com",
        final_origin=base,
        status=status,
        business_type="saas",
        lang="en",
        created_at=_NOW,
        finished_at=_NOW + timedelta(seconds=94) if status != AuditStatus.FAILED else _NOW + timedelta(seconds=3),
        coverage={
            "pages_discovered": 1840,
            "pages_planned": 25 if variant != "partial" else 25,
            "pages_fetched": 10 if variant != "partial" else 6,
            "pages_rendered": 1,
            "checks_run": 47,
            "sample_seed": "a41c9e",
            "templates": [
                {"key": "/blog/*", "discovered": 1200, "sampled": 6},
                {"key": "/urun/*", "discovered": 600, "sampled": 3},
            ],
        },
        budget={"max_pages": 25, "requested_pages": 50, "tier": "unverified"},
        cost={"fetch_requests": 31},
        stages=[StageRecord(stage=s, status=StageStatus.DONE) for s in Stage],
        pages=pages,
        findings=findings,
        not_checked=[
            NotChecked(check_id="title-not-reflected-in-content", reason_code="language-unsupported"),
            NotChecked(check_id="noindex-header", reason_code="headers-unavailable", url=f"{base}/urun/9"),
            NotChecked(check_id="core-web-vitals", reason_code="psi-unavailable"),
        ],
        scores=scores,
        **_deep(variant, amode, base, xss),
    )
    if gsc is not None:
        record.gsc = sample_gsc(gsc, base, xss=xss)
    if field is not None:
        record.field = sample_field(field)
    if variant == "failed":
        record.findings, record.scores, record.pages, record.coverage = [], None, [], {}
        record.links, record.perf, record.indexability, record.visibility = None, [], [], None
        record.not_checked = []
        record.error = "unreachable: dns failure for internal-host"
    if variant == "partial":
        record.not_checked.append(NotChecked(check_id="js_gap", reason_code="render-failed"))
    return record


def sample_gsc(kind: str, base: str = "https://example.com", *, xss: bool = False) -> GscData:
    if kind != "ok":
        return GscData(
            status=kind,  # type: ignore[arg-type]
            error="http-500: backend failure" if kind == "error" else None,
            property=f"sc-domain:{base[8:]}" if kind == "no-permission" else None,
        )
    q = XSS_EVIDENCE if xss else "example widgets"
    return GscData(
        status="ok",
        account="owner@example.com",
        property="sc-domain:example.com",
        permission="siteOwner",
        period_days=28,
        totals={"clicks": 1234.0, "impressions": 45678.0, "ctr": 0.027, "position": 14.3},
        top_queries=[
            GscRow(key=q, clicks=120, impressions=3400, ctr=0.035, position=6.2),
            GscRow(key="buy widgets", clicks=40, impressions=2100, ctr=0.019, position=11.0),
        ],
        top_pages=[GscRow(key=f"{base}/", clicks=300, impressions=9000, ctr=0.033, position=8.1)],
        inspections=[
            GscInspection(
                url=f"{base}/",
                verdict="PASS",
                coverage_state="Submitted and indexed",
                google_canonical=f"{base}/",
                user_canonical=f"{base}/",
                last_crawl_time="2026-11-10T08:15:00Z",
                crawled_as="MOBILE",
                indexed=True,
            ),
            GscInspection(
                url=f"{base}/blog/post-2",
                verdict="NEUTRAL",
                coverage_state="Discovered - currently not indexed",
                google_canonical=f"{base}/blog/other",
                user_canonical=XSS_EVIDENCE_2 if xss else f"{base}/blog/post-2",
                indexed=False,
            ),
            GscInspection(url=f"{base}/urun/7", error="quota-exceeded: daily limit"),
        ],
        sitemaps=[
            GscSitemap(
                path=f"{base}/sitemap.xml",
                last_submitted="2026-10-01T00:00:00Z",
                last_downloaded="2026-11-09T00:00:00Z",
                errors=1,
                warnings=2,
            )
        ],
        rankings=KeywordRankings(
            period_days=28,
            keywords_requested=3,
            rows=[
                KeywordRankRow(
                    keyword=XSS_EVIDENCE if xss else "widgets",
                    matched_query=XSS_EVIDENCE_2 if xss else "buy widgets",
                    impressions=2100,
                    clicks=40,
                    ctr=0.019,
                    position=11.0,
                    top_page=f"{base}/",
                    status="ok",
                ),
                KeywordRankRow(keyword="rare thing", status="no-data"),
                KeywordRankRow(keyword="limited", status="error", error="rate-limited"),
            ],
            opportunities=[RankingQuery(query=q, impressions=900, clicks=9, ctr=0.01, position=8.4)],
            low_ctr=[RankingQuery(query="buy widgets", impressions=2100, clicks=10, ctr=0.005, position=7.0)],
        ),
    )


def sample_field(kind: str) -> FieldData:
    if kind != "ok":
        return FieldData(error={"no-data": "no-data", "no-api-key": "no-api-key"}.get(kind, "http-500"))
    return FieldData(
        scope="origin",
        form_factor="PHONE",
        period={"first": "2026-10-10", "last": "2026-11-06"},
        metrics=[
            FieldMetric(
                metric="lcp",
                p75=3100.0,
                rating="needs-improvement",
                good_share=0.55,
                needs_improvement_share=0.3,
                poor_share=0.15,
            ),
            FieldMetric(
                metric="inp",
                p75=180.0,
                rating="good",
                good_share=0.8,
                needs_improvement_share=0.15,
                poor_share=0.05,
            ),
            FieldMetric(
                metric="cls",
                p75=0.31,
                rating="poor",
                good_share=0.5,
                needs_improvement_share=0.1,
                poor_share=0.4,
            ),
            FieldMetric(metric="ttfb", p75=620.0, rating="good"),
        ],
    )


def _check(cid: str, sev: Severity, weight: float, ratio: float, aff: int, assessed: int) -> CheckResult:
    status = "pass" if ratio >= 1.0 else ("warn" if sev in (Severity.LOW, Severity.INFO) else "fail")
    return CheckResult(
        check_id=cid,
        severity=sev,
        weight=weight,
        pass_ratio=ratio,
        pages_affected=aff,
        pages_assessed=assessed,
        status=status,  # type: ignore[arg-type]
    )


def _cat(
    category: Category,
    weight: float,
    score: float,
    checks: list[CheckResult],
    penalties: list[Penalty] | None = None,
) -> CategoryScore:
    return CategoryScore(
        category=category,
        weight=weight,
        score=score,
        measured=True,
        penalties=penalties or [],
        checks=checks,
        checks_total=len(checks),
        checks_passed=sum(1 for c in checks if c.pass_ratio >= 1.0),
        checks_failed=sum(1 for c in checks if c.pass_ratio < 1.0),
    )


def _categories(variant: str, gated: bool) -> list[CategoryScore]:
    """Score v2 categories with the checks behind each score."""
    H, M, L, C = Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.CRITICAL
    tech_checks = [
        _check("noindex", C, 8, 0.9 if gated else 1.0, 1 if gated else 0, 10),
        _check("canonical-points-elsewhere", H, 4, 0.9, 1, 10),
        _check("mixed-content", H, 4, 0.9, 1, 10),
        _check("hsts-missing", M, 2, 0.0, 1, 1),
        _check("robots-blocks-site", C, 8, 1.0, 0, 1),
    ]
    tech_pen = [Penalty(check_id="canonical-points-elsewhere", severity=H, points=-5.0)]
    if gated:
        tech_pen.append(Penalty(check_id="noindex", severity=C, points=-11.0))
    cats = [
        _cat(Category.TECHNICAL, 20, 55.0, tech_checks, tech_pen),
        _cat(
            Category.ONPAGE,
            15,
            78.0,
            [
                _check("missing-title", H, 4, 1.0, 0, 10),
                _check("missing-meta-description", M, 2, 0.7, 3, 10),
                _check("title-too-long", M, 2, 0.8, 2, 10),
                _check("multiple-h1", L, 1, 0.9, 1, 10),
                _check("url-underscores", L, 1, 0.9, 1, 10),
            ],
            [Penalty(check_id="missing-meta-description", severity=M, points=-6.0)],
        ),
        _cat(
            Category.CONTENT,
            20,
            70.0,
            [
                _check("thin-content", M, 2, 0.9, 1, 10),
                _check("placeholder-text", H, 4, 1.0, 0, 10),
            ],
        ),
        _cat(
            Category.LINKS,
            10,
            72.0,
            [
                _check("broken-internal-link", H, 4, 0.8, 2, 10),
                _check("orphan-page", M, 2, 0.9, 1, 10),
                _check("deep-page", L, 1, 1.0, 0, 10),
                _check("internal-redirect-link", L, 1, 0.7, 3, 10),
            ],
            [Penalty(check_id="broken-internal-link", severity=H, points=-8.0)],
        ),
        _cat(
            Category.PERFORMANCE,
            12,
            74.0,
            [
                _check("cwv-lcp-needs-improvement", M, 2, 0.0, 1, 1),
                _check("cwv-cls-poor", H, 4, 1.0, 0, 1),
                _check("page-weight-heavy", M, 2, 1.0, 0, 2),
            ],
            [Penalty(check_id="cwv-lcp-needs-improvement", severity=M, points=-14.0)],
        ),
        _cat(Category.SCHEMA, 8, 90.0, [_check("invalid-jsonld", M, 2, 0.9, 1, 10)]),
        _cat(
            Category.AI_READINESS,
            8,
            82.0,
            [_check("ai-search-bots-blocked", M, 2, 1.0, 0, 1)],
        ),
        _cat(
            Category.IMAGES,
            7,
            85.0,
            [
                _check("images-missing-alt", M, 2, 0.9, 1, 10),
                _check("image-too-large", M, 2, 0.7, 3, 10),
            ],
        ),
    ]
    if variant == "clean":
        cats = [
            c.model_copy(
                update={
                    "score": 96.0,
                    "penalties": [],
                    "checks": [
                        k.model_copy(update={"pass_ratio": 1.0, "pages_affected": 0, "status": "pass"})
                        for k in c.checks
                    ],
                    "checks_passed": c.checks_total,
                    "checks_failed": 0,
                }
            )
            for c in cats
        ]
    if variant in ("partial", "no-deep-analysis"):
        reason = "psi-unavailable" if variant == "partial" else "lab-unavailable"
        cats = [
            CategoryScore(category=c.category, weight=c.weight, score=None, measured=False, reason=reason)
            if c.category == Category.PERFORMANCE
            else c
            for c in cats
        ]
    if variant == "no-deep-analysis":
        cats = [
            CategoryScore(
                category=c.category,
                weight=c.weight,
                score=None,
                measured=False,
                reason="analysis-not-run",
            )
            if c.category == Category.LINKS
            else c
            for c in cats
        ]
    return cats


def _deep(variant: str, amode: AuditMode, base: str, xss: bool) -> dict[str, Any]:
    """Deep-analysis data. ``variant='no-deep-analysis'`` leaves everything empty."""
    if variant in ("no-deep-analysis", "failed"):
        return {"links": None, "perf": [], "indexability": [], "visibility": None}
    bad_anchor = XSS_EVIDENCE if xss else "Eski kampanya"
    bad_target = f'{base}/x"><img src=x onerror=alert(3)>' if xss else f"{base}/eski-kampanya"
    links = [
        LinkRecord(
            source_url=f"{base}/",
            target_url=bad_target,
            anchor=bad_anchor,
            kind="internal",
            zone="main",
            target_status=404,
            checked=True,
        ),
        LinkRecord(
            source_url=f"{base}/blog/post-2",
            target_url="https://old-partner.example/",
            anchor="Partner",
            kind="external",
            zone="footer",
            error="timeout",
            checked=True,
        ),
        LinkRecord(
            source_url=f"{base}/",
            target_url=f"{base}/urun/7",
            anchor="Ürün 7",
            kind="internal",
            zone="nav",
            target_status=301,
            final_url=f"{base}/urun/7-yeni",
            redirect_hops=1,
            checked=True,
        ),
        LinkRecord(
            source_url=f"{base}/",
            target_url=f"{base}/blog/post-1",
            anchor="Blog",
            kind="internal",
            zone="nav",
            target_status=200,
            checked=True,
        ),
        LinkRecord(
            source_url=f"{base}/blog/post-1",
            target_url="https://twitter.com/example",
            anchor="Twitter",
            kind="external",
            rel=["nofollow"],
            zone="footer",
            target_status=200,
            checked=True,
        ),
        LinkRecord(
            source_url=f"{base}/blog/post-3",
            target_url="https://docs.example.org/guide",
            anchor="Rehber",
            kind="external",
            zone="main",
        ),
    ]
    stats = [
        PageLinkStats(
            url=f"{base}/",
            outlinks_internal=12,
            outlinks_external=2,
            inlinks=9,
            depth=0,
            in_sitemap=True,
        ),
        PageLinkStats(
            url=f"{base}/blog/post-1",
            outlinks_internal=6,
            outlinks_external=1,
            inlinks=3,
            depth=1,
            in_sitemap=True,
        ),
        PageLinkStats(
            url=f"{base}/urun/7",
            outlinks_internal=4,
            outlinks_external=0,
            inlinks=1,
            depth=2,
            in_sitemap=False,
        ),
        PageLinkStats(
            url=f"{base}/eski-kampanya",
            outlinks_internal=0,
            outlinks_external=0,
            inlinks=0,
            depth=None,
            in_sitemap=True,
        ),
    ]
    lsum = LinkSummary(
        internal_total=312,
        external_total=48,
        internal_unique=140,
        external_unique=21,
        external_domains=[
            {"domain": "twitter.com", "count": 12},
            {"domain": "docs.example.org", "count": 7},
            {"domain": "old-partner.example", "count": 3},
        ],
        checked=60,
        skipped=5,
        broken=2,
        redirecting=3,
        nofollow=4,
        max_depth=4,
        orphans=[f"{base}/eski-kampanya"],
        pages=stats,
        links=links,
    )
    src = f"{base}/media/hero.png"
    imgs = [
        ImageRecord(
            page_url=f"{base}/",
            src=(f'{base}/a.png"><script>alert(2)</script>' if xss else src),
            served_format="png",
            bytes=820_000,
            natural_w=2400,
            natural_h=1200,
            displayed_w=800,
            displayed_h=400,
            above_fold=True,
            category="hero",
            issues=["image-too-large", "image-oversized-for-display", "image-legacy-format"],
        ),
        ImageRecord(
            page_url=f"{base}/",
            src=f"{base}/media/logo.webp",
            served_format="webp",
            bytes=14_000,
            natural_w=200,
            natural_h=60,
            displayed_w=200,
            displayed_h=60,
            category="thumbnail",
        ),
        ImageRecord(
            page_url=f"{base}/blog/post-1",
            src=f"{base}/media/photo.jpg",
            served_format="jpeg",
            bytes=310_000,
            natural_w=1600,
            natural_h=900,
            displayed_w=720,
            displayed_h=405,
            has_alt=False,
            lazy=True,
            issues=["images-missing-alt", "image-too-large"],
        ),
    ]
    perf = [
        PagePerf(
            url=f"{base}/",
            device="desktop",
            ttfb_ms=210,
            fcp_ms=1100,
            lcp_ms=3100,
            cls=0.04,
            tbt_ms=320,
            dom_content_loaded_ms=1400,
            load_ms=2900,
            total_requests=64,
            total_bytes=2_400_000,
            html_bytes=45_000,
            by_type={
                "script": {"count": 18, "bytes": 900_000},
                "image": {"count": 22, "bytes": 1_100_000},
                "stylesheet": {"count": 4, "bytes": 120_000},
            },
            third_party=[
                {"host": "www.googletagmanager.com", "requests": 6, "bytes": 310_000},
                {"host": "cdn.example-ads.com", "requests": 9, "bytes": 150_000},
            ],
            render_blocking=3,
            uncompressed_text=[f"{base}/static/app.js"],
            no_cache_static=[f"{base}/static/logo.svg"],
            images=imgs,
        ),
        PagePerf(
            url=f"{base}/",
            device="mobile",
            ttfb_ms=260,
            fcp_ms=2300,
            lcp_ms=4600,
            cls=0.31,
            tbt_ms=710,
            dom_content_loaded_ms=2600,
            load_ms=5200,
            total_requests=61,
            total_bytes=2_300_000,
            html_bytes=45_000,
            mobile={
                "horizontal_overflow": True,
                "small_font_share": 0.18,
                "small_tap_targets": 7,
                "viewport_meta": True,
            },
            render_blocking=3,
        ),
        PagePerf(url=f"{base}/blog/post-1", device="mobile", error="render-failed: timeout"),
    ]
    index = [
        IndexabilityRow(
            url=f"{base}/",
            status=200,
            indexable=variant not in ("gated", "canceled"),
            reasons=[] if variant not in ("gated", "canceled") else ["noindex"],
            canonical=f"{base}/",
            in_sitemap=True,
            inlinks=9,
            depth=0,
        ),
        IndexabilityRow(
            url=f"{base}/blog/post-2",
            status=200,
            indexable=False,
            reasons=["canonical-elsewhere"],
            canonical=f"{base}/other",
            in_sitemap=True,
            inlinks=2,
            depth=1,
        ),
        IndexabilityRow(
            url=f"{base}/eski-kampanya",
            status=404,
            indexable=False,
            reasons=["status-4xx"],
            in_sitemap=True,
            inlinks=0,
            depth=None,
        ),
    ]
    if variant == "partial":
        vis = VisibilitySignals(
            domain="example.com",
            rdap={"error": "rdap-unsupported-tld"},
            wayback={"error": "timeout"},
            common_crawl={"error": "unreachable"},
            notes=["some-unknown-note"],
        )
    else:
        vis = VisibilitySignals(
            domain="example.com",
            rdap={
                "created": "2026-08-01",
                "age_days": 104,
                "registrar": (XSS_EVIDENCE if xss else "Example Registrar Inc."),
            },
            wayback={"first_capture": "2026-08-05", "last_capture": "2026-11-01", "captures": 7},
            common_crawl={"collection": "CC-MAIN-2026-40", "present": False, "pages_seen": 0},
            notes=["domain-under-180-days", "common-crawl-absent"],
        )
    return {
        "links": lsum,
        "perf": [] if (variant in ("partial",) or amode == AuditMode.PAGE) else perf,
        "indexability": index,
        "visibility": vis,
    }
