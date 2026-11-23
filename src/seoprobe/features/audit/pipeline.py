"""
Audit orchestration: a deterministic state machine over the stages.

No LLM decides anything here. Every stage calls the public operations of the features; the
audit's own state lives in memory and is turned into an ``Audit`` at the end, also when a stage
failed (a failed audit keeps what was produced up to that point).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from urllib.parse import urlparse

from seoprobe.core.errors import ErrorCode, fetch_error_code
from seoprobe.core.feature import Context
from seoprobe.core.models import AuditMode, Category, Finding, NotChecked
from seoprobe.core.net.safety import URLSafetyError, check_url
from seoprobe.core.page import Page
from seoprobe.core.parse.text import new_id
from seoprobe.core.parse.urls import normalize_url, origin_of
from seoprobe.core.result import Result
from seoprobe.core.rules import catalog
from seoprobe.core.rules.scoring import SCORE_VERSION, GateInputs, PageRecord, Scores, compute_scores, page_score
from seoprobe.features.content import schema
from seoprobe.features.crawl import CrawlReport, IndexabilityRow, LinkSummary, crawl, duplicates, indexability, links
from seoprobe.features.offsite import VisibilitySignals, visibility
from seoprobe.features.page import classify, onpage
from seoprobe.features.performance import FieldData, PagePerf, PageSpeedReport, field_data, lab_metrics, pagespeed
from seoprobe.features.render import render
from seoprobe.features.search import GscData, gsc
from seoprobe.features.site import RobotsReport, SitemapReport, robots, sitemap, transport

from .models import Audit, AuditOptions, AuditStatus, Stage, StageRecord, StageStatus
from .modules import ModuleInputs, run_modules

log = logging.getLogger("seoprobe.audit")
T = TypeVar("T")

RENDER_BUDGET = 5


class _Fatal(Exception):
    def __init__(self, code: str, detail: str = "", error_code: ErrorCode | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.error_code = error_code


class _StepFailed(Exception):
    """A required operation returned an error result (a bug or a timeout, never a site property)."""

    def __init__(self, res: Result[Any]) -> None:
        super().__init__(res.operation)
        self.operation = res.operation
        self.error_code = res.error.code if res.error else ErrorCode.INTERNAL_ERROR


def _need(res: Result[T]) -> T:
    if not res.ok or res.data is None:
        raise _StepFailed(res)
    return res.data


@dataclass
class _State:
    url: str
    mode: AuditMode
    options: AuditOptions
    seed: int
    created_at: datetime
    entry_url: str = ""
    origin: str = ""
    entry_raw: Page | None = None  # the entry page as fetched
    pages: dict[str, Page] = field(default_factory=dict)  # url -> page (rendered where rendered)
    records: dict[str, PageRecord] = field(default_factory=dict)
    robots: RobotsReport | None = None
    sitemap: SitemapReport | None = None
    crawl: CrawlReport | None = None
    sitemap_urls: list[str] = field(default_factory=list)
    coverage: dict[str, Any] | None = None
    findings: list[Finding] = field(default_factory=list)
    not_checked: list[NotChecked] = field(default_factory=list)
    # What the audit record holds: the source recorded findings and not-checked entries at the
    # end of the measure and analyze stages (the field-data merge in analyze only affects scoring).
    stored_findings: list[Finding] = field(default_factory=list)
    stored_nc: list[NotChecked] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)
    stages: dict[Stage, StageRecord] = field(default_factory=dict)
    renderer: bool = False
    perf_all: list[PagePerf] | None = None
    perf_out: tuple[list[Finding], list[NotChecked]] = ([], [])
    lang: str | None = None
    business_type: str | None = None
    has_schema: bool = False
    has_images: bool = False
    links: LinkSummary | None = None
    indexability: list[IndexabilityRow] = field(default_factory=list)
    visibility: VisibilitySignals | None = None
    gsc: GscData | None = None
    crux: FieldData | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    provided: set[str] = field(default_factory=set)
    scores: Scores | None = None
    final_origin: str | None = None

    @property
    def entry(self) -> Page:
        return self.pages[self.entry_url]

    def parsed_pages(self) -> list[Page]:
        return [p for p in self.pages.values() if p.parsed is not None]


class Runner:
    def __init__(self, ctx: Context, state: _State) -> None:
        self.ctx = ctx
        self.s = state

    # ------------------------------------------------------------------ plumbing

    async def _emit(self, **data: Any) -> None:
        await self.ctx.emit("progress", **{k: getattr(v, "value", v) for k, v in data.items()})

    def _set_stage(self, stage: Stage, status: StageStatus, detail: str | None = None) -> None:
        now = datetime.now(UTC)
        rec = self.s.stages.get(stage)
        if rec is None:
            rec = self.s.stages[stage] = StageRecord(stage=stage, status=status, started_at=now)
        rec.status = status
        if status in (StageStatus.DONE, StageStatus.SKIPPED, StageStatus.FAILED):
            rec.finished_at = now
        if detail is not None:
            rec.detail = detail

    async def _stage(self, stage: Stage, fn: Callable[[], Awaitable[None]], *, skip: bool = False) -> None:
        if skip:
            self._set_stage(stage, StageStatus.SKIPPED)
            await self._emit(stage=stage, status=StageStatus.SKIPPED, message_key=f"stage.{stage.value}.skipped")
            return
        self._set_stage(stage, StageStatus.RUNNING)
        await self._emit(stage=stage, status=StageStatus.RUNNING, message_key=f"stage.{stage.value}.start")
        try:
            await fn()
        except _Fatal:
            self._set_stage(stage, StageStatus.FAILED)
            raise
        except Exception as exc:
            if not isinstance(exc, _StepFailed):
                self.ctx.log.exception("stage %s failed", stage.value)
            self._set_stage(stage, StageStatus.FAILED, type(exc).__name__)
            await self._emit(stage=stage, status=StageStatus.FAILED, message_key=f"stage.{stage.value}.failed")
            raise
        self._set_stage(stage, StageStatus.DONE)
        await self._emit(stage=stage, status=StageStatus.DONE, message_key=f"stage.{stage.value}.done")

    # ------------------------------------------------------------------ run

    async def run(self) -> Audit:
        t0 = time.monotonic()
        status = AuditStatus.RUNNING
        error: str | None = None
        error_code: ErrorCode | None = None
        cost: dict[str, Any] = {}
        await self._emit(status=status, message_key="audit.running")
        try:
            await self._pipeline()
            status = AuditStatus.PARTIAL if self.s.partial_reasons else AuditStatus.COMPLETED
            cost = {"wall_ms": int((time.monotonic() - t0) * 1000)}
            await self._emit(status=status, message_key=f"audit.{status.value}")
        except _Fatal as f:
            # only the stable code: the detail is client/exception text and may name internal addresses
            self.ctx.log.debug("audit of %s failed: %s %s", self.s.url, f.code, f.detail)
            status, error, error_code = AuditStatus.FAILED, f.code, f.error_code
            await self._emit(status=status, message_key=f"audit.failed.{f.code}")
        except _StepFailed as exc:  # a required operation failed: keep what exists, say so
            status = AuditStatus.FAILED
            self.ctx.log.debug("audit of %s failed in %s: %s", self.s.url, exc.operation, exc.error_code.value)
            error, error_code = exc.error_code.value, exc.error_code
            await self._emit(status=status, message_key=f"audit.failed.{exc.error_code.value}")
        except Exception:  # a bug, not a user error: keep what exists, say so
            self.ctx.log.exception("audit of %s crashed", self.s.url)
            status, error = AuditStatus.FAILED, "internal-error"
            error_code = ErrorCode.INTERNAL_ERROR
            await self._emit(status=status, message_key="audit.failed.internal-error")
        return self._audit(status, error, error_code, cost)

    async def _pipeline(self) -> None:
        page_mode = self.s.mode == AuditMode.PAGE
        await self._stage(Stage.VALIDATE, self._validate)
        await self._stage(Stage.FETCH_ENTRY, self._fetch_entry)
        await self._stage(Stage.ROBOTS, self._robots)
        await self._stage(Stage.DISCOVERY, self._discovery, skip=page_mode)
        await self._stage(Stage.SAMPLE, self._sample, skip=page_mode)
        await self._stage(Stage.FETCH_PAGES, self._fetch_pages, skip=page_mode)
        self.s.renderer = await self._renderer_available()
        await self._stage(Stage.RENDER, self._render, skip=not self.s.renderer)
        await self._stage(Stage.MEASURE, self._measure)
        await self._stage(Stage.ANALYZE, self._analyze)
        await self._stage(Stage.SCORE, self._score)

    async def _renderer_available(self) -> bool:
        """The source rendered only when rendering was on and a browser could be started."""
        from seoprobe.core.net.browser import playwright_installed

        if not self.ctx.config.render or not playwright_installed():
            return False
        renderer = await self.ctx.session.browser()
        return renderer.launch_error is None

    # ------------------------------------------------------------------ stages

    async def _validate(self) -> None:
        raw = self.s.url.strip()
        if "://" not in raw:
            raw = "https://" + raw
        url = normalize_url(raw)
        if not url:
            raise _Fatal("target-rejected", "invalid URL", ErrorCode.INVALID_URL)
        try:
            check_url(url)
        except URLSafetyError as exc:
            raise _Fatal("target-rejected", str(exc), exc.code) from exc
        self.s.entry_url = url

    async def _fetch_entry(self) -> None:
        url = self.s.entry_url
        fr = await self.ctx.http.fetch(url)
        # no fallback for dns-failed: http:// resolves the same host name
        if fr.error in ("tls-handshake-failed", "unreachable") and url.startswith("https://"):
            alt = await self.ctx.http.fetch("http://" + url[len("https://") :])
            if alt.error is None and alt.status is not None:
                fr = alt
                self.s.findings.append(self.ctx.finding("https-unavailable", url=url, evidence=fr.final_url))
        if fr.error or fr.status is None:
            code = fr.error or "unreachable"
            raise _Fatal(code, fr.error_detail or "", fetch_error_code(code))
        entry = Page.from_fetch(fr)
        if entry.url != url:  # the http fallback: the page stays keyed by the requested URL
            entry = entry.model_copy(update={"url": url})
        await entry.parse()
        self.s.entry_raw = entry
        self.s.origin = origin_of(fr.final_url)
        self.s.final_origin = self.s.origin
        self.s.pages = {url: entry}
        self.s.records = {
            url: PageRecord(
                id=new_id(),
                url=url,
                final_url=fr.final_url,
                status=fr.status,
                sample_kind="core",
                html_bytes=fr.html_bytes,
                ttfb_ms=fr.ttfb_ms,
            )
        }
        await self._emit(stage=Stage.FETCH_ENTRY, pages_fetched=1, pages_planned=1)

    async def _robots(self) -> None:
        res = await robots(self.s.entry_url, origin=self.s.origin)
        self.s.robots = _need(res)
        self.s.findings += res.findings
        self.s.not_checked += res.not_checked

    async def _discovery(self) -> None:
        assert self.s.robots is not None
        res = await sitemap(self.s.origin, robots_sitemaps=list(self.s.robots.sitemaps))
        self.s.sitemap = _need(res)
        self.s.findings += res.findings
        self.s.not_checked += res.not_checked

    async def _sample(self) -> None:
        """Candidates, sample and fetch: one ``crawl`` call with the robots and sitemap above."""
        res = await crawl(
            self.s.entry_url,
            entry=self.s.entry,
            robots=self.s.robots,
            sitemap=self.s.sitemap,
            max_pages=self.s.options.max_pages,
            extra_urls=self.s.options.extra_urls,
            seed=self.s.seed,
        )
        self.s.crawl = _need(res)
        self.s.findings += res.findings
        self.s.not_checked += res.not_checked

    async def _fetch_pages(self) -> None:
        report = self.s.crawl
        assert report is not None
        self.s.coverage = dict(report.coverage)
        self.s.sitemap_urls = list(report.sitemap_urls)
        self.s.records[self.s.entry_url].template_key = report.templates.get(self.s.entry_url)
        for p in report.pages[1:]:
            self.s.pages[p.url] = p
            self.s.records[p.url] = PageRecord(
                id=new_id(),
                url=p.url,
                final_url=p.final_url,
                status=p.status,
                sample_kind=p.sample_kind,
                template_key=report.templates.get(p.url),
                html_bytes=p.html_bytes,
                ttfb_ms=p.ttfb_ms,
                error=p.error,
            )
        if report.partial_sample:
            self.s.partial_reasons.append("partial-sample")

    async def _render(self) -> None:
        rendered = 0
        order = [self.s.entry_url] + [u for u in self.s.pages if u != self.s.entry_url]
        for url in order:
            page = self.s.pages[url]
            rec = self.s.records[url]
            if not page.html_ok or rendered >= RENDER_BUDGET:
                continue
            res = await render(page)
            if not res.ok or res.data is None:
                self.s.not_checked.append(
                    NotChecked(check_id="js-dependent-content", reason_code="render-failed", url=url)
                )
                continue
            rec.is_spa = res.data.is_spa
            if not res.data.is_spa:
                continue
            if not res.data.rendered or not res.data.html:
                self.s.not_checked += res.not_checked
                continue
            rendered += 1
            self.s.pages[url] = page.with_rendered(res.data.html)
            rec.rendered = True
            rec.js_gap = res.data.js_gap
        if self.s.coverage is not None:
            self.s.coverage["pages_rendered"] = rendered

        # lab performance: entry on desktop and mobile, a few more pages on desktop
        targets: list[str | Page | tuple[str, Literal["desktop", "mobile"]]] = [
            (self.s.entry_url, "desktop"),
            (self.s.entry_url, "mobile"),
        ]
        others = [u for u, r in self.s.records.items() if u != self.s.entry_url and r.status == 200]
        targets += [(u, "desktop") for u in others[:2]]
        lab = await lab_metrics(targets)
        self.s.perf_all = list(lab.data.items) if lab.ok and lab.data is not None else []
        self.s.perf_out = (list(lab.findings), list(lab.not_checked))

    async def _measure(self) -> None:
        findings, not_checked = self.s.findings, self.s.not_checked
        cls = _need(await classify(self.s.entry))
        self.s.lang, self.s.business_type = cls.lang, cls.business_type
        lang = cls.lang

        titles: dict[str, list[str]] = defaultdict(list)
        descs: dict[str, list[str]] = defaultdict(list)
        for url, page in self.s.pages.items():
            rec = self.s.records[url]
            if page.status is not None and page.status >= 500:
                findings.append(self.ctx.finding("page-status-5xx", url=url, evidence=str(page.status)))
            elif page.status is not None and page.status >= 400:
                findings.append(self.ctx.finding("page-status-4xx", url=url, evidence=str(page.status)))
            if len(page.redirect_chain) >= 2:
                findings.append(
                    self.ctx.finding(
                        "redirect-chain-long",
                        url=url,
                        evidence=" > ".join(h.url for h in page.redirect_chain)[:200],
                        count=len(page.redirect_chain),
                    )
                )
            if page.ttfb_ms is not None and page.ttfb_ms > 1800:
                findings.append(self.ctx.finding("slow-ttfb", url=url, evidence=f"{page.ttfb_ms} ms"))
            parsed = await page.parse()
            if parsed is None:
                continue
            op_res = await onpage(page, lang=lang)
            sch_res = await schema(page)
            _need(op_res)
            _need(sch_res)
            findings += [*op_res.findings, *sch_res.findings]
            not_checked += [*op_res.not_checked, *sch_res.not_checked]
            rec.title = parsed.title or None
            rec.word_count = parsed.word_count
            if parsed.title:
                titles[parsed.title.strip().lower()].append(url)
            if parsed.meta_description:
                descs[parsed.meta_description.strip().lower()].append(url)
            gap = rec.js_gap or {}
            if rec.rendered and gap.get("gap") is True:
                findings.append(
                    self.ctx.finding(
                        "js-dependent-content", url=url, evidence="; ".join(list(gap.get("reasons", [])))[:200]
                    )
                )
        for same, check in ((titles, "duplicate-title"), (descs, "duplicate-meta-description")):
            for text, urls in same.items():
                if len(urls) > 1:
                    for u in urls:
                        findings.append(self.ctx.finding(check, url=u, evidence=text[:120], count=len(urls)))

        assert self.s.entry_raw is not None
        tr = await transport(self.s.entry_url, page=self.s.entry_raw)
        _need(tr)
        findings += tr.findings
        not_checked += tr.not_checked
        self.s.stored_findings = list(findings)
        self.s.stored_nc = list(not_checked)

    async def _analyze(self) -> None:
        """
        Deep analysis: link graph, indexability table, duplicate content, optional data sources
        and the analysis modules.
        """
        s, cfg = self.s, self.ctx.config
        pages = list(s.pages.values())
        assert s.robots is not None
        vis_task: asyncio.Task[Result[VisibilitySignals]] | None = None
        if cfg.visibility:  # public lookups are slow: run them alongside the other analyses
            vis_task = asyncio.create_task(
                visibility(urlparse(s.origin).hostname or "", budget_s=s.options.visibility_budget_s)
            )
        psi_task: asyncio.Task[Result[PageSpeedReport]] | None = None
        crux_task: asyncio.Task[Result[FieldData]] | None = None
        if cfg.google_api_key:  # one key serves PageSpeed and CrUX; both are slow, so run alongside
            psi_task = asyncio.create_task(pagespeed(s.entry_url))
            crux_task = asyncio.create_task(field_data(s.entry_url, origin=s.origin))
        gsc_task: asyncio.Task[Result[GscData]] | None = None
        if self.ctx.session.token_store is not None and cfg.google_app is not None:
            gsc_task = asyncio.create_task(
                gsc.summary(
                    s.entry_url,
                    sample_urls=[p.url for p in pages],
                    keywords=s.options.keywords or None,
                    inspect_limit=cfg.gsc_inspect_limit,
                    account=s.options.account,
                )
            )
        new_findings: list[Finding] = []
        new_nc: list[NotChecked] = []

        lk = await links(
            pages,
            entry_url=s.entry_url,
            sitemap_urls=s.sitemap_urls,
            robots=s.robots,
            mode=s.mode,
            link_check_budget_s=s.options.link_check_budget_s,
        )
        s.links = _need(lk)
        new_findings += lk.findings
        new_nc += lk.not_checked

        ix = await indexability(pages, links=s.links, sitemap_urls=s.sitemap_urls, robots=s.robots)
        s.indexability = list(_need(ix).items)

        if s.mode == AuditMode.SITE:
            dup = await duplicates(pages)
            _need(dup)
            new_findings += dup.findings
            new_nc += dup.not_checked

        parsed = [p.parsed for p in s.parsed_pages() if p.parsed is not None]
        if parsed and not any(p.schema_types for p in parsed):
            new_findings.append(self.ctx.finding("schema-none-found", url=s.entry_url))
        s.has_schema = any(p.schema_types for p in parsed)
        s.has_images = any(p.images for p in parsed)

        if s.perf_all:
            new_findings += s.perf_out[0]
            new_nc += s.perf_out[1]
        psi = False
        if psi_task is not None:
            pr = await psi_task
            if pr.ok and pr.data is not None and pr.data.measured:
                psi = True
                new_findings += pr.findings
                new_nc += pr.not_checked
        cr = await crux_task if crux_task is not None else None
        fd = cr.data if cr is not None and cr.ok else None
        if cr is not None and fd is not None:
            s.crux = fd
            new_findings += cr.findings
            got = {m.metric for m in fd.metrics}
            # real-user data answers the checks lab data cannot (INP): they are no longer "not measured"
            new_nc += cr.not_checked

            def _answered(n: NotChecked) -> bool:
                return n.check_id.startswith("cwv-") and n.check_id.split("-")[1] in got

            new_nc[:] = [n for n in new_nc if not (_answered(n) and n.reason_code == "field-data-unavailable")]
            s.not_checked[:] = [n for n in s.not_checked if not _answered(n)]
        crux = bool(fd is not None and fd.metrics)
        if s.coverage is not None:
            s.coverage["psi"] = psi
            s.coverage["crux"] = crux
        else:
            s.coverage = {"psi": psi, "crux": crux}

        gsc_ok = False
        if gsc_task is not None:
            gr = await gsc_task
            if gr.ok and gr.data is not None:
                s.gsc = gr.data
                new_findings += gr.findings
                new_nc += gr.not_checked
                gsc_ok = gr.data.status == "ok"

        if vis_task is not None:
            vr = await vis_task
            if vr.ok and vr.data is not None:
                s.visibility = vr.data
                new_nc += vr.not_checked  # a lookup its time budget cut short

        extras, mod_findings, mod_nc, provided = await run_modules(
            ModuleInputs(
                mode=s.mode,
                entry_url=s.entry_url,
                entry=s.entry,
                pages=pages,
                lang=s.lang,
                keywords=list(s.options.keywords),
                competitors=list(s.options.competitors),
                robots=s.robots,
                sitemap_urls=s.sitemap_urls,
                renderer=s.renderer,
            )
        )
        s.extras = extras
        new_findings += mod_findings
        new_nc += mod_nc
        s.provided = provided | ({"gsc"} if gsc_ok else set())
        s.findings += new_findings
        s.not_checked += new_nc
        s.stored_findings += new_findings
        s.stored_nc += new_nc

    async def _score(self) -> None:
        s = self.s
        records = list(s.records.values())
        parsed_ok = bool(s.parsed_pages())
        assert s.robots is not None and s.entry_raw is not None
        has_lab = bool([p for p in s.perf_all or [] if not p.error])
        measured: dict[Category, str | None] = {
            Category.TECHNICAL: None,
            Category.ONPAGE: None if parsed_ok else "no-html",
            Category.CONTENT: None if parsed_ok else "no-html",
            Category.SCHEMA: None if s.has_schema else ("no-structured-data" if parsed_ok else "no-html"),
            Category.IMAGES: None if s.has_images else ("no-images" if parsed_ok else "no-html"),
            Category.AI_READINESS: None if s.robots.available else "robots-unreachable",
            Category.LINKS: None if s.links is not None else "no-data",
            Category.PERFORMANCE: None if has_lab else "no-lab-data",
        }
        available = {"headers", "robots", "sitemap", "rendered"} | ({"lab"} if has_lab else set())
        available |= s.provided
        scores = compute_scores(
            s.findings,
            s.not_checked,
            mode=s.mode,
            pages=records,
            measured=measured,
            gate_inputs=GateInputs(entry_url=s.entry_url, entry_status=s.entry_raw.status),
            available=available,
        )
        by_url: dict[str, list[Finding]] = defaultdict(list)
        for f in s.findings:
            if f.url:
                by_url[f.url].append(f)
        page_cats = (Category.ONPAGE, Category.CONTENT, Category.TECHNICAL, Category.SCHEMA, Category.IMAGES)
        cats = [c.category for c in scores.categories if c.measured and c.category in page_cats]
        for p in records:
            if p.error is None and p.status is not None and cats:
                p.page_score = page_score(by_url.get(p.url, []), cats)
        s.scores = scores

    # ------------------------------------------------------------------ result

    def _audit(
        self, status: AuditStatus, error: str | None, error_code: ErrorCode | None, cost: dict[str, Any]
    ) -> Audit:
        s = self.s
        cfg = self.ctx.config
        return Audit(
            id=new_id(),
            mode=s.mode,
            target_url=s.entry_url or s.url,
            final_origin=s.final_origin,
            status=status,
            business_type=s.business_type,
            lang=s.lang,
            created_at=s.created_at,
            finished_at=datetime.now(UTC),
            rule_version=catalog.RULE_VERSION,
            score_version=SCORE_VERSION,
            coverage=dict(s.coverage or {}),
            budget={"max_pages": min(s.options.max_pages or cfg.max_pages, cfg.max_pages_cap)},
            cost=cost,
            stages=list(s.stages.values()),
            pages=list(s.records.values()),
            findings=list(s.stored_findings),
            not_checked=list(s.stored_nc),
            scores=s.scores,
            links=s.links,
            perf=list(s.perf_all or []),
            indexability=list(s.indexability),
            visibility=s.visibility,
            gsc=s.gsc,
            field=s.crux,
            extras=dict(s.extras),
            error=error,
            error_code=error_code,
            options=s.options,
            seed=s.seed if s.mode == AuditMode.SITE else None,
            partial_reasons=list(s.partial_reasons),
        )


async def run_audit(
    ctx: Context, url: str, mode: AuditMode, options: AuditOptions, seed: int, created_at: datetime
) -> Audit:
    state = _State(url=url, mode=mode, options=options, seed=seed, created_at=created_at)
    return await Runner(ctx, state).run()
