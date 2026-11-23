"""
PageSpeed Insights v5 and Chrome UX Report (CrUX) API clients (one Google API key serves both).

CrUX: real-user Core Web Vitals (28-day p75). Public data, one API key, no site-owner
authorisation. Sites with too little traffic have no record (that is normal).
INP is the only interactivity metric (FID was removed from CrUX in 2024).

The key travels in the ``X-Goog-Api-Key`` header, never in the URL. The rules, rating thresholds
and PSI response parsing live in ``vitals``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import aiohttp

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import CheckOutput, NotChecked, Requirements
from seoprobe.core.net.api import request_json
from seoprobe.core.net.client import budget_exhausted, record
from seoprobe.core.parse.urls import origin_of
from seoprobe.core.rules.catalog import make_finding

from .models import FieldData, FieldMetric, PageSpeedReport
from .vitals import parse_psi, rate

if TYPE_CHECKING:
    from seoprobe.core.session import Session

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CRUX_ENDPOINT = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
METRICS = {
    "largest_contentful_paint": "lcp",
    "interaction_to_next_paint": "inp",
    "cumulative_layout_shift": "cls",
    "first_contentful_paint": "fcp",
    "experimental_time_to_first_byte": "ttfb",
}
_RATED = ("lcp", "inp", "cls")  # the Core Web Vitals


def _key_header(key: str) -> dict[str, str]:
    return {"X-Goog-Api-Key": key}


# ----------------------------------------------------------------------------- PageSpeed Insights


async def run_psi(url: str, *, session: Any, key: str | None, timeout_s: float = 90.0) -> CheckOutput | None:
    """
    None when PSI cannot be used (no key, quota, network): the category stays 'not measured'.

    Own request code rather than ``request_json``: a PSI run often takes longer than the API
    session's default timeout, and a 90 s request is not retried.
    """
    if not key or budget_exhausted():
        return None
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    shown = f"{PSI_ENDPOINT}?{urlencode(params)}"
    started = time.monotonic()
    try:
        async with session.get(
            PSI_ENDPOINT, params=params, headers=_key_header(key), timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as r:
            if r.status != 200:
                record(shown, status=r.status, started=started, size=0, error=f"http-{r.status}")
                return None
            data = await r.json()
            record(shown, status=r.status, started=started, size=r.content_length or 0, error=None)
            return parse_psi(data, url)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        code = "timeout" if isinstance(exc, TimeoutError) else "unreachable"
        record(shown, status=None, started=started, size=0, error=code)
        return None


# ----------------------------------------------------------------------------- CrUX


def _date(d: dict[str, Any] | None) -> str | None:
    if not d:
        return None
    return f"{d.get('year', 0):04d}-{d.get('month', 0):02d}-{d.get('day', 0):02d}"


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_crux(data: dict[str, Any], scope: str, form_factor: str | None) -> FieldData:
    rec = data.get("record", {})
    out: list[FieldMetric] = []
    for api_name, short in METRICS.items():
        m = rec.get("metrics", {}).get(api_name)
        if not m:
            continue
        p75 = _num(m.get("percentiles", {}).get("p75"))
        hist = m.get("histogram") or []
        shares = [_num(b.get("density")) for b in hist[:3]] + [None, None, None]
        out.append(
            FieldMetric(
                metric=short,  # type: ignore[arg-type]
                p75=p75,
                rating=rate(short, p75) if (p75 is not None and short in _RATED) else None,  # type: ignore[arg-type]
                good_share=shares[0],
                needs_improvement_share=shares[1],
                poor_share=shares[2],
            )
        )
    cp = rec.get("collectionPeriod") or {}
    return FieldData(
        scope=scope,  # type: ignore[arg-type]
        form_factor=form_factor,
        period={"first": _date(cp.get("firstDate")) or "", "last": _date(cp.get("lastDate")) or ""},
        metrics=out,
        error=None if out else "no-data",
    )


async def _query(session: Any, key: str, body: dict[str, Any]) -> tuple[int | None, dict[str, Any]]:
    status, data, _err = await request_json(session, "POST", CRUX_ENDPOINT, headers=_key_header(key), json_body=body)
    return status, data if isinstance(data, dict) else {}


async def fetch_crux(url: str, origin: str, *, session: Any, key: str | None) -> FieldData:
    """Page-level record when the page has one, else origin-level. Never raises."""
    if not key:
        return FieldData(error="no-api-key")
    for scope, body in (("url", {"url": url}), ("origin", {"origin": origin})):
        status, data = await _query(session, key, body)
        if status == 200:
            fd = parse_crux(data, scope, None)
            if fd.metrics:
                return fd
        elif status == 404:
            continue  # no record at this level: try the broader one
        elif status is None:
            return FieldData(error="unreachable")
        elif status in (400, 403):
            return FieldData(error=f"http-{status}")  # bad/unauthorised key or API not enabled
        elif status == 429:
            return FieldData(error="rate-limited")
        else:
            return FieldData(error=f"http-{status}")
    return FieldData(error="no-data")


def field_checks(fd: FieldData | None, url: str) -> CheckOutput:
    """Findings from real-user data. The same rule ids as the lab checks; evidence says "field"."""
    out = CheckOutput()
    if fd is None or fd.error or not fd.metrics:
        return out
    for m in fd.metrics:
        if m.metric in _RATED and m.p75 is not None and m.rating and m.rating != "good":
            unit = "" if m.metric == "cls" else " ms"
            out.findings.append(
                make_finding(
                    f"cwv-{m.metric}-{m.rating}",
                    url=url,
                    evidence=f"{m.metric.upper()} p75 {m.p75:g}{unit} (field, {fd.scope})",
                )
            )
    got = {m.metric for m in fd.metrics}
    for metric in _RATED:
        if metric not in got:
            out.not_checked.append(NotChecked(check_id=f"cwv-{metric}-poor", reason_code="field-data-unavailable"))
    return out


# ----------------------------------------------------------------------------- operations


@operation(
    "pagespeed",
    output=PageSpeedReport,
    requires=Requirements(network=True, credentials=["google_api_key"]),
    timeout_s=90,
)
async def pagespeed(ctx: Context, url: str) -> Outcome[PageSpeedReport]:
    """PageSpeed Insights (mobile) of ``url``; ``measured=False`` when PageSpeed could not be used."""
    remaining = ctx.remaining_s()
    timeout_s = 90.0 if remaining is None else max(1.0, min(90.0, remaining - 1.0))
    out = await run_psi(url, session=ctx.http.api_session(), key=ctx.config.google_api_key, timeout_s=timeout_s)
    if out is None:
        return Outcome(PageSpeedReport(url=url, measured=False), partial=True)
    return Outcome(PageSpeedReport(url=url, measured=True, facts=out.facts), out.findings, out.not_checked)


@operation(
    "field_data",
    output=FieldData,
    requires=Requirements(network=True, credentials=["google_api_key"]),
    timeout_s=45,
)
async def field_data(ctx: Context, url: str, *, origin: str | None = None) -> Outcome[FieldData]:
    """CrUX real-user data of ``url`` (the origin's record when the page has none)."""
    fd = await fetch_crux(url, origin or origin_of(url), session=ctx.http.api_session(), key=ctx.config.google_api_key)
    out = field_checks(fd, url)
    return Outcome(fd, out.findings, out.not_checked, partial=bool(fd.error))


# ----------------------------------------------------------------------------- access check


def _test_code(name: str, error: str | None) -> str:
    """Map a provider's error code onto the few outcomes the settings page can explain."""
    if error is None or error == "no-data":  # no data for the sample site still means the key works
        return "ok"
    if error in ("unauthorized", "http-400", "http-401", "http-403"):
        return f"rejected.{name}"
    if error in ("rate-limited", "unreachable", "timeout"):
        return "unreachable" if error == "timeout" else error
    if error == "no-api-key":
        return "missing"
    return "other"


async def check_google_key(session: Session) -> tuple[bool, str]:
    """Try the configured Google API key with one harmless CrUX request. Never echoes the key."""
    key = session.config.google_api_key
    if not key:
        return False, _test_code("gkey", "no-api-key")
    fd = await fetch_crux("https://web.dev/", "https://web.dev", session=session.http.api_session(), key=key)
    code = _test_code("gkey", fd.error)
    return code == "ok", code
