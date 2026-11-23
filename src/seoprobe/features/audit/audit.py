"""
The audit operations: one page or a sampled site through the whole pipeline, and a rerun.

A target that cannot be audited (invalid or blocked URL, entry page unreachable) is not an error
result: like the source, the operation returns an ``Audit`` with ``status=failed``, ``error``
("<code>: <detail>") and ``error_code``. Error results are left for programming errors and the
operation's own timeout.
"""

from __future__ import annotations

import zlib
from datetime import UTC, datetime

from seoprobe.core.errors import ErrorCode, OperationError
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import AuditMode, Requirements

from .models import Audit, AuditOptions, AuditStatus
from .pipeline import run_audit

PAGE_TIMEOUT_S = 600.0
SITE_TIMEOUT_S = 1800.0
MAX_URL_LENGTH = 2048
MAX_EXTRA_URLS = 20
MAX_KEYWORDS, MAX_KEYWORD_CHARS = 20, 80
MAX_COMPETITORS, MAX_COMPETITOR_CHARS = 5, 300


def normalize_options(
    *,
    max_pages: int | None = None,
    extra_urls: list[str] | None = None,
    keywords: list[str] | None = None,
    competitors: list[str] | None = None,
    seed: int | None = None,
    link_check_budget_s: float | None = None,
    visibility_budget_s: float | None = None,
    account: str = "default",
) -> AuditOptions:
    """The caller's options, limited the way the source service limited them."""
    return AuditOptions(
        max_pages=max_pages,
        extra_urls=(extra_urls or [])[:MAX_EXTRA_URLS],
        keywords=[k.strip()[:MAX_KEYWORD_CHARS] for k in (keywords or []) if k.strip()][:MAX_KEYWORDS],
        competitors=[c.strip()[:MAX_COMPETITOR_CHARS] for c in (competitors or []) if c.strip()][:MAX_COMPETITORS],
        seed=seed,
        link_check_budget_s=link_check_budget_s,
        visibility_budget_s=visibility_budget_s,
        account=account,
    )


async def _run(ctx: Context, url: str, mode: AuditMode, options: AuditOptions) -> Outcome[Audit]:
    url = (url or "").strip()
    if not url or len(url) > MAX_URL_LENGTH:
        ctx.fail(ErrorCode.INVALID_URL, reason="empty or longer than 2048 characters")
    created_at = datetime.now(UTC)
    seed = options.seed if options.seed is not None else zlib.crc32(f"{url}|{created_at.isoformat()}".encode())
    audit = await run_audit(ctx, url, mode, options, seed, created_at)
    return Outcome(data=audit, partial=audit.status == AuditStatus.PARTIAL)


@operation("audit_page", output=Audit, schema_version=2, requires=Requirements(network=True), timeout_s=PAGE_TIMEOUT_S)
async def audit_page(
    ctx: Context,
    url: str,
    *,
    keywords: list[str] | None = None,
    competitors: list[str] | None = None,
    extra_urls: list[str] | None = None,
    seed: int | None = None,
    link_check_budget_s: float | None = None,
    visibility_budget_s: float | None = None,
    account: str = "default",
) -> Outcome[Audit]:
    """
    Audit one page with every applicable analysis, then score it.

    ``link_check_budget_s`` / ``visibility_budget_s``: how long the link-health probes (see
    ``links``) and the public lookups (see ``visibility``) may take. A caller who prefers a quick
    answer sets a few seconds, one who can wait passes ``math.inf``; what a budget cuts short is
    reported as ``not_checked`` (``time-budget``).

    ``account``: the token-store account whose Search Console data the audit reads (see
    ``gsc.auth_url``); kept in ``options``, so ``rerun`` reads the same one.
    """
    options = normalize_options(
        keywords=keywords,
        competitors=competitors,
        extra_urls=extra_urls,
        seed=seed,
        link_check_budget_s=link_check_budget_s,
        visibility_budget_s=visibility_budget_s,
        account=account,
    )
    return await _run(ctx, url, AuditMode.PAGE, options)


@operation("audit_site", output=Audit, schema_version=2, requires=Requirements(network=True), timeout_s=SITE_TIMEOUT_S)
async def audit_site(
    ctx: Context,
    url: str,
    *,
    max_pages: int | None = None,
    extra_urls: list[str] | None = None,
    keywords: list[str] | None = None,
    competitors: list[str] | None = None,
    seed: int | None = None,
    link_check_budget_s: float | None = None,
    visibility_budget_s: float | None = None,
    account: str = "default",
) -> Outcome[Audit]:
    """
    Audit a site: discover its pages, audit a stratified seeded sample, then score it.

    ``seed`` fixes the sample (default: derived from the URL and the start time).
    ``link_check_budget_s`` / ``visibility_budget_s`` / ``account``: as in ``audit_page``.
    """
    options = normalize_options(
        max_pages=max_pages,
        extra_urls=extra_urls,
        keywords=keywords,
        competitors=competitors,
        seed=seed,
        link_check_budget_s=link_check_budget_s,
        visibility_budget_s=visibility_budget_s,
        account=account,
    )
    return await _run(ctx, url, AuditMode.SITE, options)


@operation("rerun", output=Audit, schema_version=2, requires=Requirements(network=True), timeout_s=SITE_TIMEOUT_S)
async def rerun(ctx: Context, audit: Audit) -> Outcome[Audit]:
    """A fresh audit with the same URL, mode and options as ``audit``."""
    o = audit.options
    if audit.mode == AuditMode.SITE:
        res = await audit_site(
            audit.target_url,
            max_pages=o.max_pages,
            extra_urls=o.extra_urls,
            keywords=o.keywords,
            competitors=o.competitors,
            seed=o.seed,
            link_check_budget_s=o.link_check_budget_s,
            visibility_budget_s=o.visibility_budget_s,
            account=o.account,
        )
    else:
        res = await audit_page(
            audit.target_url,
            keywords=o.keywords,
            competitors=o.competitors,
            extra_urls=o.extra_urls,
            seed=o.seed,
            link_check_budget_s=o.link_check_budget_s,
            visibility_budget_s=o.visibility_budget_s,
            account=o.account,
        )
    if res.data is None:
        err = res.error
        raise OperationError(err.code if err else ErrorCode.INTERNAL_ERROR, details=err.details if err else None)
    return Outcome(data=res.data, partial=res.data.status == AuditStatus.PARTIAL)
