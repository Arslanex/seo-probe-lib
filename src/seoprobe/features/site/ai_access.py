"""
Agent readiness: what an AI agent that browses or reads the site meets.

Adapted from the upstream project ``scripts/agentic_check.py`` (llms.txt format rules, Markdown delivery,
Content-Signal, WebMCP traces) and ``skills/seo-agentic/SKILL.md`` (priorities, honest-reporting
rules). MIT.

What this module does NOT do, on purpose:

* No Lighthouse "Agentic Browsing" fraction. That ``X/N`` comes from Lighthouse via PSI; without
  PSI it is not produced (``lighthouse_fraction`` stays ``None``). ``fraction`` below is our own
  small checklist and says so.
* No AI-crawler access analysis and no "llms.txt missing" finding: the robots operation already does
  both (``ai_crawlers``, ``llms-txt-missing``); only the *content* of an existing llms.txt is
  judged here.
* ``Accept: text/markdown`` negotiation is not tested: ``HttpClient`` has no request-header
  parameter. It is reported as a ``NotChecked`` (``fetcher-headers-unsupported``). A ``.md`` sibling
  URL and a ``rel="alternate" type="text/markdown"`` link are checked instead.
* Honest-reporting rules of the skill apply: nothing here is said to affect Google Search or to
  be requested by a named agent; llms.txt, Content-Signal and WebMCP are drafts or community
  practice, so every finding is Info or Low and heuristic.

At most ``MAX_REQUESTS`` requests, all through the safe HTTP client; fetched documents are untrusted
data and are only measured, never followed or obeyed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.client import RequestBudget
from seoprobe.core.page import Page
from seoprobe.core.parse.html import _HTML_PARSER
from seoprobe.core.parse.urls import origin_of
from seoprobe.core.rules.catalog import make_finding

from .models import AgenticCheck, AgenticReadiness, RobotsReport, Status
from .robots import robots as robots_op

MAX_REQUESTS = 6
LLMS_MAX_BYTES = 200_000
DOC_MAX_BYTES = 100_000
LLMS_MAX_LINES = 5000
LLMS_OVERSIZE_BYTES = 100_000
MAX_HTML_CHARS = 1_500_000

# ------------------------------------------------------------------------------- llms.txt

_LINK = re.compile(r"\[[^\]\n]{1,200}\]\([^)\s]{1,500}\)")
_H1 = re.compile(r"^#\s+\S")
_H2 = re.compile(r"^##\s+\S")
_FENCE = re.compile(r"^(```|~~~)")


def llms_txt_notes(text: str) -> list[str]:
    """
    Stable note codes for an llms.txt body (Lighthouse-style errors + llmstxt.org structure).

    Linear time; lines beyond ``LLMS_MAX_LINES`` are ignored.
    """
    raw = text.lstrip("﻿")
    if re.match(r"\s*<(!doctype|html)", raw[:200], re.I):
        return ["served-html"]
    notes: list[str] = []
    lines = raw.splitlines()[:LLMS_MAX_LINES]
    content = [ln.strip() for ln in lines if ln.strip()]
    if len(raw.strip()) < 50:
        notes.append("too-short")
    in_fence = False
    h1_count = h2_count = link_count = 0
    first = content[0] if content else ""
    summary_after_title = len(content) > 1 and content[1].startswith(">")
    for ln in content:
        if _FENCE.match(ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _H1.match(ln):
            h1_count += 1
        elif _H2.match(ln):
            h2_count += 1
        link_count += len(_LINK.findall(ln))
    if h1_count == 0:
        notes.append("missing-h1")
    elif not _H1.match(first):
        notes.append("h1-not-first")
    if h1_count > 1:
        notes.append("multiple-h1")
    if h1_count and not any(ln.startswith(">") for ln in content[:5]) and not summary_after_title:
        notes.append("no-summary-blockquote")
    if link_count == 0:
        notes.append("no-markdown-links")
    if h2_count == 0:
        notes.append("no-h2-sections")
    if len(raw.encode("utf-8", "replace")) > LLMS_OVERSIZE_BYTES:
        notes.append("oversize")
    return notes


# Notes that make the file "malformed" (the Lighthouse-style hard errors plus structure);
# the rest are hygiene notes shown in the report only.
_HARD = {"served-html", "too-short", "missing-h1", "no-markdown-links"}
_SOFT_FINDING = {"h1-not-first", "multiple-h1", "no-summary-blockquote", "no-h2-sections"}


# ------------------------------------------------------------------------------- DOM checks

_INPUT_SKIP = {"hidden", "submit", "button", "reset", "image"}


MAX_CONTROLS = 500  # per kind; a page with more is judged on its first 500 (hostile markup)


def _has_label(el: Any, label_for: set[str]) -> bool:
    for attr in ("aria-label", "aria-labelledby", "title", "placeholder"):
        if str(el.get(attr) or "").strip():
            return True
    if str(el.get("id") or "") in label_for:
        return True
    return el.find_parent("label") is not None


def unnamed_controls(html: str) -> dict[str, int]:
    """Counts of buttons, inputs and links without an accessible name (no accessibility tree)."""
    soup = BeautifulSoup(html[:MAX_HTML_CHARS], _HTML_PARSER)
    label_for = {str(lb.get("for")) for lb in soup.find_all("label", attrs={"for": True})}
    buttons = 0
    for b in [*soup.find_all("button"), *soup.find_all(None, {"role": "button"})][:MAX_CONTROLS]:
        name = b.get_text(" ", strip=True)
        has_img_alt = any(str(i.get("alt") or "").strip() for i in b.find_all("img"))
        if not (name or has_img_alt or _has_label(b, label_for)):
            buttons += 1
    inputs = 0
    for i in soup.find_all(["input", "select", "textarea"])[:MAX_CONTROLS]:
        if i.name == "input" and str(i.get("type") or "text").lower() in _INPUT_SKIP:
            continue
        if not _has_label(i, label_for):
            inputs += 1
    links = 0
    for a in soup.find_all("a", href=True)[:MAX_CONTROLS]:
        if a.get_text(" ", strip=True) or any(str(i.get("alt") or "").strip() for i in a.find_all("img")):
            continue
        if not _has_label(a, label_for):
            links += 1
    return {"buttons": buttons, "inputs": inputs, "links": links}


_MD_ALTERNATE = re.compile(r"<link\b[^>]*\brel=[\"']?alternate[\"']?[^>]*\btype=[\"']?text/markdown", re.I)
_MD_ALTERNATE_REV = re.compile(r"<link\b[^>]*\btype=[\"']?text/markdown[\"']?[^>]*\brel=[\"']?alternate", re.I)
_WEBMCP_MARKS = ("modelContext", "registerTool(", "ai-plugin+json")


# ------------------------------------------------------------------------------- fetching


@dataclass
class _Doc:
    status: int | None
    body: str
    content_type: str
    error: str | None = None

    @property
    def is_real(self) -> bool:
        """A 200 that is not an HTML catch-all page (the soft-404 trap)."""
        head = self.body[:200].lstrip().lower()
        return (
            self.status == 200
            and bool(self.body.strip())
            and "html" not in self.content_type
            and not head.startswith(("<!doctype", "<html"))
        )


async def _get(budget: RequestBudget, url: str, max_bytes: int) -> _Doc | None:
    """One budgeted fetch as a ``_Doc``; None once the budget is used up."""
    if budget.left <= 0:
        return None
    raw = await budget.fetch_bytes(url, max_bytes=max_bytes)
    return _Doc(
        raw.status,
        raw.body[:max_bytes].decode("utf-8", "replace"),
        raw.headers.get("content-type", "").lower(),
        raw.error,
    )


def _json_dict(doc: _Doc | None) -> dict[str, Any] | None:
    if doc is None or not doc.is_real:
        return None
    try:
        data = json.loads(doc.body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _md_sibling(entry_url: str) -> str:
    p = urlparse(entry_url)
    path = p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc}" + (path + ".md" if path else "/index.md")


async def audit_agentic(
    budget: RequestBudget, entry: Page, robots: RobotsReport | None
) -> tuple[AgenticReadiness, list[Finding], list[NotChecked]]:
    report = AgenticReadiness()
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    origin = origin_of(entry.effective_url).rstrip("/")
    html = entry.content_html[:MAX_HTML_CHARS]

    # 1. llms.txt content (its absence is judged by the robots module)
    present = robots.llms_txt_present if robots is not None else None
    if present is False:
        report.checks.append(AgenticCheck(id="llms-txt", status="na", detail="absent-optional"))
    elif present is None and robots is None:
        report.checks.append(AgenticCheck(id="llms-txt", status="unknown", detail="robots-unavailable"))
    else:
        doc = await _get(budget, f"{origin}/llms.txt", LLMS_MAX_BYTES)
        if doc is None or doc.error or doc.status is None:
            report.checks.append(AgenticCheck(id="llms-txt", status="unknown", detail="fetch-failed"))
            not_checked.append(NotChecked(check_id="llms-txt-malformed", reason_code="unreachable"))
        elif not doc.is_real:
            code = "served-html" if doc.status == 200 else f"http-{doc.status}"
            status: Status = "na" if doc.status != 200 and doc.status < 500 else "fail"
            report.checks.append(AgenticCheck(id="llms-txt", status=status, detail=code))
            if status == "fail":
                report.llms_txt_notes = [code]
        else:
            notes = llms_txt_notes(doc.body)
            report.llms_txt_notes = notes
            hard = [n for n in notes if n in _HARD]
            soft = [n for n in notes if n in _SOFT_FINDING]
            bad = hard or soft
            report.checks.append(AgenticCheck(id="llms-txt", status="fail" if bad else "pass", detail=",".join(notes)))
            if bad:
                findings.append(
                    make_finding(
                        "llms-txt-malformed",
                        url=f"{origin}/llms.txt",
                        evidence=", ".join(bad),
                        count=len(bad),
                    )
                )

    # 2. Markdown delivery: .md sibling and alternate link; negotiation cannot be tested
    not_checked.append(NotChecked(check_id="markdown-negotiation", reason_code="fetcher-headers-unsupported"))
    alt_link = bool(_MD_ALTERNATE.search(html) or _MD_ALTERNATE_REV.search(html))
    md = await _get(budget, _md_sibling(entry.url), DOC_MAX_BYTES)
    md_ok = md is not None and md.is_real
    if alt_link or md_ok:
        report.checks.append(
            AgenticCheck(
                id="markdown-delivery",
                status="pass",
                detail=",".join(x for x, ok in (("alternate-link", alt_link), ("md-sibling", md_ok)) if ok),
            )
        )
    elif md is None or md.error or md.status is None:
        report.checks.append(AgenticCheck(id="markdown-delivery", status="unknown", detail="fetch-failed"))
    else:
        report.checks.append(AgenticCheck(id="markdown-delivery", status="na", detail="not-found"))
        findings.append(
            make_finding(
                "markdown-not-available",
                url=entry.url,
                evidence="no .md sibling, no alternate Markdown link; Accept negotiation not tested",
            )
        )

    # 3. Content-Signal in robots.txt
    groups = robots.rules.groups if robots is not None and robots.rules is not None else None
    if robots is None or groups is None or robots.status == "unreachable":
        report.checks.append(AgenticCheck(id="content-signal", status="unknown", detail="robots-unavailable"))
        not_checked.append(NotChecked(check_id="content-signal-missing", reason_code="robots-unavailable"))
    else:
        signals = [s for g in groups for s in g.content_signal]
        if signals:
            report.checks.append(AgenticCheck(id="content-signal", status="pass", detail=signals[0][:80]))
        else:
            report.checks.append(AgenticCheck(id="content-signal", status="na", detail="absent-optional"))
            findings.append(
                make_finding(
                    "content-signal-missing",
                    url=f"{origin}/robots.txt",
                    evidence="no Content-Signal line in any robots.txt group",
                )
            )

    # 4. WebMCP / agent plugin traces
    traces = [m for m in _WEBMCP_MARKS if m in html]
    plugin = _json_dict(await _get(budget, f"{origin}/.well-known/ai-plugin.json", DOC_MAX_BYTES))
    mcp = _json_dict(await _get(budget, f"{origin}/.well-known/mcp", DOC_MAX_BYTES))
    if plugin is not None:
        traces.append("well-known:ai-plugin.json")
    if mcp is not None:
        traces.append("well-known:mcp")
    report.checks.append(
        AgenticCheck(
            id="agent-discovery",
            status="pass" if traces else "na",
            detail=",".join(dict.fromkeys(traces)) if traces else "none-found",
        )
    )

    # 5. agent-friendly controls
    if html.strip():
        try:
            counts = unnamed_controls(html)
        except Exception:  # noqa: BLE001 - hostile markup must not break the module
            counts = {}
        if counts:
            report.unnamed = counts
            total = sum(counts.values())
            report.checks.append(
                AgenticCheck(
                    id="named-controls",
                    status="fail" if total else "pass",
                    detail=", ".join(f"{k}={v}" for k, v in counts.items() if v) or "all-named",
                )
            )
            if total:
                findings.append(
                    make_finding(
                        "agent-unfriendly-controls",
                        url=entry.url,
                        evidence=", ".join(f"{k}={v}" for k, v in counts.items() if v),
                        count=total,
                    )
                )
        else:
            report.checks.append(AgenticCheck(id="named-controls", status="unknown", detail="parse-failed"))
    else:
        report.checks.append(AgenticCheck(id="named-controls", status="unknown", detail="no-html"))
        not_checked.append(NotChecked(check_id="agent-unfriendly-controls", reason_code="no-html"))

    passed = sum(c.status == "pass" for c in report.checks)
    counted = sum(c.status in ("pass", "fail") for c in report.checks)
    report.fraction = f"{passed}/{counted}"
    report.requests = budget.used
    report.notes += ["local-checklist-not-lighthouse", "lighthouse-fraction-needs-psi"]
    return report, findings, not_checked


@operation("ai_access", output=AgenticReadiness, requires=Requirements(network=True), timeout_s=40.0)
async def ai_access(
    ctx: Context, target: str | Page, *, robots: RobotsReport | None = None
) -> Outcome[AgenticReadiness]:
    """
    What an AI agent that browses or reads the site meets: llms.txt, Markdown, Content-Signal,
    agent discovery files and accessible control names.
    """
    entry = await ctx.page(target)
    if robots is None:
        rb = await robots_op(entry.url, origin=origin_of(entry.effective_url))
        robots = rb.data if rb.ok else None
    report, findings, not_checked = await audit_agentic(RequestBudget(ctx.http, MAX_REQUESTS), entry, robots)
    return Outcome(data=report, findings=findings, not_checked=not_checked)
