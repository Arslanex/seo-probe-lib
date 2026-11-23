"""
Site setup: redirect matrix, soft-404, custom 404, compression, cache validators, header
disclosure, and well-known files (favicon, manifest, security.txt, ads.txt, humans.txt).

At most ``MAX_REQUESTS`` requests, all through the safe HTTP client. The http->https and www/apex
redirects are *measured* here (the matrix) but not turned into findings: the transport checks
already own ``http-no-https-redirect``, ``http-https-redirect-temporary`` and
``www-not-consolidated``, and one problem must not be reported twice.
``robots.txt`` and the sitemap belong to other checks. HTTP version is not observable through
aiohttp, so it is reported as ``unknown`` rather than guessed.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import secrets
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.client import RawResponse, RequestBudget
from seoprobe.core.page import Page
from seoprobe.core.parse.text import decode_body
from seoprobe.core.parse.urls import origin_of
from seoprobe.core.rules.catalog import make_finding

from .models import Probe, RedirectVariant, SiteSetup

MAX_REQUESTS = 12
_PAGE_BYTES = 200_000
_SMALL = 4096


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_CANON = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-zA-Z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_VERSION = re.compile(r"\d+\.\d+")


def _attrs(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): (m.group(2) or m.group(3) or "") for m in _ATTR.finditer(tag)}


def _link_tags(html: str) -> list[dict[str, str]]:
    return [_attrs(m.group(0)) for m in _CANON.finditer(html[:300_000])]


def _title(html: str) -> str:
    m = _TITLE.search(html[:100_000])
    return re.sub(r"\s+", " ", m.group(1)).strip().lower() if m else ""


def _canonical(html: str, base: str) -> str | None:
    for a in _link_tags(html):
        if "canonical" in a.get("rel", "").lower().split() and a.get("href"):
            return urljoin(base, a["href"])
    return None


def _code(err: str | None) -> str | None:
    return err.partition(":")[0] if err else None


def _norm(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path or "/", "", p.query, ""))


def _sibling(host: str) -> str | None:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None
    except ValueError:
        pass
    if "." not in host:
        return None
    if host.startswith("www.") and host.count(".") >= 2:
        return host[4:]
    return f"www.{host}" if not host.startswith("www.") else None


def _variant(name: str, url: str, raw: RawResponse) -> RedirectVariant:
    chain = raw.redirect_chain or []
    return RedirectVariant(
        name=name,
        variant_url=url,
        status=chain[0].status if chain else raw.status,
        final_url=raw.final_url if raw.error is None else None,
        final_status=raw.status,
        hops=len(chain),
        permanent=(chain[0].status in (301, 308)) if chain else None,
        error=_code(raw.error),
    )


def _is_duplicate(raw: RawResponse, variant_url: str, base_title: str) -> bool:
    """200 on the variant itself, same title as the base page, and no canonical pointing elsewhere."""
    if raw.error or raw.status != 200 or raw.redirect_chain:
        return False
    html = decode_body(raw.body, raw.headers.get("content-type"))
    if not base_title or _title(html) != base_title:
        return False
    canon = _canonical(html, variant_url)
    return canon is None or _norm(canon) == _norm(variant_url)


def _hsts(headers: dict[str, str]) -> dict[str, Any]:
    v = headers.get("strict-transport-security")
    if not v:
        return {"present": False}
    age = re.search(r"max-age\s*=\s*(\d+)", v, re.I)
    low = v.lower()
    return {
        "present": True,
        "max_age": int(age.group(1)) if age else None,
        "include_subdomains": "includesubdomains" in low,
        "preload": "preload" in low,
    }


def _disclosure(headers: dict[str, str]) -> str | None:
    for key in ("server", "x-powered-by"):
        val = headers.get(key, "")
        if _VERSION.search(val) or (key == "x-powered-by" and val):
            return f"{key}: {val[:80]}"
    return None


def _same_host_page(entry: Page, pages: list[Page], origin: str) -> tuple[str, str] | None:
    """(url, title) of a real page whose path is not the root, to test slash/case variants on."""
    host = urlparse(origin).netloc.lower()
    cands: list[tuple[str, str]] = []
    ep = entry.parsed
    cands.append((entry.final_url or entry.url, (ep.title if ep else "") or ""))
    for p in pages:
        if p.status == 200 and p.parsed is not None and not p.error:
            cands.append((p.effective_url, p.parsed.title or ""))
    for url, title in cands:
        u = urlparse(url)
        last = u.path.rsplit("/", 1)[-1]
        if u.netloc.lower() == host and u.path not in ("", "/") and not u.query and "." not in last and title.strip():
            return url, re.sub(r"\s+", " ", title).strip().lower()
    return None


def _probe(url: str, raw: RawResponse, *, ok_if: bool, detail: str | None = None) -> Probe:
    return Probe(
        url=url,
        status=raw.status,
        present=ok_if and raw.error is None,
        detail=detail,
        error=_code(raw.error),
    )


@operation("site_setup", output=SiteSetup, requires=Requirements(network=True), timeout_s=60.0)
async def site_setup(ctx: Context, target: str | Page, *, pages: list[Page] | None = None) -> Outcome[SiteSetup]:
    """Redirect matrix, soft-404, custom 404, response headers and well-known files of the site."""
    entry = await ctx.page(target)
    sample = [entry] if pages is None else pages
    b = RequestBudget(ctx.http, MAX_REQUESTS)
    origin = origin_of(entry.effective_url).rstrip("/")
    op = urlparse(origin)
    host = op.netloc
    out = SiteSetup()
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    headers = entry.headers
    html = entry.content_html

    # ---- plan the (at most 12) requests
    jobs: dict[str, tuple[str, int]] = {}  # key -> (url, max_bytes)
    if op.scheme == "https":
        jobs["http"] = (f"http://{host}/", _SMALL)
    sib = _sibling(op.hostname or "")
    if sib:
        port = f":{op.port}" if op.port else ""
        jobs["www-counterpart"] = (f"{op.scheme}://{sib}{port}/", _SMALL)
    page = _same_host_page(entry, sample, origin)
    if page:
        url, _t = page
        pu = urlparse(url)
        toggled = pu.path[:-1] if pu.path.endswith("/") else pu.path + "/"
        jobs["trailing-slash"] = (urlunparse(pu._replace(path=toggled)), _PAGE_BYTES)
        if pu.path.lower() != pu.path.upper():
            jobs["uppercase"] = (urlunparse(pu._replace(path=pu.path.upper())), _PAGE_BYTES)
    else:
        out.skipped["trailing-slash"] = out.skipped["uppercase"] = "no-non-root-page"
    jobs["index-html"] = (f"{origin}/index.html", _PAGE_BYTES)
    jobs["index-php"] = (f"{origin}/index.php", _PAGE_BYTES)
    probe_url = f"{origin}/{secrets.token_hex(12)}-seoprobe-probe"
    jobs["probe-404"] = (probe_url, 65_536)
    jobs["security"] = (f"{origin}/.well-known/security.txt", _SMALL)
    jobs["ads"] = (f"{origin}/ads.txt", _SMALL)
    jobs["humans"] = (f"{origin}/humans.txt", _SMALL)
    icon_declared = any(
        "icon" in a.get("rel", "").lower().split() or a.get("rel", "").lower() in ("shortcut icon", "apple-touch-icon")
        for a in _link_tags(html)
    )
    if not icon_declared:
        jobs["favicon"] = (f"{origin}/favicon.ico", _SMALL)
    manifest_href = next(
        (a["href"] for a in _link_tags(html) if "manifest" in a.get("rel", "").lower().split() and a.get("href")),
        None,
    )
    if manifest_href:
        jobs["manifest"] = (
            urljoin(entry.final_url or entry.url, manifest_href),
            65_536,
        )

    keys = list(jobs)
    results = dict(
        zip(keys, await asyncio.gather(*(b.fetch_bytes(jobs[k][0], max_bytes=jobs[k][1]) for k in keys)), strict=True)
    )

    # ---- redirect matrix
    for key in (
        "http",
        "www-counterpart",
        "trailing-slash",
        "uppercase",
        "index-html",
        "index-php",
    ):
        if key in results:
            out.redirects.append(_variant(key, jobs[key][0], results[key]))

    # ---- soft 404 / custom 404
    probe = results["probe-404"]
    soft = False
    if probe.error:
        not_checked.append(NotChecked(check_id="soft-404", reason_code="fetch-failed"))
        out.soft_404 = {"probe_url": probe_url, "error": _code(probe.error)}
    else:
        soft = probe.status == 200
        out.soft_404 = {
            "probe_url": probe_url,
            "status": (probe.redirect_chain[0].status if probe.redirect_chain else probe.status),
            "final_url": probe.final_url,
            "final_status": probe.status,
            "detected": soft,
        }
        if soft:
            ev = f"{probe_url} -> {probe.status}"
            if probe.redirect_chain:
                ev += f" via redirect to {probe.final_url}"
            findings.append(make_finding("soft-404", url=origin, evidence=ev))
        elif probe.status in (404, 410):
            body = decode_body(probe.body, probe.headers.get("content-type"))
            bare = len(probe.body) < 600 or "<a " not in body.lower()
            out.custom_404 = not bare
            if bare:
                findings.append(
                    make_finding(
                        "no-custom-404",
                        url=origin,
                        evidence=f"{probe.status}, {len(probe.body)} bytes, no links",
                        confidence="medium",
                    )
                )

    # ---- duplicate URL variants (a soft-404 site answers 200 to everything: skip them)
    if soft:
        for cid in (
            "trailing-slash-duplicate",
            "uppercase-path-duplicate",
            "index-file-duplicate",
        ):
            not_checked.append(NotChecked(check_id=cid, reason_code="soft-404-site"))
    else:
        if page:
            for key, cid in (
                ("trailing-slash", "trailing-slash-duplicate"),
                ("uppercase", "uppercase-path-duplicate"),
            ):
                if key in results and _is_duplicate(results[key], jobs[key][0], page[1]):
                    findings.append(make_finding(cid, url=jobs[key][0], evidence=f"{jobs[key][0]} -> 200"))
        home_title = _title(html) or (entry.parsed.title.strip().lower() if entry.parsed else "")
        idx = [k for k in ("index-html", "index-php") if _is_duplicate(results[k], jobs[k][0], home_title)]
        if idx:
            findings.append(
                make_finding(
                    "index-file-duplicate",
                    url=jobs[idx[0]][0],
                    evidence=", ".join(jobs[k][0] for k in idx),
                )
            )

    # ---- entry page headers
    enc = headers.get("content-encoding", "").lower()
    ctype = (headers.get("content-type") or entry.content_type or "").lower()
    out.headers = {
        "content_encoding": enc or None,
        "cache_control": headers.get("cache-control"),
        "etag": "etag" in headers,
        "last_modified": "last-modified" in headers,
        "vary": headers.get("vary"),
        "server": headers.get("server"),
        "x_powered_by": headers.get("x-powered-by"),
    }
    out.hsts = _hsts(headers) if op.scheme == "https" else {}
    if entry.status == 200 and "html" in ctype:
        if not enc and entry.html_bytes > 1536:
            findings.append(
                make_finding(
                    "html-not-compressed",
                    url=entry.url,
                    evidence=f"{entry.html_bytes} bytes, no Content-Encoding",
                )
            )
        if "etag" not in headers and "last-modified" not in headers:
            findings.append(
                make_finding(
                    "no-cache-validators",
                    url=entry.url,
                    evidence="no ETag, no Last-Modified",
                    confidence="medium",
                )
            )
    if (disc := _disclosure(headers)) is not None:
        findings.append(make_finding("server-version-disclosed", url=entry.url, evidence=disc))

    # ---- well-known files
    sec = results["security"]
    sec_text = decode_body(sec.body, sec.headers.get("content-type")) if sec.status == 200 else ""
    has_contact = bool(re.search(r"^\s*contact\s*:", sec_text, re.I | re.M))
    out.files["security_txt"] = _probe(jobs["security"][0], sec, ok_if=sec.status == 200 and has_contact)
    if not (sec.error is None and sec.status == 200 and has_contact):
        if sec.error:
            not_checked.append(NotChecked(check_id="security-txt-missing", reason_code="fetch-failed"))
        else:
            findings.append(
                make_finding(
                    "security-txt-missing",
                    url=origin,
                    evidence=f"{jobs['security'][0]} -> {sec.status}",
                )
            )
    for key, name in (("ads", "ads_txt"), ("humans", "humans_txt")):
        r = results[key]
        is_text = r.status == 200 and "html" not in r.headers.get("content-type", "").lower()
        out.files[name] = _probe(jobs[key][0], r, ok_if=is_text)

    if icon_declared:
        out.files["favicon"] = Probe(url=origin, present=True, detail="declared in HTML")
    else:
        fv = results["favicon"]
        has_icon = fv.status == 200 and bool(fv.body) and "html" not in fv.headers.get("content-type", "").lower()
        out.files["favicon"] = _probe(jobs["favicon"][0], fv, ok_if=has_icon)
        if fv.error:
            not_checked.append(NotChecked(check_id="favicon-missing", reason_code="fetch-failed"))
        elif not has_icon:
            findings.append(
                make_finding(
                    "favicon-missing",
                    url=origin,
                    evidence=f"{jobs['favicon'][0]} -> {fv.status}",
                )
            )

    if "manifest" in results:
        mr = results["manifest"]
        detail = None
        valid = False
        if mr.error is None and mr.status == 200:
            try:
                doc = json.loads(decode_body(mr.body, mr.headers.get("content-type")))
                valid = isinstance(doc, dict)
                detail = "ok" if valid else "not a JSON object"
            except ValueError:
                detail = "invalid JSON"
        else:
            detail = f"status {mr.status}" if mr.error is None else _code(mr.error)
        out.files["manifest"] = _probe(jobs["manifest"][0], mr, ok_if=valid, detail=detail)
        if not valid and mr.error is None:
            findings.append(make_finding("manifest-invalid", url=jobs["manifest"][0], evidence=detail))

    out.requests_made = b.used
    return Outcome(data=out, findings=findings, not_checked=not_checked)
