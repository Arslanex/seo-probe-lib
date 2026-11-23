"""
robots.txt: RFC 9309 parsing, ``can_fetch``, and AI crawler access findings.

Group selection and path matching follow the upstream project ``scripts/agentic_check.py``
(``parse_robots``/``select_group``/``is_allowed``, MIT), which implements RFC 9309:

* a named group replaces ``*`` (nothing inherits); all groups naming the same token merge;
* the longest matching rule wins, ``Allow`` wins a tie, an empty ``Disallow`` allows everything;
* ``*`` is any run of characters, a final ``$`` anchors the end. Matching never builds a regex
  from a rule (an untrusted rule such as ``****x`` would backtrack without limit);
* a user-agent value is reduced to its leading ``[A-Za-z_-]`` product token (Google's reading).

Status handling (RFC 9309 s.2.3.1): 2xx parse; 4xx = unavailable, no restrictions; 5xx or no
answer = unreachable, crawlers must assume the whole site is disallowed for now.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import CheckOutput, Finding, NotChecked, Requirements, Severity
from seoprobe.core.net.client import HttpClient
from seoprobe.core.parse.urls import origin_of
from seoprobe.core.rules.catalog import make_finding

from .models import AICrawlerStatus, Group, RobotsReport, RobotsRules

MAX_ROBOTS_BYTES = 1024 * 1024
MAX_RULES = 10_000
MAX_SITEMAP_LINES = 64
LLMS_MAX_BYTES = 200_000
AI_CRAWLERS_RESOURCE = "data/bots.json"  # in the seoprobe.core package

_LINE_BREAK = re.compile(r"\r\n|\n|\r")
_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]+")
_STAR_RUN = re.compile(r"\*{2,}")


def robots_lines(text: str) -> list[str]:
    """Split on CR, LF or CRLF only (RFC 9309); U+2028 must not turn a comment into a rule."""
    return _LINE_BREAK.split(text.lstrip("﻿"))


def product_token(value: str) -> str:
    """Lowercased product token of one ``User-agent:`` value: ``*``, a name, or ``""``."""
    value = (value or "").strip()
    if value == "*":
        return "*"
    m = _PRODUCT_TOKEN.match(value)
    return m.group(0).lower() if m else ""


def parse_robots(text: str, *, max_bytes: int = MAX_ROBOTS_BYTES) -> RobotsRules:
    """Parse robots.txt into RFC 9309 groups. Input beyond ``max_bytes`` is ignored."""
    rules = RobotsRules()
    raw = text.encode("utf-8", "replace")
    if len(raw) > max_bytes:
        text = raw[:max_bytes].decode("utf-8", "ignore")
        rules.truncated = True
    current: Group | None = None
    last_was_ua = False
    total = 0
    for raw_line in robots_lines(text):
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not last_was_ua:
                current = Group()
                rules.groups.append(current)
            current.agents.append(value)
            last_was_ua = True
            continue
        last_was_ua = False
        if key == "sitemap":
            if len(rules.sitemaps) < MAX_SITEMAP_LINES:
                rules.sitemaps.append(value)
        elif current is None:
            continue
        elif key in ("allow", "disallow"):
            total += 1
            if total > MAX_RULES:
                rules.truncated = True
                break
            current.rules.append((key, value))
        elif key == "content-signal":
            current.content_signal.append(value)
    return rules


def select_rules(rules: RobotsRules, token: str) -> tuple[str, list[tuple[str, str]]]:
    """RFC 9309: merge every group naming ``token``; else every ``*`` group. -> (matched, rules)"""
    token_l = token.strip().lower()
    named = [g for g in rules.groups if any(product_token(a) == token_l for a in g.agents)]
    chosen = named or [g for g in rules.groups if any(product_token(a) == "*" for a in g.agents)]
    matched = "named" if named else ("star" if chosen else "none")
    return matched, [r for g in chosen for r in g.rules]


def _pattern_matches(pattern: str, path: str) -> bool:
    """RFC 9309 path match. Uses ``str.find`` so no untrusted rule can cause backtracking."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    if "*" not in body:
        return path == body if anchored else path.startswith(body)
    first, *middle, last = _STAR_RUN.sub("*", body).split("*")
    if not path.startswith(first):
        return False
    pos = len(first)
    for piece in middle:
        found = path.find(piece, pos)
        if found < 0:
            return False
        pos = found + len(piece)
    if anchored:
        return len(path) - len(last) >= pos and path.endswith(last)
    return path.find(last, pos) >= 0


def is_allowed(rules: list[tuple[str, str]], path: str = "/") -> bool:
    """Longest-match evaluation; allow wins a tie; an empty ``Disallow`` allows everything."""
    best_len, allowed = -1, True
    for field_name, value in rules:
        if field_name == "disallow" and value == "":
            continue
        if _pattern_matches(value, path):
            if len(value) > best_len or (len(value) == best_len and field_name == "allow"):
                best_len, allowed = len(value), field_name == "allow"
    return allowed


def _path_of(url: str) -> str:
    p = urlparse(url)
    path = p.path or "/"
    return f"{path}?{p.query}" if p.query else path


def can_fetch(rules: RobotsRules, user_agent: str, url: str) -> bool:
    """May ``user_agent`` (e.g. ``"seoprobe/0.1"``) fetch ``url``?"""
    _matched, chosen = select_rules(rules, product_token(user_agent))
    return is_allowed(chosen, _path_of(url))


# ------------------------------------------------------------------ AI crawler data


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files("seoprobe.core").joinpath(AI_CRAWLERS_RESOURCE).read_text("utf-8"))
    return data


def load_ai_crawlers() -> list[dict[str, Any]]:
    """Crawler records from ``seoprobe/core/data/bots.json`` (token, vendor, capability, ...)."""
    return list(_load()["crawlers"])


@dataclass
class RobotsResult:
    status: str  # ok | missing | unreachable
    http_status: int | None
    rules: RobotsRules
    sitemaps: list[str]
    ai_crawlers: list[AICrawlerStatus]
    output: CheckOutput
    entry_allowed_for_bot: bool = True
    entry_allowed_for_googlebot: bool = True
    llms_txt_present: bool | None = None

    def allows(self, bot_name: str, url: str) -> bool:
        """Crawl permission for discovery/sampling. Unreachable robots = disallow (RFC 9309)."""
        return can_fetch(self.rules, bot_name, url)


def _disallow_all() -> RobotsRules:
    return RobotsRules(groups=[Group(agents=["*"], rules=[("disallow", "/")])])


def _site_blocked(rules: RobotsRules, token: str) -> bool:
    """Root disallowed for ``token`` and no ``Allow`` rule in its group (conservative)."""
    _m, chosen = select_rules(rules, token)
    return (not is_allowed(chosen, "/")) and not any(f == "allow" for f, _v in chosen)


def _origin_url(origin: str, path: str) -> str:
    return origin.rstrip("/") + path


async def run_robots_checks(origin: str, entry_url: str, fetcher: HttpClient, bot_name: str) -> RobotsResult:
    """Fetch and judge robots.txt (and /llms.txt) for ``origin``; never raises."""
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    url = _origin_url(origin, "/robots.txt")
    raw = await fetcher.fetch_bytes(url, max_bytes=MAX_ROBOTS_BYTES + 1)

    status = "ok"
    http_status = raw.status
    rules = RobotsRules()
    if raw.error is not None or raw.status is None or raw.status >= 500:
        status = "unreachable"
        rules = _disallow_all()  # RFC 9309: assume complete disallow while unreachable
        findings.append(
            make_finding(
                "robots-unreachable",
                url=url,
                evidence=f"status={raw.status} error={raw.error}"[:200],
            )
        )
        for rid in (
            "ai-search-bots-blocked",
            "ai-training-bots-blocked",
            "robots-blocks-site",
            "robots-disallows-entry",
        ):
            not_checked.append(NotChecked(check_id=rid, reason_code="robots-unreachable", url=url))
    elif 400 <= raw.status < 500:
        status = "missing"
        findings.append(make_finding("robots-missing", url=url, evidence=f"status={raw.status}"))
    else:
        text = raw.body[:MAX_ROBOTS_BYTES].decode("utf-8", "replace")
        rules = parse_robots(text)
        if raw.truncated or len(raw.body) > MAX_ROBOTS_BYTES:
            rules.truncated = True

    sitemaps = [s for s in dict.fromkeys(rules.sitemaps) if urlparse(s).scheme in ("http", "https")]

    # --- site / entry blocking (only meaningful when a file was actually parsed)
    entry_bot = entry_g = True
    if status == "ok":
        if _site_blocked(rules, "googlebot"):  # falls back to the * group when no named group
            findings.append(make_finding("robots-blocks-site", url=url, evidence="Googlebot (or * group): Disallow: /"))
        entry_bot = can_fetch(rules, bot_name, entry_url)
        entry_g = can_fetch(rules, "Googlebot", entry_url)
        if not entry_g:
            findings.append(make_finding("robots-disallows-entry", url=entry_url, evidence="blocked for Googlebot"))
        elif not entry_bot:
            # only our own bot is excluded: not an SEO problem; the entry URL is still fetched
            findings.append(
                make_finding(
                    "robots-disallows-entry",
                    url=entry_url,
                    evidence=f"blocked for {product_token(bot_name)} only",
                    severity=Severity.INFO,
                    severity_reason="Only the audit bot is excluded; Googlebot may crawl the page.",
                )
            )

    # --- AI crawlers (per capability)
    ai: list[AICrawlerStatus] = []
    if status != "unreachable":
        for rec in load_ai_crawlers():
            matched, chosen = select_rules(rules, rec["token"])
            ai.append(
                AICrawlerStatus(
                    token=rec["token"],
                    vendor=rec["vendor"],
                    capability=rec["capability"],
                    governs=rec["governs"],
                    not_evidence_for=rec["not_evidence_for"],
                    group=matched,
                    root_allowed=is_allowed(chosen, "/"),
                )
            )
        blocked_search = [a.token for a in ai if a.capability == "search" and not a.root_allowed]
        blocked_train = [a.token for a in ai if a.capability == "training" and not a.root_allowed]
        if blocked_search:
            findings.append(
                make_finding(
                    "ai-search-bots-blocked",
                    url=url,
                    evidence="blocked search crawlers: " + ", ".join(blocked_search),
                )
            )
        if blocked_train:
            findings.append(
                make_finding(
                    "ai-training-bots-blocked",
                    url=url,
                    evidence="training tokens blocked: " + ", ".join(blocked_train),
                )
            )

    # --- llms.txt (optional signal; Google Search ignores it)
    llms_present: bool | None = None
    lr = await fetcher.fetch_bytes(_origin_url(origin, "/llms.txt"), max_bytes=LLMS_MAX_BYTES)
    if lr.error is None and lr.status is not None:
        ctype = lr.headers.get("content-type", "").lower()
        # a 200 HTML page is a catch-all, not a real llms.txt (as in the upstream project agentic_check)
        llms_present = lr.status == 200 and bool(lr.body.strip()) and "html" not in ctype
        if not llms_present:
            findings.append(make_finding("llms-txt-missing", url=_origin_url(origin, "/llms.txt")))
    else:
        not_checked.append(NotChecked(check_id="llms-txt-missing", reason_code="unreachable", url=url))

    facts = {
        "robots_status": status,
        "http_status": http_status,
        "groups": len(rules.groups),
        "sitemaps": sitemaps,
        "truncated": rules.truncated,
        "llms_txt_present": llms_present,
        "ai_crawlers": [a.model_dump() for a in ai],
    }
    return RobotsResult(
        status=status,
        http_status=http_status,
        rules=rules,
        sitemaps=sitemaps,
        ai_crawlers=ai,
        output=CheckOutput(findings=findings, not_checked=not_checked, facts=facts),
        entry_allowed_for_bot=entry_bot,
        entry_allowed_for_googlebot=entry_g,
        llms_txt_present=llms_present,
    )


@operation("robots", output=RobotsReport, requires=Requirements(network=True), timeout_s=30.0)
async def robots(ctx: Context, url: str, *, origin: str | None = None) -> Outcome[RobotsReport]:
    """robots.txt rules, AI crawler access and llms.txt presence for the site of ``url``."""
    res = await run_robots_checks(origin or origin_of(url), url, ctx.http, ctx.config.bot_name)
    report = RobotsReport(
        status=res.status,
        http_status=res.http_status,
        rules=res.rules,
        sitemaps=list(res.sitemaps),
        ai_crawlers=res.ai_crawlers,
        entry_allowed_for_bot=res.entry_allowed_for_bot,
        entry_allowed_for_googlebot=res.entry_allowed_for_googlebot,
        llms_txt_present=res.llms_txt_present,
        facts=res.output.facts,
    )
    return Outcome(data=report, findings=list(res.output.findings), not_checked=list(res.output.not_checked))
