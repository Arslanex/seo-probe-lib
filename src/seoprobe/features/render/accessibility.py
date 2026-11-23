"""
Accessibility: axe-core WCAG checks in the guarded browser.

axe-core (MPL-2.0, Deque Systems) ships unmodified in ``axe/`` at a pinned version. It is evaluated
in the page through the DevTools protocol, so a Content-Security-Policy cannot block it and no
request leaves the machine for it. Violations are grouped into four findings by axe impact; the
full list (rule, impact, WCAG criteria, help URL, nodes) stays in the data. Results axe cannot
decide alone ("incomplete") need a manual review and are listed separately, without findings.
"""

from __future__ import annotations

import asyncio
import json
import re
from functools import cache
from importlib.resources import files
from typing import Any, Literal

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, Requirements
from seoprobe.core.net.browser import DESKTOP_VIEWPORT, PlaywrightError, Renderer, RenderUnavailable
from seoprobe.core.net.safety import URLSafetyError
from seoprobe.core.page import Page
from seoprobe.core.rules import catalog

from .models import A11yNode, A11yRule, AccessibilityReport

NAV_TIMEOUT_MS = 20000
SETTLE_MS = 800
AXE_TIMEOUT_S = 30.0

Standard = Literal["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"]
STANDARD_TAGS: dict[str, tuple[str, ...]] = {
    "wcag2a": ("wcag2a",),
    "wcag2aa": ("wcag2a", "wcag2aa"),
    "wcag21aa": ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa"),
    "wcag22aa": ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"),
}
IMPACTS = ("critical", "serious", "moderate", "minor")
A11Y_RULES = tuple(f"a11y-{i}" for i in IMPACTS)

_CRITERION = re.compile(r"^wcag(\d)(\d)(\d{1,2})$")
_VERSION = re.compile(r"axe v(\d+\.\d+\.\d+)")


@cache
def axe_source() -> str:
    return files(__package__).joinpath("axe/axe.min.js").read_text(encoding="utf-8")


def axe_version() -> str:
    """The vendored axe-core version (from the file header)."""
    m = _VERSION.search(axe_source()[:200])
    return m.group(1) if m else ""


@cache
def defaults() -> dict[str, int]:
    data: dict[str, dict[str, int]] = json.loads(
        files(__package__).joinpath("thresholds.json").read_text(encoding="utf-8")
    )
    return data["accessibility"]


def wcag_criteria(tags: list[str]) -> list[str]:
    """``["wcag143", "wcag1410"]`` -> ``["1.4.3", "1.4.10"]``."""
    out: list[str] = []
    for tag in tags:
        m = _CRITERION.match(tag)
        if m:
            out.append(".".join(m.groups()))
    return out


# Inject = evaluate the source inside a function; axe attaches itself to ``window``.
INJECT_JS = "() => {{\n{source}\n;return window.axe ? window.axe.version : null; }}"

RUN_JS = """
async (opts) => {
  const r = await window.axe.run(document, {runOnly: {type: 'tag', values: opts.tags},
                                            resultTypes: ['violations', 'incomplete']});
  const slim = rules => rules.map(v => ({
    id: v.id, impact: v.impact || null, tags: v.tags, help: v.help, help_url: v.helpUrl,
    nodes_count: v.nodes.length,
    nodes: v.nodes.slice(0, opts.maxNodes).map(n => ({
      selector: (n.target || []).map(t => Array.isArray(t) ? t.join(' >>> ') : String(t)).join(' | '),
      html: String(n.html || '').slice(0, opts.maxHtml)}))}));
  return {version: r.testEngine && r.testEngine.version, violations: slim(r.violations),
          incomplete: slim(r.incomplete), passes: r.passes.length, inapplicable: r.inapplicable.length};
}
"""


def _rules(raw: list[dict[str, Any]]) -> list[A11yRule]:
    rules = [
        A11yRule(
            id=str(v["id"]),
            impact=v.get("impact"),
            tags=list(v.get("tags") or []),
            wcag=wcag_criteria(list(v.get("tags") or [])),
            help=str(v.get("help") or ""),
            help_url=str(v.get("help_url") or ""),
            nodes_count=int(v.get("nodes_count") or 0),
            nodes=[A11yNode(**n) for n in v.get("nodes") or []],
        )
        for v in raw
    ]
    rank = {i: n for n, i in enumerate(IMPACTS)}
    return sorted(rules, key=lambda r: (rank.get(r.impact or "minor", len(IMPACTS)), r.id))


async def _run_axe(renderer: Renderer, url: str, tags: tuple[str, ...], opts: dict[str, int]) -> dict[str, Any]:
    async with renderer.open_page(
        viewport=DESKTOP_VIEWPORT,
        blocked_types=(),
        max_requests=300,
        max_bytes=30 * 1024 * 1024,
    ) as sess:
        page = sess.page
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(SETTLE_MS)
        loaded = await page.evaluate(INJECT_JS.format(source=axe_source()))
        if not loaded:
            raise RenderUnavailable("axe-core did not load")
        result: dict[str, Any] = await asyncio.wait_for(
            page.evaluate(
                RUN_JS,
                {"tags": list(tags), "maxNodes": opts["max_nodes_per_rule"], "maxHtml": opts["max_html_chars"]},
            ),
            timeout=AXE_TIMEOUT_S,
        )
    return result


def a11y_findings(violations: list[A11yRule], url: str, lang: str = "en") -> list[Finding]:
    out: list[Finding] = []
    for impact in IMPACTS:
        group = [v for v in violations if (v.impact or "minor") == impact]
        if not group:
            continue
        evidence = "; ".join(f"{v.id}: {v.help} ({v.nodes_count} nodes)" for v in group)
        out.append(
            catalog.make_finding(
                f"a11y-{impact}", url=url, evidence=evidence, count=sum(v.nodes_count for v in group), lang=lang
            )
        )
    return out


@operation(
    "accessibility",
    output=AccessibilityReport,
    requires=Requirements(network=True, browser=True, extras=["browser"]),
    timeout_s=90.0,
)
async def accessibility(
    ctx: Context, target: str | Page, *, standard: Standard = "wcag21aa"
) -> Outcome[AccessibilityReport]:
    """WCAG checks with axe-core for the chosen conformance level."""
    page = await ctx.page(target)
    url = page.final_url or page.url
    tags = STANDARD_TAGS[standard]
    rep = AccessibilityReport(url=url, standard=standard, tags=list(tags), axe_version=axe_version())
    renderer = await ctx.browser()
    try:
        await renderer.judge(url)
    except URLSafetyError:
        rep.error = "target-rejected"
        return Outcome(data=rep, not_checked=[ctx.not_checked(r, "target-rejected", url=url) for r in A11Y_RULES])
    try:
        raw = await asyncio.wait_for(
            _run_axe(renderer, url, tags, defaults()), timeout=NAV_TIMEOUT_MS / 1000 + AXE_TIMEOUT_S + 10
        )
    except (RenderUnavailable, PlaywrightError, TimeoutError, OSError) as exc:
        rep.error = f"render-failed: {type(exc).__name__}"
        return Outcome(data=rep, not_checked=[ctx.not_checked(r, "render-failed", url=url) for r in A11Y_RULES])
    rep.axe_version = str(raw.get("version") or rep.axe_version)
    rep.violations = _rules(raw["violations"])
    rep.incomplete = _rules(raw["incomplete"])
    rep.passes = int(raw["passes"])
    rep.inapplicable = int(raw["inapplicable"])
    for v in rep.violations:
        key = v.impact or "minor"
        rep.violations_by_impact[key] = rep.violations_by_impact.get(key, 0) + 1
    return Outcome(data=rep, findings=a11y_findings(rep.violations, url, ctx.lang))
