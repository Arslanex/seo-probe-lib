"""
Hreflang audit.

Adapted from the upstream project ``skills/seo-hreflang/SKILL.md`` (validation checks 1-7, MIT). What is
checked, from the sampled pages and the HTTP ``Link`` header:

* language / script / region codes (ISO 639-1, ISO 15924 shape, ISO 3166-1 alpha-2),
* the same language pointing at two different URLs,
* return links: A -> B needs B -> A (the most common error; Google ignores an unreciprocated pair).
  Targets outside the sample are fetched (at most ``MAX_PROBES``, polite, via the safe fetcher),
* the target answers 200, is not noindex and does not canonicalise elsewhere,
* the page itself is not canonicalised elsewhere (hreflang on a non-canonical URL is ignored),
* ``x-default`` present (a recommendation, heuristic),
* ``<html lang>`` agrees with the page's own hreflang entry.

Not covered (reported honestly, never guessed): hreflang in XML sitemaps (our sitemap reader
does not keep ``xhtml:link``), absolute-URL form (the parser resolves relative hrefs before
this module sees them), protocol and trailing-slash consistency beyond what the target checks
reveal. The self-reference check lives in the page checks (``hreflang-missing-self``).
A check that could not run is a ``NotChecked``, never a pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from typing import Any
from urllib.parse import urljoin

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.client import HttpClient
from seoprobe.core.page import Page
from seoprobe.core.parse.html import ParsedPage, url_match_key
from seoprobe.core.rules.catalog import make_finding
from seoprobe.features.site import header_directives, meta_noindex, same_page

from .models import HreflangEntry, HreflangPageRow, HreflangReport

MAX_PROBES = 8
MAX_ENTRIES_PER_PAGE = 100  # a page with more is truncated (hostile input)
MAX_PAGES = 200

HREFLANG_RULE_IDS = (
    "hreflang-invalid-code",
    "hreflang-no-return-link",
    "hreflang-target-not-200",
    "hreflang-target-noindex",
    "hreflang-canonical-conflict",
    "hreflang-no-x-default",
    "hreflang-duplicate-language",
    "hreflang-lang-attr-mismatch",
)


# ------------------------------------------------------------------------------- code validation


@lru_cache(maxsize=1)
def _codes() -> tuple[frozenset[str], frozenset[str]]:
    data = json.loads(files(__package__).joinpath("hreflang-codes.json").read_text("utf-8"))
    return frozenset(data["languages"]), frozenset(data["regions"])


_SCRIPT = re.compile(r"^[A-Za-z]{4}$")


def valid_hreflang_code(code: str) -> bool:
    """``x-default`` or ``lang[-Script][-REGION]``; a region alone is invalid."""
    code = code.strip()
    if code.lower() == "x-default":
        return True
    langs, regions = _codes()
    parts = code.split("-")
    if not 1 <= len(parts) <= 3 or parts[0].lower() not in langs or len(parts[0]) != 2:
        return False
    rest = parts[1:]
    if rest and _SCRIPT.match(rest[0]):
        rest = rest[1:]
    if not rest:
        return True
    return len(rest) == 1 and rest[0].upper() in regions and len(rest[0]) == 2


def primary_language(code: str) -> str:
    return code.strip().lower().split("-")[0].replace("_", "-")


# ------------------------------------------------------------------------------- extraction

_LINK_PART = re.compile(r"<([^>]{1,2000})>([^<]{0,400})")
_PARAM = re.compile(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s;,]+))')


def link_header_hreflang(value: str, base: str) -> list[tuple[str, str]]:
    """``Link: <url>; rel="alternate"; hreflang="de"`` entries of a Link header value."""
    out: list[tuple[str, str]] = []
    for m in _LINK_PART.finditer(value[:8000]):
        params = {k.lower(): (q or u).strip() for k, q, u in _PARAM.findall(m.group(2))}
        rels = params.get("rel", "").lower().split()
        if "alternate" in rels and params.get("hreflang"):
            out.append((params["hreflang"], urljoin(base, m.group(1).strip())))
    return out


def page_entries(page: Page) -> list[HreflangEntry]:
    seen: set[tuple[str, str]] = set()
    out: list[HreflangEntry] = []
    sources: list[tuple[str, list[tuple[str, str]]]] = []
    if page.parsed is not None:
        sources.append(("html", list(page.parsed.hreflang)))
    link = page.headers.get("link", "")
    if link:
        sources.append(("header", link_header_hreflang(link, page.effective_url)))
    for source, items in sources:
        for lang, href in items:
            key = (lang.strip().lower(), url_match_key(href))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                HreflangEntry(
                    lang=lang.strip()[:20],
                    href=href[:500],
                    source=source,
                    valid=valid_hreflang_code(lang),
                )
            )
            if len(out) >= MAX_ENTRIES_PER_PAGE:
                return out
    return out


_LANG_PATH = re.compile(r"^/([a-z]{2})(?:-[a-z]{2})?(?:/|$)", re.I)


def multilingual_signals(pages: list[Page]) -> list[str]:
    """Why the site looks multilingual even without hreflang (for the not-applicable decision)."""
    signals: list[str] = []
    langs = {primary_language(p.parsed.lang) for p in pages if p.parsed is not None and p.parsed.lang.strip()}
    if len(langs) >= 2:
        signals.append("html-lang:" + ",".join(sorted(langs)[:6]))
    path_langs: set[str] = set()
    known, _ = _codes()
    for p in pages:
        if p.parsed is None:
            continue
        for link in p.parsed.links[:300]:
            if not link.resolved:
                continue
            m = _LANG_PATH.match(_path_of(link.resolved))
            if m and m.group(1).lower() in known:
                path_langs.add(m.group(1).lower())
    if len(path_langs) >= 2:
        signals.append("language-paths:" + ",".join(sorted(path_langs)[:6]))
    return signals


def _path_of(url: str) -> str:
    rest = url.split("://", 1)[-1]
    path = "/" + rest.partition("/")[2]
    return path.split("?", 1)[0].split("#", 1)[0]


# ------------------------------------------------------------------------------- target lookup


@dataclass
class _Target:
    page: Page
    entries: list[HreflangEntry]
    probed: bool = False

    @property
    def url(self) -> str:
        return self.page.url

    @property
    def status(self) -> int | None:
        return self.page.status

    @property
    def final_url(self) -> str:
        return self.page.effective_url

    @property
    def error(self) -> str | None:
        return self.page.error

    @property
    def parsed(self) -> ParsedPage | None:
        return self.page.parsed

    @property
    def noindex(self) -> bool:
        if meta_noindex(self.page):
            return True
        xrt = self.page.headers.get("x-robots-tag", "")
        return bool(xrt and header_directives(xrt) & {"noindex", "none"})

    @property
    def canonical(self) -> str | None:
        if self.parsed is not None and self.parsed.canonical:
            return urljoin(self.final_url or self.url, self.parsed.canonical)
        return None


@dataclass
class _Resolver:
    fetcher: HttpClient
    by_key: dict[str, _Target] = field(default_factory=dict)
    probes: int = 0
    probed: list[str] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)

    def seed(self, pages: list[Page]) -> None:
        for p in pages:
            t = _Target(p, page_entries(p))
            for u in (p.url, p.effective_url):
                self.by_key.setdefault(url_match_key(u), t)

    async def get(self, url: str) -> _Target | None:
        key = url_match_key(url)
        if key in self.by_key:
            return self.by_key[key]
        if self.probes >= MAX_PROBES:
            self.unchecked.append(url)
            return None
        self.probes += 1
        self.probed.append(url)
        target = await self._probe(url)
        self.by_key[key] = target
        self.by_key.setdefault(url_match_key(target.final_url), target)
        return target

    async def _probe(self, url: str) -> _Target:
        try:
            fr = await self.fetcher.fetch(url)
        except Exception as exc:  # noqa: BLE001 - one bad target must not break the module
            failed = Page(url=url, final_url=url, error=f"internal-error: {type(exc).__name__}")
            return _Target(failed, [], True)
        page = Page.from_fetch(fr)
        await page.parse()
        return _Target(page, page_entries(page), True)


# ------------------------------------------------------------------------------- the audit


def _points_at(entries: list[HreflangEntry], page_urls: tuple[str, ...]) -> bool:
    keys = {url_match_key(u) for u in page_urls}
    return any(url_match_key(e.href) in keys for e in entries)


async def audit_hreflang(
    pages: list[Page], fetcher: HttpClient
) -> tuple[HreflangReport, list[Finding], list[NotChecked]]:
    pages = [p for p in pages if p.error is None and p.status is not None and p.status < 400]
    pages = pages[:MAX_PAGES]
    report = HreflangReport()
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []

    with_entries = [(p, page_entries(p)) for p in pages]
    with_entries = [(p, e) for p, e in with_entries if e]
    if not with_entries:
        signals = multilingual_signals(pages)
        report.multilingual_signals = signals
        report.notes.append("multilingual-without-hreflang" if signals else "single-language-site")
        not_checked.append(NotChecked(check_id="hreflang", reason_code="not-applicable"))
        return report, findings, not_checked

    report.applicable = True
    resolver = _Resolver(fetcher)
    resolver.seed(pages)
    languages: set[str] = set()
    missing_x_default = 0

    for p, entries in with_entries:
        row = HreflangPageRow(
            url=p.url,
            html_lang=(p.parsed.lang if p.parsed else "")[:20],
            entries=entries,
            canonical=urljoin(p.effective_url, p.parsed.canonical)
            if p.parsed is not None and p.parsed.canonical
            else None,
        )
        add = _adder(findings, row, p.url)

        bad = [e.lang for e in entries if not e.valid]
        if bad:
            add("hreflang-invalid-code", ", ".join(dict.fromkeys(bad))[:150], len(bad))
        valid = [e for e in entries if e.valid]
        languages.update(e.lang.lower() for e in valid if e.lang.lower() != "x-default")

        by_lang: dict[str, set[str]] = {}
        for e in valid:
            by_lang.setdefault(e.lang.lower(), set()).add(url_match_key(e.href))
        dups = [lang for lang, urls in by_lang.items() if len(urls) > 1]
        if dups:
            add("hreflang-duplicate-language", ", ".join(dups[:8]), len(dups))

        if "x-default" in by_lang:
            report.has_x_default = True
        elif len(by_lang) >= 2:
            missing_x_default += 1

        if row.canonical and not same_page(row.canonical, p.effective_url) and not same_page(row.canonical, p.url):
            add("hreflang-canonical-conflict", f"page canonical -> {row.canonical}")

        # a page may name itself under several codes (apple.com: en-US and es-US); it is a
        # mismatch only when none of them is the language of its <html lang>
        own = [e for e in _own_entries(entries, p) if e.valid and e.lang.lower() != "x-default"]
        if own and row.html_lang and all(primary_language(e.lang) != primary_language(row.html_lang) for e in own):
            codes = ", ".join(e.lang for e in own[:5])
            add("hreflang-lang-attr-mismatch", f"hreflang={codes} html lang={row.html_lang}")

        # targets
        for e in valid:
            if url_match_key(e.href) in {url_match_key(p.url), url_match_key(p.effective_url)}:
                continue
            target = await resolver.get(e.href)
            if target is None:
                report.unchecked += 1
                not_checked.append(
                    NotChecked(check_id="hreflang-no-return-link", reason_code="probe-limit", url=e.href)
                )
                continue
            if target.error or target.status is None or target.status != 200:
                code = target.error or f"status={target.status}"
                add("hreflang-target-not-200", f"{e.lang} {e.href} ({code})")
                continue
            if url_match_key(target.final_url) != url_match_key(e.href):
                add("hreflang-target-not-200", f"{e.lang} {e.href} redirects to {target.final_url}")
                continue
            if target.noindex:
                add("hreflang-target-noindex", f"{e.lang} {e.href}")
            tcanon = target.canonical
            if tcanon and not same_page(tcanon, target.final_url) and not same_page(tcanon, e.href):
                add("hreflang-canonical-conflict", f"{e.href} canonical -> {tcanon}")
            if target.parsed is None:
                report.unchecked += 1
                not_checked.append(
                    NotChecked(
                        check_id="hreflang-no-return-link",
                        reason_code="target-not-html",
                        url=e.href,
                    )
                )
                continue
            if not _points_at(target.entries, (p.url, p.effective_url)):
                add("hreflang-no-return-link", f"{e.lang} {e.href} does not link back to {p.url}")
        report.pages.append(row)

    report.languages = sorted(languages)
    report.probed = list(resolver.probed)
    if missing_x_default and not report.has_x_default:
        first = with_entries[0][0]
        findings.append(
            make_finding(
                "hreflang-no-x-default",
                url=first.url,
                evidence=f"{len(languages)} language(s), no x-default on {missing_x_default} page(s)",
                count=missing_x_default,
            )
        )
        if report.pages:
            report.pages[0].issues.append("hreflang-no-x-default")
    return report, findings, not_checked


def _own_entries(entries: list[HreflangEntry], page: Page) -> list[HreflangEntry]:
    keys = {url_match_key(page.url), url_match_key(page.effective_url)}
    return [e for e in entries if url_match_key(e.href) in keys]


def _adder(findings: list[Finding], row: HreflangPageRow, url: str) -> Any:
    def add(rule: str, evidence: str, count: int | None = None) -> None:
        findings.append(make_finding(rule, url=url, evidence=evidence, count=count))
        if rule not in row.issues:
            row.issues.append(rule)

    return add


@operation("hreflang", output=HreflangReport, requires=Requirements(network=True), timeout_s=60.0)
async def hreflang(ctx: Context, target: str | Page, *, pages: list[Page] | None = None) -> Outcome[HreflangReport]:
    """
    Hreflang codes, return links and targets of the sampled pages (targets outside the sample
    are probed, at most ``MAX_PROBES``).
    """
    if pages is None:
        pages = [await ctx.page(target)]
    report, findings, not_checked = await audit_hreflang(pages, ctx.http)
    return Outcome(data=report, findings=findings, not_checked=not_checked)
