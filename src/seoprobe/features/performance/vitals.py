"""
Core Web Vitals rating, PageSpeed parsing, findings from lab measurements, and ``web_vitals``.

Thresholds are Google's published CWV values (the upstream project: references/cwv-thresholds.md, scripts/pagespeed_check.py):
LCP good <=2500 ms / poor >4000; INP good <=200 ms / poor >500; CLS good <=0.1 / poor >0.25.
Field data (CrUX, p75) wins over lab data when both exist. INP is the only interactivity metric (no FID).
INP is a field metric and cannot be measured in the lab.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import CheckOutput, Finding, NotChecked, Requirements
from seoprobe.core.net.browser import playwright_installed
from seoprobe.core.rules.catalog import make_finding

from .models import FieldData, PagePerf, WebVitals

THRESHOLDS = {"lcp": (2500.0, 4000.0), "inp": (200.0, 500.0), "cls": (0.1, 0.25)}

# Thresholds (module constants, used by perf_checks). Heuristic ones are marked in the rule.
PAGE_WEIGHT_BYTES = 3 * 1024 * 1024
MAX_REQUESTS = 100
RENDER_BLOCKING_MIN = 4
THIRD_PARTY_REQUESTS = 25
THIRD_PARTY_BYTES_SHARE = 0.40
MOBILE_SMALL_FONT_SHARE = 0.20
MOBILE_TAP_TARGETS_MIN = 3
NO_CACHE_MIN_FILES = 3

# Rules produced by the lab performance / image / mobile measurement (catalog: requires "lab").
PERF_RULE_IDS = [
    "page-weight-heavy",
    "too-many-requests",
    "uncompressed-text-resources",
    "static-assets-no-cache",
    "render-blocking-resources",
    "third-party-heavy",
    "mobile-horizontal-scroll",
    "mobile-tap-targets-small",
    "mobile-font-too-small",
    "image-too-large",
    "image-critically-large",
    "image-oversized-for-display",
    "image-legacy-format",
]
# Every id the lab measurement can produce a verdict for (used for NotChecked on a failed render).
LAB_RULE_IDS = PERF_RULE_IDS + [f"cwv-{m}-{r}" for m in ("lcp", "cls") for r in ("poor", "needs-improvement")]
MOBILE_RULE_IDS = ["mobile-horizontal-scroll", "mobile-tap-targets-small", "mobile-font-too-small"]

Add = Callable[[Finding], None]

_IMAGE_ISSUES = (
    "image-too-large",
    "image-critically-large",
    "image-oversized-for-display",
    "image-legacy-format",
)


def rate(metric: str, value: float) -> str:
    good, poor = THRESHOLDS[metric]
    return "good" if value <= good else ("needs-improvement" if value <= poor else "poor")


def parse_psi(data: dict[str, Any], url: str) -> CheckOutput:
    """Turn a PSI JSON response into findings. Field p75 values win over lab values."""
    out = CheckOutput()
    audits = data.get("lighthouseResult", {}).get("audits", {})
    field = (data.get("loadingExperience") or {}).get("metrics", {})
    values: dict[str, tuple[float, str]] = {}
    lab = {
        "lcp": audits.get("largest-contentful-paint", {}).get("numericValue"),
        "cls": audits.get("cumulative-layout-shift", {}).get("numericValue"),
    }
    for m, v in lab.items():
        if isinstance(v, (int, float)):
            values[m] = (float(v), "lab")
    for m, key in (
        ("lcp", "LARGEST_CONTENTFUL_PAINT_MS"),
        ("inp", "INTERACTION_TO_NEXT_PAINT"),
        ("cls", "CUMULATIVE_LAYOUT_SHIFT_SCORE"),
    ):
        p75 = field.get(key, {}).get("percentile")
        if isinstance(p75, (int, float)):
            values[m] = (float(p75) / 100.0 if m == "cls" else float(p75), "field")
    for m, (v, src) in values.items():
        r = rate(m, v)
        if r != "good":
            unit = "" if m == "cls" else " ms"
            shown = f"{v:.3g}" if m == "cls" else f"{v:.0f}"
            where = "PageSpeed mobile lab" if src == "lab" else "field"
            out.findings.append(make_finding(f"cwv-{m}-{r}", url=url, evidence=f"{m.upper()} {shown}{unit} ({where})"))
    out.facts.update({f"cwv_{m}": {"value": v, "source": s, "rating": rate(m, v)} for m, (v, s) in values.items()})
    if "inp" not in values:
        out.not_checked.append(NotChecked(check_id="cwv-inp-poor", reason_code="field-data-unavailable"))
    return out


# ----------------------------------------------------------------------------- lab checks


def _mib(n: int) -> str:
    return f"{n / 1048576:.1f} MiB"


def _kb(n: int | None) -> str:
    return "? KB" if n is None else f"{n / 1024:.0f} KB"


def _path(url: str) -> str:
    p = urlsplit(url)
    return (p.path or "/") + (f"?{p.query[:30]}" if p.query else "")


def perf_checks(perfs: list[PagePerf]) -> CheckOutput:
    """Turn lab measurements into findings. One finding per (rule, page); desktop wins on a tie."""
    out = CheckOutput()
    seen: set[tuple[str, str]] = set()  # (check id, url): the two devices must not double-count

    def add(f: Finding) -> None:
        key = (f.id, f.url or "")
        if key not in seen:
            seen.add(key)
            out.findings.append(f)

    ordered = sorted(perfs, key=lambda p: (p.url, 0 if p.device == "desktop" else 1))
    measured = 0
    for p in ordered:
        if p.error:
            for rid in LAB_RULE_IDS:
                if p.device == "desktop" and rid in MOBILE_RULE_IDS:
                    continue
                if p.device == "mobile" and rid not in MOBILE_RULE_IDS:
                    continue
                out.not_checked.append(NotChecked(check_id=rid, reason_code="render-failed", url=p.url))
            continue
        measured += 1
        tag = f"{p.device}, lab"
        if p.lcp_ms is not None:
            r = rate("lcp", float(p.lcp_ms))
            if r != "good":
                add(make_finding(f"cwv-lcp-{r}", url=p.url, evidence=f"LCP {p.lcp_ms} ms ({tag})"))
        if p.cls is not None:
            r = rate("cls", float(p.cls))
            if r != "good":
                add(make_finding(f"cwv-cls-{r}", url=p.url, evidence=f"CLS {p.cls:g} ({tag})"))
        if p.device == "desktop":
            _page_findings(p, add)
        else:
            _mobile_findings(p, add)
        _image_findings(p, add)
    if measured:
        for r in ("poor", "needs-improvement"):
            out.not_checked.append(NotChecked(check_id=f"cwv-inp-{r}", reason_code="field-data-unavailable"))
    out.facts["lab_pages_measured"] = measured
    return out


def _page_findings(p: PagePerf, add: Add) -> None:
    if p.total_bytes > PAGE_WEIGHT_BYTES:
        add(
            make_finding(
                "page-weight-heavy",
                url=p.url,
                evidence=f"{_mib(p.total_bytes)}, {p.total_requests} requests",
            )
        )
    if p.total_requests > MAX_REQUESTS:
        add(
            make_finding(
                "too-many-requests",
                url=p.url,
                evidence=f"{p.total_requests} requests",
                count=p.total_requests,
            )
        )
    if p.uncompressed_text:
        n = len(p.uncompressed_text)
        add(
            make_finding(
                "uncompressed-text-resources",
                url=p.url,
                count=n,
                evidence=", ".join(_path(u) for u in p.uncompressed_text[:3]),
            )
        )
    if len(p.no_cache_static) >= NO_CACHE_MIN_FILES:
        n = len(p.no_cache_static)
        add(
            make_finding(
                "static-assets-no-cache",
                url=p.url,
                count=n,
                evidence=", ".join(_path(u) for u in p.no_cache_static[:3]),
            )
        )
    if p.render_blocking >= RENDER_BLOCKING_MIN:
        add(
            make_finding(
                "render-blocking-resources",
                url=p.url,
                count=p.render_blocking,
                evidence=f"{p.render_blocking} blocking resources",
            )
        )
    tp_req = sum(int(t.get("requests", 0)) for t in p.third_party)
    tp_bytes = sum(int(t.get("bytes", 0)) for t in p.third_party)
    share = tp_bytes / p.total_bytes if p.total_bytes else 0.0
    if tp_req >= THIRD_PARTY_REQUESTS or share >= THIRD_PARTY_BYTES_SHARE:
        top = ", ".join(str(t["host"]) for t in p.third_party[:3])
        add(
            make_finding(
                "third-party-heavy",
                url=p.url,
                count=tp_req,
                evidence=f"{tp_req} requests, {share:.0%} of bytes: {top}",
            )
        )


def _mobile_findings(p: PagePerf, add: Add) -> None:
    m = p.mobile
    if not m:
        return
    if m.get("horizontal_overflow"):
        add(
            make_finding(
                "mobile-horizontal-scroll",
                url=p.url,
                evidence="page is wider than the screen (390 px)",
            )
        )
    share = float(m.get("small_font_share") or 0.0)
    if share >= MOBILE_SMALL_FONT_SHARE:
        add(make_finding("mobile-font-too-small", url=p.url, evidence=f"{share:.0%} of text below 12 px"))
    n = int(m.get("small_tap_targets") or 0)
    if n >= MOBILE_TAP_TARGETS_MIN:
        ex = ", ".join(str(x) for x in (m.get("tap_target_examples") or [])[:3])
        add(
            make_finding(
                "mobile-tap-targets-small",
                url=p.url,
                count=n,
                evidence=f"{n} targets under 24 px: {ex}",
            )
        )


def _image_findings(p: PagePerf, add: Add) -> None:
    for issue in _IMAGE_ISSUES:
        # one row per file: a logo repeated in a carousel is still one file to fix
        imgs = list({i.src: i for i in p.images if issue in i.issues}.values())
        if not imgs:
            continue
        top = sorted(imgs, key=lambda i: -(i.bytes or 0))[:3]
        ev = ", ".join(f"{_path(i.src)} ({_kb(i.bytes)})" for i in top)
        add(make_finding(issue, url=p.url, count=len(imgs), evidence=ev))


# ----------------------------------------------------------------------------- web_vitals


def merge_field(not_checked: list[NotChecked], field: FieldData | None) -> list[NotChecked]:
    """Real-user data answers the checks lab data cannot (INP): they are no longer "not measured"."""
    if field is None:
        return not_checked
    got = {m.metric for m in field.metrics}

    def _answered(n: NotChecked) -> bool:
        return n.check_id.startswith("cwv-") and n.check_id.split("-")[1] in got

    return [n for n in not_checked if not (_answered(n) and n.reason_code == "field-data-unavailable")]


def _lab_unavailable(ctx: Context) -> str | None:
    if not ctx.config.render:
        return "render-disabled"
    if not playwright_installed():
        return "missing-extra:browser"
    return None


@operation("web_vitals", output=WebVitals, requires=Requirements(network=True), timeout_s=180)
async def web_vitals(ctx: Context, url: str) -> Outcome[WebVitals]:
    """
    Lab (desktop and mobile), PageSpeed and CrUX data of ``url`` with their findings merged.

    PageSpeed and CrUX run when a Google API key is configured, the lab measurement when the
    browser can be used; a missing source is reported in ``not_checked`` (status ``partial``).
    """
    from .google import field_data, pagespeed  # these modules import this one
    from .lab import lab_metrics

    data = WebVitals(url=url)
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    partial = False

    has_key = bool(ctx.config.google_api_key)
    lab_reason = _lab_unavailable(ctx)
    lab_task = asyncio.ensure_future(lab_metrics([(url, "desktop"), (url, "mobile")])) if lab_reason is None else None
    psi_res, field_res = await asyncio.gather(pagespeed(url), field_data(url)) if has_key else (None, None)
    lab_res = await lab_task if lab_task is not None else None

    if lab_res is not None and lab_res.data is not None:
        data.lab = lab_res.data.items
        findings += lab_res.findings
        not_checked += lab_res.not_checked
    else:
        reason = lab_reason or (lab_res.error.code.value if lab_res is not None and lab_res.error else "lab-failed")
        not_checked += [NotChecked(check_id=rid, reason_code=reason, url=url) for rid in LAB_RULE_IDS]
        partial = True

    if psi_res is not None and psi_res.data is not None:
        data.pagespeed = psi_res.data
        findings += psi_res.findings
        not_checked += psi_res.not_checked
        partial = partial or not psi_res.data.measured
    elif has_key:
        partial = True

    if field_res is not None and field_res.data is not None:
        data.field = field_res.data
        findings += field_res.findings
        not_checked += field_res.not_checked
        partial = partial or bool(field_res.data.error)
    elif has_key:
        partial = True
    not_checked = merge_field(not_checked, data.field)

    if not has_key:
        # "field-data-unavailable" and the missing key both say the field data is absent; the key is
        # the reason to report (and to fix). Other reasons (the lab's "render-disabled") stay.
        keyed = {f"cwv-{m}-poor" for m in ("lcp", "inp", "cls")}
        not_checked = [
            n for n in not_checked if not (n.check_id in keyed and n.reason_code == "field-data-unavailable")
        ]
        not_checked += [
            NotChecked(check_id=rid, reason_code="missing-credential:google_api_key") for rid in sorted(keyed)
        ]
        partial = True
    return Outcome(data, findings, not_checked, partial=partial)
