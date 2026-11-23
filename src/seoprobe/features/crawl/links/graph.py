"""
Link analysis: link graph, link health, click depth, orphan pages.

What it measures
----------------
* every countable link of every sampled page (fragment-only, ``mailto:``, ``tel:``,
  ``javascript:`` are skipped), split into internal and external with ``in_scope``;
* the target status of each link. A target that is itself a sampled page is answered from the
  sample (no network). Other targets are probed gently with ``fetcher.fetch_bytes`` (first
  2 KiB only), internal ones up to ``max_internal_checks`` and external ones up to
  ``max_external_checks`` *unique* URLs per audit (links in the main content first).
  Every probe goes through the fetcher, i.e. through the SSRF policy. Probes stop at
  ``deadline`` (the link-check time budget): targets not answered by then are ``unchecked``
  with ``error="time-budget"``, never broken. Like the count limits, the budget leaves the rules
  judged over the targets that were checked (``not_checked``: ``link-health``); only a kind with
  no checked target at all makes its rule ``not_checked``;
* a **sampled link graph**: inlinks per sampled page, click depth from the entry page (BFS over
  sampled pages only) and sitemap membership.

What it does not claim
----------------------
* Depth and orphan status are computed over the *sample*. A page that is reachable in the real
  site by a shorter path may look deeper here, and a sitemap URL linked only from an unsampled
  page looks orphaned. Hence ``deep-page`` and ``orphan-page`` are heuristics, and orphan
  detection only runs when the sample covers at least ``ORPHAN_MIN_COVERAGE`` of the in-scope
  sitemap URLs (otherwise it is reported as ``not_checked``: ``sample-too-small``).
* ``broken`` means: the target answered 4xx/5xx or could not be reached (``unreachable``,
  ``timeout``, TLS failure, redirect loop). 401/403/429 (and LinkedIn's 999) are *not* broken: they
  are usually bot blocking (recorded with ``error="blocked"``). A target refused by the SSRF
  policy is ``error="target-rejected"``: never broken, never fetched.
* Counts in the summary are over *unique targets* for ``checked``, ``skipped``, ``broken`` and
  ``redirecting`` (``checked`` = a status was obtained, from the sample or by a probe;
  ``skipped`` = unique targets without a status because of a limit, robots.txt, the circuit
  breaker or the SSRF policy).
* Thresholds (150 links, 20% nofollow, depth 3) are heuristics, not search engine rules.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from seoprobe.core.feature import TIME_BUDGET, Context, Outcome, operation
from seoprobe.core.models import AuditMode, CheckOutput, Finding, NotChecked, Requirements
from seoprobe.core.net.client import HttpClient
from seoprobe.core.page import Page
from seoprobe.core.parse.urls import in_scope, is_infrastructure_path, normalize_url, url_key
from seoprobe.core.rules.catalog import make_finding
from seoprobe.features.site import RobotsReport, RobotsRules, can_fetch

from ..models import (
    BLOCKED_STATUSES,
    LinkRecord,
    LinkSummary,
    PageLinkStats,
    Verdict,
    classify_link,
)

MAX_RECORDS = 2000
MAX_FINDINGS_PER_PAGE_AND_RULE = 25
TOO_MANY_LINKS = 150  # heuristic, not a search engine limit
NOFOLLOW_SHARE = 0.20  # heuristic
NOFOLLOW_MIN_LINKS = 5
DEEP_PAGE_DEPTH = 3  # the upstream project seo-technical: "important pages within 3 clicks of homepage"
ORPHAN_MIN_COVERAGE = 0.9
ANCHOR_MAX = 120


@dataclass
class LinksResult:
    summary: LinkSummary
    output: CheckOutput


@dataclass
class _Status:
    status: int | None
    final_url: str | None
    hops: int
    error: str | None
    verdict: Verdict


def _safe_normalize(url: str) -> str | None:
    try:
        return normalize_url(url)
    except (ValueError, UnicodeError):
        return None


@dataclass
class _Raw:
    """A link before its target status is known."""

    source: str
    target: str
    key: str
    anchor: str
    kind: Literal["internal", "external"]
    rel: list[str]
    zone: Literal["nav", "footer", "main", "other"]
    self_link: bool


@dataclass
class _Collected:
    raws: list[_Raw] = field(default_factory=list)
    per_page_total: dict[str, int] = field(default_factory=dict)


def _collect(pages: list[Page], entry_host: str) -> _Collected:
    out = _Collected()
    for page in pages:
        if page.parsed is None:
            continue
        own = {url_key(page.url), url_key(page.effective_url)}
        n = 0
        for info in page.parsed.links:
            if not info.resolved:
                continue
            target = _safe_normalize(info.resolved)
            if target is None or is_infrastructure_path(target):
                continue
            n += 1
            key = url_key(target)
            kind: Literal["internal", "external"] = "internal" if in_scope(target, entry_host) else "external"
            out.raws.append(
                _Raw(
                    source=page.url,
                    target=target,
                    key=key,
                    anchor=" ".join(info.text.split())[:ANCHOR_MAX],
                    kind=kind,
                    rel=[r.lower() for r in info.rel],
                    zone="nav" if info.zone == "chrome" else "main",
                    self_link=key in own,
                )
            )
        out.per_page_total[page.url] = n
    return out


PLACEHOLDER_HREFS = frozenset({"#", "#!", "javascript:", "javascript:;", "javascript:void(0)", "javascript:void(0);"})
MIN_PLACEHOLDER_LINKS = 2  # one "#" is usually a script trigger; several visible ones are dead


def _placeholder_links(page: Page) -> list[tuple[str, str]]:
    """(anchor text, href) of links that point nowhere; only links with visible text count."""
    if page.parsed is None:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for info in page.parsed.links:
        href = info.href.strip().lower().replace(" ", "")
        text = " ".join(info.text.split())[:ANCHOR_MAX]
        if href in PLACEHOLDER_HREFS and text and (text, href) not in seen:
            seen.add((text, href))
            out.append((text, href))
    return out


def _sample_status(page: Page) -> _Status:
    if page.error or page.status is None:
        code = page.error or "unreachable"
        return _Status(None, page.final_url, 0, code, classify_link(None, code))
    moved = _safe_normalize(page.effective_url) != _safe_normalize(page.url)
    return _Status(page.status, page.final_url, 1 if moved else 0, None, classify_link(page.status, None))


def _status_label(s: _Status) -> str:
    return str(s.status) if s.status is not None else (s.error or "unknown")


async def analyze_links(
    pages: list[Page],
    *,
    entry_url: str,
    sitemap_urls: set[str],
    fetcher: HttpClient,
    robots_rules: RobotsRules | None = None,
    bot_name: str = "",
    mode: str = "site",
    max_external_checks: int = 40,
    max_internal_checks: int = 60,
    concurrency: int = 4,
    deadline: float | None = None,  # event-loop time; None: no time limit of its own
) -> LinksResult:
    entry_host = urlparse(entry_url).hostname or ""
    col = _collect(pages, entry_host)

    # ---- sample index: both the requested and the final URL name a sampled page
    by_key: dict[str, Page] = {}
    for p in pages:
        by_key.setdefault(url_key(p.url), p)
    for p in pages:
        by_key.setdefault(url_key(p.effective_url), p)

    # ---- target statuses: sample first, then bounded probes
    statuses: dict[str, _Status] = {}
    for raw in col.raws:
        if raw.key in by_key and raw.key not in statuses:
            statuses[raw.key] = _sample_status(by_key[raw.key])

    unknown: dict[str, tuple[int, str, Literal["internal", "external"]]] = {}
    for raw in col.raws:
        if raw.key in statuses or raw.self_link:
            continue
        rank = 0 if raw.zone == "main" else 1
        prev = unknown.get(raw.key)
        if prev is None or rank < prev[0]:
            unknown[raw.key] = (rank, raw.target, raw.kind)
    ordered = sorted(unknown.items(), key=lambda kv: (kv[1][0], kv[1][1]))
    to_probe: list[tuple[str, str]] = []
    skipped_keys: list[str] = []
    n_int = n_ext = 0
    for key, (_rank, target, kind) in ordered:
        if kind == "internal":
            if robots_rules is not None and not can_fetch(robots_rules, bot_name or "*", target):
                skipped_keys.append(key)
            elif n_int < max_internal_checks:
                n_int += 1
                to_probe.append((key, target))
            else:
                skipped_keys.append(key)
        elif n_ext < max_external_checks:
            n_ext += 1
            to_probe.append((key, target))
        else:
            skipped_keys.append(key)

    sem = asyncio.Semaphore(max(1, concurrency))

    async def probe(key: str, target: str) -> None:
        try:
            async with asyncio.timeout_at(deadline), sem:
                resp = await fetcher.fetch_bytes(target, max_bytes=2048)
            code = (resp.error or "").split(":")[0].strip() or None
            statuses[key] = _Status(
                resp.status,
                resp.final_url,
                len(resp.redirect_chain or []),
                code,
                classify_link(resp.status, code),
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:  # the link-check time budget ran out: not checked, not broken
            statuses[key] = _Status(None, None, 0, TIME_BUDGET, "unchecked")
        except Exception:  # noqa: BLE001 - a probe must never break the analysis
            statuses[key] = _Status(None, None, 0, "fetch-error", "broken")

    await asyncio.gather(*(probe(k, t) for k, t in to_probe))
    for key in skipped_keys:
        statuses.setdefault(key, _Status(None, None, 0, None, "unchecked"))

    # ---- records
    records: list[LinkRecord] = []
    for raw in col.raws:
        st = statuses.get(raw.key)
        error: str | None = None
        if st is not None:
            error = "blocked" if st.verdict == "blocked" else st.error
        records.append(
            LinkRecord(
                source_url=raw.source,
                target_url=raw.target,
                anchor=raw.anchor,
                kind=raw.kind,
                rel=raw.rel,
                zone=raw.zone,
                target_status=st.status if st else None,
                final_url=st.final_url if st else None,
                redirect_hops=st.hops if st else 0,
                error=error,
                checked=bool(st and st.verdict in ("ok", "blocked", "broken")),
            )
        )

    # ---- graph over the sample: inlinks and click depth
    page_keys: dict[str, set[str]] = {p.url: {url_key(p.url), url_key(p.effective_url)} for p in pages}
    key_to_page: dict[str, str] = {}
    for p in pages:
        for k in page_keys[p.url]:
            key_to_page.setdefault(k, p.url)
    edges: dict[str, set[str]] = {p.url: set() for p in pages}
    inlink_sources: dict[str, set[str]] = {p.url: set() for p in pages}
    out_int: Counter[str] = Counter()
    out_ext: Counter[str] = Counter()
    linked_keys: set[str] = set()
    for raw in col.raws:
        if raw.kind == "internal":
            out_int[raw.source] += 1
        else:
            out_ext[raw.source] += 1
        if raw.self_link:
            continue
        if raw.kind == "internal":
            linked_keys.add(raw.key)
            tgt = key_to_page.get(raw.key)
            if tgt is not None and tgt != raw.source:
                edges[raw.source].add(tgt)
                inlink_sources[tgt].add(raw.source)

    depth: dict[str, int] = {}
    entry_page = key_to_page.get(url_key(entry_url))
    if entry_page is not None:
        depth[entry_page] = 0
        queue: deque[str] = deque([entry_page])
        while queue:
            cur = queue.popleft()
            for nxt in sorted(edges.get(cur, ())):
                if nxt not in depth:
                    depth[nxt] = depth[cur] + 1
                    queue.append(nxt)

    sm_keys = {url_key(u) for u in sitemap_urls}
    stats = [
        PageLinkStats(
            url=p.url,
            outlinks_internal=out_int[p.url],
            outlinks_external=out_ext[p.url],
            inlinks=len(inlink_sources[p.url]),
            depth=depth.get(p.url),
            in_sitemap=(bool(page_keys[p.url] & sm_keys) if sm_keys else None),
        )
        for p in pages
    ]

    # ---- orphans (site mode, only when the sample covers the sitemap well enough)
    out = CheckOutput()
    orphans: list[str] = []
    if mode == "site":
        sm_in_scope: dict[str, str] = {}
        for u in sorted(sitemap_urls):
            nu = _safe_normalize(u)
            if nu and in_scope(nu, entry_host):
                sm_in_scope.setdefault(url_key(nu), nu)
        if not sm_in_scope:
            out.not_checked.append(NotChecked(check_id="orphan-page", reason_code="sitemap-missing"))
        else:
            covered = sum(1 for k in sm_in_scope if k in by_key)
            if covered / len(sm_in_scope) >= ORPHAN_MIN_COVERAGE:
                entry_key = url_key(entry_url)
                orphans = [u for k, u in sm_in_scope.items() if k not in linked_keys and k != entry_key]
            else:
                out.not_checked.append(NotChecked(check_id="orphan-page", reason_code="sample-too-small"))

    # ---- summary
    int_recs = [r for r in records if r.kind == "internal"]
    ext_recs = [r for r in records if r.kind == "external"]
    unique_status: dict[str, _Status] = {}
    for raw in col.raws:
        if raw.key in statuses:
            unique_status.setdefault(raw.key, statuses[raw.key])
    broken = sum(1 for s in unique_status.values() if s.verdict == "broken")
    redirecting = sum(1 for s in unique_status.values() if s.verdict == "ok" and s.hops >= 1)
    checked = sum(1 for s in unique_status.values() if s.verdict in ("ok", "blocked", "broken"))
    skipped = sum(1 for s in unique_status.values() if s.verdict in ("unchecked", "rejected"))
    # The time budget truncates the probes the way the count limits do: a rule is judged over the
    # targets that were checked. Only when no target of a kind got a status is the rule itself
    # not checked; otherwise ``link-health`` (no rule, not scored) tells that the check was partial.
    kinds_cut: set[str] = set()
    kinds_checked: set[str] = set()
    for link in col.raws:
        link_status = unique_status.get(link.key)
        if link_status is not None and link_status.error == TIME_BUDGET:
            kinds_cut.add(link.kind)
        elif link_status is not None and link_status.verdict in ("ok", "blocked", "broken"):
            kinds_checked.add(link.kind)
    for unmeasured in sorted(kinds_cut - kinds_checked):
        out.not_checked.append(NotChecked(check_id=f"broken-{unmeasured}-link", reason_code=TIME_BUDGET))
    if kinds_cut & kinds_checked:
        out.not_checked.append(NotChecked(check_id="link-health", reason_code=TIME_BUDGET))
    domains = Counter((urlparse(r.target_url).hostname or "").lower() for r in ext_recs)
    kept = records
    if len(records) > MAX_RECORDS:  # keep the problematic links first, then the rest in order
        problematic: list[LinkRecord] = []
        healthy: list[LinkRecord] = []
        for r in records:
            bad_status = (
                r.target_status is not None and r.target_status >= 400 and r.target_status not in BLOCKED_STATUSES
            )
            is_problem = bad_status or r.error not in (None, "blocked") or r.redirect_hops > 0
            (problematic if is_problem else healthy).append(r)
        kept = (problematic + healthy)[:MAX_RECORDS]
    summary = LinkSummary(
        internal_total=len(int_recs),
        external_total=len(ext_recs),
        internal_unique=len({r.key for r in col.raws if r.kind == "internal"}),
        external_unique=len({r.key for r in col.raws if r.kind == "external"}),
        external_domains=[{"domain": d, "count": c} for d, c in domains.most_common(15) if d],
        checked=checked,
        skipped=skipped,
        skipped_time_budget=sum(1 for s in unique_status.values() if s.error == TIME_BUDGET),
        broken=broken,
        redirecting=redirecting,
        nofollow=sum(1 for r in records if "nofollow" in r.rel),
        max_depth=max(depth.values()) if depth else None,
        orphans=orphans,
        pages=stats,
        links=kept,
        truncated=len(records) > MAX_RECORDS,
    )

    out.findings.extend(_findings(pages, col, statuses, stats, orphans, mode))
    out.facts["links"] = {
        "internal_total": summary.internal_total,
        "external_total": summary.external_total,
        "broken": broken,
        "redirecting": redirecting,
        "checked": checked,
        "skipped": skipped,
    }
    return LinksResult(summary=summary, output=out)


def _findings(
    pages: list[Page],
    col: _Collected,
    statuses: dict[str, _Status],
    stats: list[PageLinkStats],
    orphans: list[str],
    mode: str,
) -> list[Finding]:
    found: list[Finding] = []
    by_page: dict[str, list[_Raw]] = {}
    for raw in col.raws:
        by_page.setdefault(raw.source, []).append(raw)
    stat_of = {s.url: s for s in stats}

    for page in pages:
        if page.parsed is None:
            continue
        raws = by_page.get(page.url, [])
        seen: set[str] = set()
        counts: Counter[str] = Counter()
        for raw in raws:
            if raw.self_link:
                continue
            st = statuses.get(raw.key)
            if st is None or raw.key in seen:
                continue
            rule: str | None = None
            evidence = ""
            if st.verdict == "broken":
                rule = "broken-internal-link" if raw.kind == "internal" else "broken-external-link"
                evidence = f"{raw.target} ({_status_label(st)})"
            elif st.verdict == "ok" and st.hops >= 1 and raw.kind == "internal":
                rule = "internal-redirect-link"
                evidence = f"{raw.target} -> {st.final_url or '?'} ({st.hops} hop)"
            if rule is None or counts[rule] >= MAX_FINDINGS_PER_PAGE_AND_RULE:
                continue
            seen.add(raw.key)
            counts[rule] += 1
            found.append(make_finding(rule, url=page.url, evidence=evidence, count=1))

        total = col.per_page_total.get(page.url, 0)
        if total > TOO_MANY_LINKS:
            found.append(make_finding("too-many-links", url=page.url, evidence=f"{total} links", count=total))
        dead = _placeholder_links(page)
        if len(dead) >= MIN_PLACEHOLDER_LINKS:
            found.append(
                make_finding(
                    "placeholder-links",
                    url=page.url,
                    count=len(dead),
                    evidence=", ".join(f"{t} ({h})" for t, h in dead[:5]),
                )
            )
        internal = [r for r in raws if r.kind == "internal" and not r.self_link]
        if mode == "site" and page.status is not None and page.status < 400 and not internal:
            to_self = sum(1 for r in raws if r.kind == "internal" and r.self_link)
            found.append(
                make_finding(
                    "no-internal-links",
                    url=page.url,
                    evidence=f"0 links to other pages of the site ({to_self} to the page itself)",
                )
            )
        if len(internal) >= NOFOLLOW_MIN_LINKS:
            nf = sum(1 for r in internal if "nofollow" in r.rel)
            if nf / len(internal) >= NOFOLLOW_SHARE:
                found.append(
                    make_finding(
                        "nofollow-internal-links",
                        url=page.url,
                        evidence=f"{nf} of {len(internal)} internal links",
                        count=nf,
                    )
                )
        d = stat_of[page.url].depth
        if mode == "site" and d is not None and d > DEEP_PAGE_DEPTH:
            found.append(
                make_finding(
                    "deep-page",
                    url=page.url,
                    evidence=f"depth {d} in the sampled link graph",
                    count=d,
                )
            )
    for url in orphans:
        found.append(make_finding("orphan-page", url=url, evidence="in the sitemap, no internal link from the sample"))
    return found


# ----------------------------------------------------------------------------- operation


@operation("links", output=LinkSummary, requires=Requirements(network=True), timeout_s=300.0)
async def links(
    ctx: Context,
    pages: list[Page],
    *,
    entry_url: str,
    sitemap_urls: list[str] | None = None,
    robots: RobotsReport | None = None,
    mode: AuditMode = AuditMode.SITE,
    link_check_budget_s: float | None = None,
) -> Outcome[LinkSummary]:
    """
    Link graph, link health (bounded probes), click depth and orphan pages of the sampled pages.

    ``link_check_budget_s``: seconds the probes may take (default ``Config.link_check_budget_s``;
    ``math.inf``: as long as the operation's timeout allows). The probes always stop early enough
    to leave the rest of the operation a share of its time.
    """
    res = await analyze_links(
        pages,
        entry_url=entry_url,
        sitemap_urls=set(sitemap_urls or ()),
        fetcher=ctx.http,
        robots_rules=robots.rules if robots else None,
        bot_name=ctx.config.bot_name,
        mode=mode.value,
        max_external_checks=ctx.config.max_external_link_checks,
        max_internal_checks=ctx.config.max_internal_link_checks,
        concurrency=ctx.config.link_check_concurrency,
        deadline=ctx.budget_deadline(
            link_check_budget_s if link_check_budget_s is not None else ctx.config.link_check_budget_s
        ),
    )
    return Outcome(data=res.summary, findings=list(res.output.findings), not_checked=list(res.output.not_checked))
