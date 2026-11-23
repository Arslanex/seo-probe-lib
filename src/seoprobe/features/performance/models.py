"""Output models of the performance feature."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ImageRecord(BaseModel):
    page_url: str
    src: str
    served_format: str | None = None  # webp | avif | jpeg | png | gif | svg | unknown
    bytes: int | None = None  # transferred (or content-length)
    natural_w: int | None = None
    natural_h: int | None = None
    displayed_w: int | None = None
    displayed_h: int | None = None
    has_alt: bool = True
    lazy: bool = False
    above_fold: bool = False
    category: Literal["thumbnail", "content", "hero"] = "content"
    issues: list[str] = Field(default_factory=list)  # check ids


class PagePerf(BaseModel):
    """Lab measurement of one page (this machine and network; not field data)."""

    url: str
    device: Literal["desktop", "mobile"] = "desktop"
    ttfb_ms: int | None = None
    fcp_ms: int | None = None
    lcp_ms: int | None = None
    cls: float | None = None
    tbt_ms: int | None = None  # sum of long-task time above 50 ms (proxy for TBT)
    dom_content_loaded_ms: int | None = None
    load_ms: int | None = None
    total_requests: int = 0
    total_bytes: int = 0  # transferred
    html_bytes: int = 0
    by_type: dict[str, dict[str, int]] = Field(default_factory=dict)  # {"script": {"count": 3, "bytes": 1234}}
    third_party: list[dict[str, Any]] = Field(default_factory=list)  # [{host, requests, bytes}]
    render_blocking: int = 0
    uncompressed_text: list[str] = Field(default_factory=list)  # URLs
    no_cache_static: list[str] = Field(default_factory=list)  # URLs
    mobile: dict[str, Any] | None = None  # {horizontal_overflow, small_font_share, small_tap_targets, viewport_meta}
    images: list[ImageRecord] = Field(default_factory=list)
    error: str | None = None  # "render-failed: ..." when the page could not be measured


class FieldMetric(BaseModel):
    metric: Literal["lcp", "inp", "cls", "fcp", "ttfb"]
    p75: float | None = None
    rating: Literal["good", "needs-improvement", "poor"] | None = None
    good_share: float | None = None
    needs_improvement_share: float | None = None
    poor_share: float | None = None


class FieldData(BaseModel):
    """Real-user data (Chrome UX Report, 28-day p75). Public data; no site owner authorisation."""

    source: Literal["crux"] = "crux"
    scope: Literal["url", "origin"] | None = None
    form_factor: str | None = None  # PHONE | DESKTOP | None (all)
    period: dict[str, str] | None = None  # {first, last}
    metrics: list[FieldMetric] = Field(default_factory=list)
    error: str | None = None  # "no-data" when the site has too little traffic; "no-api-key"; ...


class LabMetrics(BaseModel):
    """Lab measurements of (url, device) pairs; failed pages keep their ``error``."""

    items: list[PagePerf] = Field(default_factory=list)


class PageSpeedReport(BaseModel):
    """PageSpeed Insights (mobile strategy). ``facts``: {"cwv_lcp": {value, source, rating}, ...}."""

    url: str
    measured: bool = False  # False when PageSpeed could not be used (quota, network, error answer)
    facts: dict[str, Any] = Field(default_factory=dict)


class WebVitals(BaseModel):
    """Lab, PageSpeed and field data of one URL together (missing sources stay None or empty)."""

    url: str
    lab: list[PagePerf] = Field(default_factory=list)
    pagespeed: PageSpeedReport | None = None
    field: FieldData | None = None


class CarbonEstimate(BaseModel):
    """CO2e per page view estimated from transferred bytes (Sustainable Web Design model)."""

    url: str
    bytes: int
    bytes_source: Literal["lab", "html"]  # html: the document only, sub-resources not measured
    green_hosting: bool = False
    green_hosting_factor: float = 0.0  # share of hosting on renewable energy (0-1)
    first_visit_share: float = 1.0
    return_visit_share: float = 0.0
    data_reload_ratio: float = 0.0
    energy_kwh: float  # per first view
    co2_grams: float  # per page view, after the model's visit/caching ratio
    rating: str  # A+ ... F
    segments_g: dict[str, float] = Field(default_factory=dict)  # "operational.data_centre" -> grams
    flag_from_rating: str  # the threshold the finding used
    model: str
    model_version: str
    constants: dict[str, Any] = Field(default_factory=dict)  # everything used, as in carbon-model.json
