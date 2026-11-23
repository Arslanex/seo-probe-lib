"""
Sitemap discovery and analysis (robots.txt Sitemap lines, common paths, index expansion).

Port of the essentials of the upstream project ``scripts/sitemap_discovery.py`` (MIT):

* formats: ``urlset``, ``sitemapindex``, RSS, Atom, text (``.txt`` / ``text/plain``), each optionally gzip;
* limits: 50 MiB uncompressed, 50,000 URLs per file, 2,048 characters per ``loc``, 16 declared
  sitemaps, 50 children per index (all overridable);
* safety: any ``DOCTYPE`` is refused outright; lxml runs with ``resolve_entities=False``,
  ``no_network=True``, ``load_dtd=False``, ``huge_tree=False`` (XXE / billion-laughs); gzip is
  inflated incrementally under the size cap (bomb protection); every request goes through the
  validated HTTP client (SSRF policy per hop).

Deviations from the source: the common paths are probed only when robots.txt declared no usable
sitemap (fewer requests to the target); sitemap file URLs in evidence are printed without their
query string; the returned ``urls`` list is limited to the audited site (host or its www/apex
counterpart). The ``sitemap-lastmod-uniform`` thresholds (>= 10 dated entries, >= 95 % identical)
are OUR heuristic, not from the source.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlunparse

from lxml import etree

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import CheckOutput, Finding, NotChecked, Requirements
from seoprobe.core.net.client import HttpClient
from seoprobe.core.parse.urls import origin_of
from seoprobe.core.rules.catalog import make_finding

from ..models import SitemapFile, SitemapReport, SitemapUrl
from ..robots import robots

COMMON_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml")
MAX_DECLARED = 16
MAX_SITEMAP_BYTES = 50 * 1024 * 1024
MAX_SITEMAP_URLS = 50_000
MAX_LOC_CHARS = 2048
DEFAULT_MAX_CHILDREN = 50
UNIFORM_MIN_DATED = 10
UNIFORM_SHARE = 0.95
_GZIP_MAGIC = b"\x1f\x8b"
_INFLATE_CHUNK = 1024 * 1024
_ENTRY_TAG = {"urlset": "url", "sitemapindex": "sitemap", "rss": "item", "feed": "entry"}
_TRACKING_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "_ga", "ref"})
_SECRET_PARAM = re.compile(r"(token|session|sig|signature|key|secret|auth|password)", re.I)

# W3C Datetime: YYYY, YYYY-MM, YYYY-MM-DD, or a date with time and zone.
_W3C = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?)?)?")


@dataclass
class SitemapResult:
    urls: list[SitemapUrl] = field(default_factory=list)  # audited-site URLs, deduplicated
    files: list[SitemapFile] = field(default_factory=list)
    found: bool = False  # at least one valid sitemap was read
    truncated: bool = False  # max_urls reached
    output: CheckOutput = field(default_factory=CheckOutput)


# ------------------------------------------------------------------ helpers


def _display_url(url: str) -> str:
    """A sitemap *file* URL without its query string (it can carry credentials)."""
    try:
        p = urlparse(url)
    except ValueError:
        return url[:200]
    return urlunparse((p.scheme, p.netloc.rpartition("@")[2], p.path, "", "", ""))[:200]


def _valid_loc_syntax(value: str) -> bool:
    try:
        p = urlparse(value)
        return p.scheme in ("http", "https") and bool(p.hostname) and p.username is None and p.password is None
    except ValueError:
        return False


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _site_key(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _normalize(url: str) -> str | None:
    """Duplicate-detection key: lower-case host, no default port/fragment/tracking params."""
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower().rstrip(".")
    default = 443 if p.scheme.lower() == "https" else 80
    netloc = f"{host}:{port}" if port and port != default else host
    path = re.sub(r"/{2,}", "/", p.path or "/")
    kept = [
        pair
        for pair in p.query.split("&")
        if pair
        and not pair.split("=", 1)[0].lower().startswith("utm_")
        and pair.split("=", 1)[0].lower() not in _TRACKING_KEYS
    ]
    return urlunparse((p.scheme.lower(), netloc, path, p.params, "&".join(kept), ""))


def parse_w3c_datetime(value: str) -> datetime | None:
    """A comparable UTC time for a W3C Datetime value, or None when invalid."""
    m = _W3C.fullmatch(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, zone = m.groups()
    try:
        moment = datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0), tzinfo=UTC)
        if zone and zone != "Z":
            hh, mm = int(zone[1:3]), int(zone[4:6])
            if hh > 23 or mm > 59:
                return None
            off = timedelta(hours=hh, minutes=mm)
            moment = moment - off if zone[0] == "+" else moment + off
    except (ValueError, OverflowError):
        return None
    return moment


def _gunzip_bounded(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """Inflate ``data`` incrementally; stop at ``max_bytes`` (bomb protection)."""
    d = zlib.decompressobj(wbits=31)
    out = bytearray()
    try:
        pos = 0
        while pos < len(data):
            chunk = d.decompress(data[pos : pos + 65536], max(1, min(_INFLATE_CHUNK, max_bytes + 1 - len(out))))
            pos += 65536
            while True:
                out.extend(chunk)
                if len(out) > max_bytes:
                    return bytes(out[:max_bytes]), True
                if not d.unconsumed_tail:
                    break
                chunk = d.decompress(d.unconsumed_tail, max(1, min(_INFLATE_CHUNK, max_bytes + 1 - len(out))))
        out.extend(d.flush())
    except zlib.error as exc:
        raise ValueError("invalid gzip data") from exc
    return bytes(out), len(out) > max_bytes


def _local(el: Any) -> str:
    tag = el.tag
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _xml_entries(kind: str, root: Any) -> Iterator[tuple[str | None, str | None]]:
    feed = kind in ("rss", "feed")
    for node in root.iter() if feed else root:
        if _local(node) != _ENTRY_TAG[kind]:
            continue
        loc = lastmod = None
        for child in node:
            tag = _local(child)
            text = (child.text or "").strip()
            if kind == "feed":
                if tag == "link" and loc is None and child.get("rel", "alternate") == "alternate":
                    loc = (child.get("href") or "").strip()
                elif tag == "updated" and lastmod is None:
                    lastmod = text
            elif kind == "rss":
                if tag == "link" and loc is None:
                    loc = text
            elif tag == "loc" and loc is None:
                loc = text
            elif tag == "lastmod" and lastmod is None:
                lastmod = text
        yield loc or None, lastmod or None


def classify(content: bytes, content_type: str, url: str) -> tuple[str | None, str | None, Any, list[str] | None]:
    """-> (kind, error, XML root, text lines). Exactly one of ``kind`` / ``error`` is set."""
    if not content.strip():
        return None, "empty response", None, None
    if b"<!DOCTYPE" in content.upper():
        return None, "DOCTYPE is not allowed in sitemap XML", None, None
    stripped = content.removeprefix(b"\xef\xbb\xbf").lstrip()
    if stripped.startswith(b"<"):
        try:
            parser = etree.XMLParser(
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
                recover=False,
                huge_tree=False,
            )
            root = etree.fromstring(content, parser=parser)
        except (etree.XMLSyntaxError, ValueError):
            return None, "invalid XML", None, None
        local = etree.QName(root).localname.lower()
        if local in ("urlset", "sitemapindex", "rss", "feed"):
            return local, None, root, None
        return None, "unsupported XML root", None, None
    path = (urlparse(url).path or "").lower()
    if "text/plain" in content_type.lower() or path.endswith(".txt"):
        lines = [ln.strip() for ln in stripped.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        return "text", None, None, lines
    return None, "unsupported format", None, None


# ------------------------------------------------------------------ discovery


async def discover_sitemaps(
    origin: str,
    fetcher: HttpClient,
    robots_sitemaps: list[str],
    *,
    max_sitemaps: int = MAX_DECLARED,
    max_children: int = DEFAULT_MAX_CHILDREN,
    max_urls: int = MAX_SITEMAP_URLS,
) -> SitemapResult:
    """Find, read and judge the sitemaps of ``origin``. Never raises."""
    site = _site_key(_host(origin))
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    files: list[SitemapFile] = []
    urls: dict[str, SitemapUrl] = {}  # normalized -> first seen
    truncated = False

    # aggregates for findings
    invalid_loc = cross_host = dup_within = dup_across = lastmod_total = lastmod_bad = 0
    lastmod_values: Counter[str] = Counter()
    too_large: list[str] = []
    invalid_files: list[str] = []
    nested: list[str] = []
    child_failed: list[str] = []
    any_net_ok = False
    seen_by_file: dict[str, int] = {}

    async def read(url: str, *, declared: bool, depth: int) -> list[str]:
        """Fetch one sitemap file; return the child sitemap URLs when it is an index."""
        nonlocal truncated, invalid_loc, cross_host, dup_within, dup_across, lastmod_total, lastmod_bad, any_net_ok
        entry = SitemapFile(url=_display_url(url), declared=declared)
        files.append(entry)
        raw = await fetcher.fetch_bytes(url, max_bytes=MAX_SITEMAP_BYTES + 1)
        entry.final_url = _display_url(raw.final_url)
        entry.status = raw.status
        if raw.error is not None or raw.status is None:
            entry.error = raw.error or "no response"
            return []
        any_net_ok = True
        if raw.status != 200:
            entry.error = f"HTTP {raw.status}"
            return []
        body = raw.body
        entry.bytes = len(body)
        if raw.truncated:
            entry.error = "over 50 MiB"
            too_large.append(entry.url)
            return []
        if body[:2] == _GZIP_MAGIC:
            entry.gzip = True
            try:
                body, big = _gunzip_bounded(body, MAX_SITEMAP_BYTES)
            except ValueError:
                entry.error = "invalid gzip data"
                return []
            if big:
                entry.error = "over 50 MiB uncompressed"
                too_large.append(entry.url)
                return []
        name = raw.final_url[:-3] if raw.final_url.lower().endswith(".gz") else raw.final_url
        kind, error, root, lines = classify(body, raw.headers.get("content-type", ""), name)
        if kind is None:
            entry.error = error
            return []
        entry.kind = kind
        entries: Iterator[tuple[str | None, str | None]] = (
            _xml_entries(kind, root) if root is not None else ((ln, None) for ln in (lines or []))
        )
        is_index = kind == "sitemapindex"
        children: list[str] = []
        count = 0
        file_host = _host(raw.final_url)
        fid = len(files)
        for loc, lastmod in entries:
            count += 1
            if count > max_urls and not is_index:
                truncated = True
                if count == max_urls + 1:
                    too_large.append(entry.url)
                continue
            if lastmod:
                lastmod_total += 1
                if parse_w3c_datetime(lastmod) is None:
                    lastmod_bad += 1
                else:
                    lastmod_values[lastmod] += 1
            if not loc or len(loc) >= MAX_LOC_CHARS or not _valid_loc_syntax(loc):
                invalid_loc += 1
                continue
            if _host(loc) != file_host:
                cross_host += 1
            if is_index:
                children.append(loc)
                continue
            key = _normalize(loc)
            if key is None:
                invalid_loc += 1
                continue
            if _site_key(_host(loc)) != site:
                continue  # not part of the audited site (still counted above)
            if key in urls:
                if seen_by_file.get(key) == fid:
                    dup_within += 1
                else:
                    dup_across += 1
                seen_by_file[key] = fid
                continue
            seen_by_file[key] = fid
            if len(urls) >= max_urls:
                truncated = True
                continue
            urls[key] = SitemapUrl(loc=loc, lastmod=lastmod)
        if is_index:
            entry.child_count = count
            if count > max_urls:
                too_large.append(entry.url)
                truncated = True
        else:
            entry.url_count = count
        if is_index and depth >= 1:
            nested.append(entry.url)
            return []
        return children

    async def read_tree(url: str, *, declared: bool) -> None:
        """Read one sitemap; when it is an index, read its children one level down."""
        children = await read(url, declared=declared, depth=0)
        if len(children) > max_children:
            not_checked.append(
                NotChecked(
                    check_id="sitemap-child-unreachable",
                    reason_code="partial-sample",
                    url=_display_url(url),
                )
            )
        for child in children[:max_children]:
            before = len(files)
            await read(child, declared=False, depth=1)  # depth 1: a nested index is only recorded
            entry = files[before]
            if entry.kind is None and entry.error:
                child_failed.append(entry.url)

    # robots.txt declarations first; the common paths only when none yielded a valid sitemap
    declared = [u for u in dict.fromkeys(robots_sitemaps) if _valid_loc_syntax(u)][:max_sitemaps]
    for u in declared:
        await read_tree(u, declared=True)
    if not any(f.kind for f in files):
        for path in COMMON_PATHS:
            cand = origin.rstrip("/") + path
            if cand in declared:
                continue
            await read_tree(cand, declared=False)
            if any(f.kind for f in files):
                break

    valid = [f for f in files if f.kind]
    found = bool(valid)

    # ---- findings
    def hard_invalid(f: SitemapFile) -> bool:
        """A declared sitemap that failed for a reason other than size (size has its own finding)."""
        return f.declared and bool(f.error) and f.kind is None and not (f.error or "").startswith("over 50")

    invalid_files += [f"{f.url} ({f.error})" for f in files if hard_invalid(f)]
    if not found and not invalid_files:
        if files and not any_net_ok:
            not_checked.append(NotChecked(check_id="sitemap-missing", reason_code="unreachable", url=origin))
        elif not too_large:
            findings.append(
                make_finding("sitemap-missing", url=origin, evidence="none in robots.txt or at common paths")
            )
    if invalid_files:
        findings.append(make_finding("sitemap-invalid", url=origin, evidence="; ".join(invalid_files)))
    if too_large:
        findings.append(make_finding("sitemap-too-large", url=origin, evidence=", ".join(dict.fromkeys(too_large))))
    if nested:
        findings.append(make_finding("sitemap-nested-index", url=origin, evidence=", ".join(dict.fromkeys(nested))))
    if child_failed:
        findings.append(
            make_finding(
                "sitemap-child-unreachable",
                url=origin,
                count=len(child_failed),
                evidence=", ".join(child_failed),
            )
        )
    if invalid_loc:
        findings.append(make_finding("sitemap-invalid-urls", url=origin, count=invalid_loc))
    if cross_host:
        findings.append(make_finding("sitemap-cross-host-urls", url=origin, count=cross_host))
    if dup_within + dup_across:
        findings.append(
            make_finding(
                "sitemap-duplicate-urls",
                url=origin,
                count=dup_within + dup_across,
                evidence=f"within a file: {dup_within}, across files: {dup_across}",
            )
        )
    if lastmod_bad:
        findings.append(make_finding("sitemap-lastmod-invalid", url=origin, count=lastmod_bad))
    good_dated = sum(lastmod_values.values())
    if good_dated >= UNIFORM_MIN_DATED:
        value, uses = lastmod_values.most_common(1)[0]
        if uses / good_dated >= UNIFORM_SHARE:
            findings.append(
                make_finding(
                    "sitemap-lastmod-uniform",
                    url=origin,
                    evidence=f"{uses}/{good_dated} entries have lastmod {value}",
                    count=uses,
                )
            )

    facts = {
        "found": found,
        "files": [f.model_dump() for f in files],
        "urls": len(urls),
        "truncated": truncated,
        "lastmod_entries": lastmod_total,
    }
    return SitemapResult(
        urls=list(urls.values()),
        files=files,
        found=found,
        truncated=truncated,
        output=CheckOutput(findings=findings, not_checked=not_checked, facts=facts),
    )


@operation("sitemap", output=SitemapReport, requires=Requirements(network=True), timeout_s=120.0)
async def sitemap(ctx: Context, url: str, *, robots_sitemaps: list[str] | None = None) -> Outcome[SitemapReport]:
    """Find, read and judge the sitemaps of the site of ``url`` (robots.txt declarations first)."""
    if robots_sitemaps is None:
        rb = await robots(url)
        robots_sitemaps = list(rb.data.sitemaps) if rb.ok and rb.data is not None else []
    res = await discover_sitemaps(origin_of(url), ctx.http, robots_sitemaps)
    report = SitemapReport(
        urls=res.urls, files=res.files, found=res.found, truncated=res.truncated, facts=res.output.facts
    )
    return Outcome(data=report, findings=list(res.output.findings), not_checked=list(res.output.not_checked))
