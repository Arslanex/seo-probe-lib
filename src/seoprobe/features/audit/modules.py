"""
Analysis modules of the audit: every module runs concurrently with its own timeout; one that
fails or times out is logged and skipped - it never breaks the audit. A module's JSON block is
stored under its ``kind`` in ``Audit.extras`` (the report reads it from there).

Each module is one operation of another feature, called with what the source module read from
its inputs. Where the source module returned no data (for example ``screenshots`` without a
browser), the block is not stored and only the not-checked entry is kept.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from seoprobe.core.models import AuditMode, Finding, NotChecked
from seoprobe.core.page import Page
from seoprobe.core.result import Result
from seoprobe.features.content import local_seo, previews, readability, suggest_schema
from seoprobe.features.crawl import hreflang
from seoprobe.features.offsite import authority, competitors
from seoprobe.features.render import screenshots
from seoprobe.features.site import RobotsReport, ai_access, generate_sitemap, site_setup, techstack

log = logging.getLogger("seoprobe.audit.modules")


@dataclass
class ModuleInputs:
    mode: AuditMode
    entry_url: str
    entry: Page  # rendered HTML when the entry page was rendered
    pages: list[Page]
    lang: str | None  # detected page language
    keywords: list[str]
    competitors: list[str]
    robots: RobotsReport
    sitemap_urls: list[str]
    renderer: bool  # a browser is available to this audit


@dataclass
class ModuleOutput:
    kind: str
    data: Any = None  # JSON-ready block, None when the module measured nothing
    findings: list[Finding] = field(default_factory=list)
    not_checked: list[NotChecked] = field(default_factory=list)
    available: set[str] = field(default_factory=set)  # scoring requirements this module provides


@dataclass(frozen=True)
class Module:
    name: str
    kind: str
    timeout_s: float
    run: Callable[[ModuleInputs], Awaitable[ModuleOutput | None]]


def _out(kind: str, res: Result[Any], *, keep: bool = True) -> ModuleOutput | None:
    """The module output of an operation result; None (skipped) when the operation failed."""
    if not res.ok:
        log.warning("module %s failed: %s", kind, res.error.code if res.error else "?")
        return None
    data = res.data.model_dump(mode="json") if keep and isinstance(res.data, BaseModel) else None
    return ModuleOutput(kind=kind, data=data, findings=list(res.findings), not_checked=list(res.not_checked))


def _left(res: Result[Any], check_id: str, *reasons: str) -> bool:
    return any(n.check_id == check_id and n.reason_code in reasons for n in res.not_checked)


async def _techstack(inp: ModuleInputs) -> ModuleOutput | None:
    return _out("techstack", await techstack(inp.entry))


async def _site_setup(inp: ModuleInputs) -> ModuleOutput | None:
    return _out("site_setup", await site_setup(inp.entry, pages=inp.pages))


async def _previews(inp: ModuleInputs) -> ModuleOutput | None:
    res = await previews(inp.entry)
    return _out("previews", res, keep=not _left(res, "previews", "parse-failed"))


async def _screenshots(inp: ModuleInputs) -> ModuleOutput | None:
    if not inp.renderer:
        url = inp.entry.final_url or inp.entry_url
        return ModuleOutput(
            kind="screenshots",
            not_checked=[NotChecked(check_id="screenshots", reason_code="render-unavailable", url=url)],
        )
    res = await screenshots(inp.entry)
    return _out("screenshots", res, keep=not _left(res, "screenshots", "target-rejected"))


async def _readability(inp: ModuleInputs) -> ModuleOutput | None:
    res = await readability(inp.entry, keywords=inp.keywords, lang=inp.lang)
    return _out("readability", res, keep=not _left(res, "readability", "parse-failed"))


async def _hreflang(inp: ModuleInputs) -> ModuleOutput | None:
    return _out("hreflang", await hreflang(inp.entry, pages=inp.pages))


async def _schema_suggest(inp: ModuleInputs) -> ModuleOutput | None:
    return _out("schema_suggestions", await suggest_schema(inp.entry, pages=inp.pages))


async def _agentic(inp: ModuleInputs) -> ModuleOutput | None:
    return _out("agentic", await ai_access(inp.entry, robots=inp.robots))


async def _competitors(inp: ModuleInputs) -> ModuleOutput | None:
    res = await competitors(inp.entry, competitors=inp.competitors)
    return _out("competitors", res, keep=not _left(res, "competitors", "no-competitors"))


async def _local(inp: ModuleInputs) -> ModuleOutput | None:
    return _out("local", await local_seo(inp.entry, pages=inp.pages))


async def _sitemap_gen(inp: ModuleInputs) -> ModuleOutput | None:
    res = await generate_sitemap(inp.pages, sitemap_urls=inp.sitemap_urls, mode=inp.mode)
    return _out(
        "sitemap_generated", res, keep=not _left(res, "sitemap_generated", "site-mode-only", "no-eligible-pages")
    )


async def _authority(inp: ModuleInputs) -> ModuleOutput | None:
    res = await authority(inp.entry)
    return _out("authority", res, keep=res.data is not None and res.data.error is None)


# The source order (by module name); findings keep this order.
MODULES: list[Module] = sorted(
    [
        Module("techstack", "techstack", 20.0, _techstack),
        Module("site_setup", "site_setup", 60.0, _site_setup),
        Module("previews", "previews", 30.0, _previews),
        Module("screenshots", "screenshots", 60.0, _screenshots),
        Module("readability", "readability", 20.0, _readability),
        Module("hreflang", "hreflang", 60.0, _hreflang),
        Module("schema_suggest", "schema_suggestions", 15.0, _schema_suggest),
        Module("agentic", "agentic", 40.0, _agentic),
        Module("competitors", "competitors", 120.0, _competitors),
        Module("local", "local", 30.0, _local),
        Module("sitemap_gen", "sitemap_generated", 15.0, _sitemap_gen),
        Module("authority", "authority", 20.0, _authority),
    ],
    key=lambda m: m.name,
)


async def run_modules(
    inp: ModuleInputs,
) -> tuple[dict[str, Any], list[Finding], list[NotChecked], set[str]]:
    """Run every module concurrently; returns (extras, findings, not_checked, provided)."""

    async def one(m: Module) -> ModuleOutput | None:
        try:
            return await asyncio.wait_for(m.run(inp), timeout=m.timeout_s)
        except Exception as exc:  # noqa: BLE001 - isolation: a module never breaks the audit
            log.warning("module %s failed: %s", m.name, type(exc).__name__)
            return None

    extras: dict[str, Any] = {}
    findings: list[Finding] = []
    nc: list[NotChecked] = []
    provided: set[str] = set()
    for res in await asyncio.gather(*(one(m) for m in MODULES)):
        if res is None:
            continue
        if res.data is not None:
            extras[res.kind] = res.data
        findings += res.findings
        nc += res.not_checked
        provided |= res.available
    return extras, findings, nc, provided
