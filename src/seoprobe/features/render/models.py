"""Output models of the render feature."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# --- from screenshots.py


class ScreenshotFile(BaseModel):
    device: str
    name: str
    width: int
    height: int
    bytes: int
    data_base64: str | None = None  # the JPEG itself


class AboveFold(BaseModel):
    h1_present: bool = False
    h1_visible: bool = False
    cta_visible: bool = False
    text_chars: int = 0
    horizontal_overflow: bool = False
    overlay_share: float = 0.0
    overlay_label: str = ""


class Screenshots(BaseModel):
    files: list[ScreenshotFile] = Field(default_factory=list)
    above_fold: dict[str, AboveFold] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


# --- from js_gap.py


class GapSignals(BaseModel):
    words: int = 0
    links: int = 0
    schema_types: list[str] = Field(default_factory=list)
    h1: list[str] = Field(default_factory=list)
    title: str | None = None
    canonical: str | None = None
    canonical_all: list[str] = Field(default_factory=list)
    meta_robots: str | None = None


class GapDiff(BaseModel):
    words_only_rendered: int = 0
    words_only_rendered_share: float = 0.0
    links_only_rendered: int = 0
    links_only_rendered_share: float = 0.0
    schema_types_only_rendered: list[str] = Field(default_factory=list)
    h1_only_rendered: list[str] = Field(default_factory=list)
    title_changed: bool = False
    canonical_changed: bool = False
    meta_robots_changed: bool = False


class JsGap(BaseModel):
    raw: GapSignals
    rendered: GapSignals
    diff: GapDiff
    raw_truncated: bool = False
    reasons: list[str] = Field(default_factory=list)
    significant: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


# --- the render operation


class RenderReport(BaseModel):
    url: str
    is_spa: bool = False
    rendered: bool = False
    js_gap: dict[str, Any] | None = None  # JsGap.to_dict() plus a "gap" flag (= significant)
    reasons: list[str] = Field(default_factory=list)
    html: str | None = None  # the rendered DOM
    error: str | None = None  # render error code ("timeout", "render-failed", "target-rejected")


# --- mobile


class TapTarget(BaseModel):
    selector: str
    width: float
    height: float
    text: str = ""


class TapOverlap(BaseModel):
    first: str  # selector
    second: str


class DeviceView(BaseModel):
    visible_text_chars: int = 0
    internal_links: int = 0  # visible links to the same host


class MobileReport(BaseModel):
    url: str
    tap_targets: int = 0
    small_tap_targets: int = 0
    small_tap_share: float = 0.0
    small_tap_examples: list[TapTarget] = Field(default_factory=list)
    overlapping_tap_targets: int = 0  # pairs of targets whose boxes intersect
    overlap_examples: list[TapOverlap] = Field(default_factory=list)
    text_nodes: int = 0
    small_text_nodes: int = 0
    small_text_share: float = 0.0
    horizontal_overflow: bool = False
    scroll_width: int = 0
    viewport_width: int = 0
    viewport_meta: str | None = None
    viewport_meta_present: bool = False
    viewport_device_width: bool = False
    mobile: DeviceView | None = None
    desktop: DeviceView | None = None
    text_ratio: float | None = None  # mobile / desktop visible text length
    link_ratio: float | None = None  # mobile / desktop internal link count
    thresholds: dict[str, float] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)  # device -> "render-failed: ..."


# --- accessibility


class A11yNode(BaseModel):
    selector: str
    html: str = ""  # at most 200 characters


class A11yRule(BaseModel):
    id: str  # axe rule id
    impact: str | None = None  # critical | serious | moderate | minor
    tags: list[str] = Field(default_factory=list)
    wcag: list[str] = Field(default_factory=list)  # success criteria, e.g. "1.4.3"
    help: str = ""
    help_url: str = ""
    nodes_count: int = 0
    nodes: list[A11yNode] = Field(default_factory=list)  # capped


class AccessibilityReport(BaseModel):
    url: str
    standard: str
    tags: list[str] = Field(default_factory=list)  # axe tags that were run
    axe_version: str = ""
    violations: list[A11yRule] = Field(default_factory=list)
    incomplete: list[A11yRule] = Field(default_factory=list)  # needs manual review
    passes: int = 0
    inapplicable: int = 0
    violations_by_impact: dict[str, int] = Field(default_factory=dict)  # impact -> rule count
    error: str | None = None
