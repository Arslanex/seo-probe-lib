"""
CO2e per page view from transferred bytes (Sustainable Web Design model, constants in
``carbon-model.json``). Pure arithmetic: same bytes, same model -> same estimate.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from seoprobe.core.errors import ErrorCode
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.net.browser import playwright_installed
from seoprobe.core.page import Page

from .lab import lab_metrics
from .models import CarbonEstimate, PagePerf


@lru_cache(maxsize=1)
def carbon_model() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(files(__package__).joinpath("carbon-model.json").read_text(encoding="utf-8"))
    return data


def rating_for(grams: float, model: dict[str, Any]) -> str:
    for band in model["rating_bands_g_per_view"]:
        if band["max"] is None or grams <= band["max"]:
            return str(band["rating"])
    return str(model["rating_bands_g_per_view"][-1]["rating"])


def estimate(
    nbytes: int,
    *,
    model: dict[str, Any],
    green_hosting_factor: float = 0.0,
    first_visit_share: float | None = None,
    return_visit_share: float | None = None,
    data_reload_ratio: float | None = None,
) -> dict[str, Any]:
    """
    Energy, per-segment grams and the per-view total for ``nbytes`` transferred.

    total = first x emissions + return x emissions x data_reload_ratio; the operational data-centre
    segment is scaled by (1 - green_hosting_factor).
    """
    visits = model["visits"]
    first = visits["first_visit_share"] if first_visit_share is None else first_visit_share
    ret = visits["return_visit_share"] if return_visit_share is None else return_visit_share
    reload = visits["data_reload_ratio"] if data_reload_ratio is None else data_reload_ratio
    gb = nbytes / model["bytes_per_gb"]
    intensity = model["grid_intensity_g_per_kwh"]["global"]
    view_factor = first + ret * reload
    energy = 0.0
    segments: dict[str, float] = {}
    for kind, parts in model["energy_kwh_per_gb"].items():
        for segment, kwh_per_gb in parts.items():
            kwh = gb * kwh_per_gb
            grams = kwh * intensity
            if kind == "operational" and segment == "data_centre":
                grams *= 1.0 - green_hosting_factor
            energy += kwh
            segments[f"{kind}.{segment}"] = grams * view_factor
    total = sum(segments.values())
    return {
        "energy_kwh": energy,
        "co2_grams": total,
        "rating": rating_for(total, model),
        "segments_g": {k: round(v, 6) for k, v in segments.items()},
        "visits": {"first_visit_share": first, "return_visit_share": ret, "data_reload_ratio": reload},
    }


def _ranks(model: dict[str, Any]) -> list[str]:
    return [str(b["rating"]) for b in model["rating_bands_g_per_view"]]


@operation("carbon", output=CarbonEstimate, requires=Requirements(network=True), timeout_s=180)
async def carbon(
    ctx: Context,
    target: str | Page | PagePerf,
    *,
    green_hosting: bool = False,
    green_hosting_factor: float | None = None,
    first_visit_share: float | None = None,
    return_visit_share: float | None = None,
    data_reload_ratio: float | None = None,
    flag_from_rating: str | None = None,
) -> Outcome[CarbonEstimate]:
    """
    CO2e per page view of ``target``.

    Bytes: a measured ``PagePerf`` as given; else a desktop lab measurement when the browser can be
    used; else the HTML document only (``partial``, sub-resources not measured).
    ``green_hosting=True`` means a verified green host (factor 1.0); ``green_hosting_factor``
    (0-1) overrides it. Visit shares default to the model's values; first + return must be 1.
    """
    model = carbon_model()
    flag = flag_from_rating or str(model["thresholds"]["flag_from_rating"])
    ranks = _ranks(model)
    if flag not in ranks:
        ctx.fail(ErrorCode.INVALID_INPUT, reason="unknown-rating", limit=", ".join(ranks))
    if green_hosting_factor is None:
        green_hosting_factor = float(model["green_hosting"]["verified_green_factor"]) if green_hosting else 0.0
    shares = {
        "green_hosting_factor": green_hosting_factor,
        "first_visit_share": first_visit_share,
        "return_visit_share": return_visit_share,
        "data_reload_ratio": data_reload_ratio,
    }
    for name, value in shares.items():
        if value is not None and not 0.0 <= value <= 1.0:
            ctx.fail(ErrorCode.INVALID_INPUT, reason=f"{name.replace('_', '-')}-out-of-range", limit="0-1")
    visits = model["visits"]
    first = visits["first_visit_share"] if first_visit_share is None else first_visit_share
    ret = visits["return_visit_share"] if return_visit_share is None else return_visit_share
    if abs(first + ret - 1.0) > 1e-9:
        ctx.fail(ErrorCode.INVALID_INPUT, reason="visit-shares-not-summing-to-one", limit="1")

    url = target.url if isinstance(target, PagePerf) else target.effective_url if isinstance(target, Page) else target
    nbytes: int | None = None
    if isinstance(target, PagePerf) and not target.error:
        nbytes = target.total_bytes
    elif ctx.config.render and playwright_installed():
        lab = await lab_metrics([url])
        perf = lab.data.items[0] if lab.data is not None and lab.data.items else None
        if perf is not None and not perf.error:
            nbytes = perf.total_bytes
    not_checked: list[NotChecked] = []
    source = "lab"
    if nbytes is None:
        page = await ctx.page(target if isinstance(target, Page) else url)
        nbytes, source = page.html_bytes, "html"
        not_checked.append(ctx.not_checked("page-carbon-high", "sub-resources-not-measured", url))

    est = estimate(
        nbytes,
        model=model,
        green_hosting_factor=green_hosting_factor,
        first_visit_share=first,
        return_visit_share=ret,
        data_reload_ratio=data_reload_ratio,
    )
    data = CarbonEstimate(
        url=url,
        bytes=nbytes,
        bytes_source=source,  # type: ignore[arg-type]
        green_hosting=green_hosting_factor > 0,
        green_hosting_factor=green_hosting_factor,
        **est["visits"],
        energy_kwh=round(est["energy_kwh"], 9),
        co2_grams=round(est["co2_grams"], 6),
        rating=est["rating"],
        segments_g=est["segments_g"],
        flag_from_rating=flag,
        model=str(model["model"]),
        model_version=str(model["version"]),
        constants={k: v for k, v in model.items() if k not in ("model", "version")},
    )
    findings: list[Finding] = []
    if source == "lab" and ranks.index(data.rating) >= ranks.index(flag):
        findings.append(
            ctx.finding(
                "page-carbon-high",
                url=url,
                evidence=f"{data.co2_grams:.3f} g CO2e per view, rating {data.rating} "
                f"(flagged from {flag}); {nbytes} bytes ({source}), SWD v{data.model_version}",
            )
        )
    return Outcome(data, findings, not_checked)
