"""
The render feature: headless rendering, SPA detection, raw-vs-rendered gap, screenshots, mobile
view and accessibility checks.
"""

from seoprobe.core.feature import Feature

from .accessibility import accessibility
from .mobile import mobile
from .models import (
    A11yNode,
    A11yRule,
    AboveFold,
    AccessibilityReport,
    DeviceView,
    GapDiff,
    GapSignals,
    JsGap,
    MobileReport,
    RenderReport,
    ScreenshotFile,
    Screenshots,
    TapOverlap,
    TapTarget,
)
from .render import render
from .screenshots import screenshots

FEATURE = Feature(
    name="render",
    package=__name__,
    operations=[render, screenshots, mobile, accessibility],
    depends_on=["page"],
)

__all__ = [
    "render",
    "screenshots",
    "mobile",
    "accessibility",
    "ScreenshotFile",
    "AboveFold",
    "Screenshots",
    "GapSignals",
    "GapDiff",
    "JsGap",
    "RenderReport",
    "TapTarget",
    "TapOverlap",
    "DeviceView",
    "MobileReport",
    "A11yNode",
    "A11yRule",
    "AccessibilityReport",
]
