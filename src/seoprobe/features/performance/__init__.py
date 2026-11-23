"""Page speed: lab measurements in the browser, PageSpeed Insights, CrUX field data and a CO2 estimate."""

from seoprobe.core.feature import Feature

from .carbon import carbon
from .google import check_google_key, field_data, pagespeed
from .lab import lab_metrics
from .models import (
    CarbonEstimate,
    FieldData,
    FieldMetric,
    ImageRecord,
    LabMetrics,
    PagePerf,
    PageSpeedReport,
    WebVitals,
)
from .vitals import web_vitals

FEATURE = Feature(
    name="performance",
    package=__name__,
    operations=[lab_metrics, pagespeed, field_data, web_vitals, carbon],
    depends_on=["page"],
    access_checks={"google": check_google_key},
)

__all__ = [
    "CarbonEstimate",
    "FieldData",
    "FieldMetric",
    "ImageRecord",
    "LabMetrics",
    "PagePerf",
    "PageSpeedReport",
    "WebVitals",
    "carbon",
    "field_data",
    "lab_metrics",
    "pagespeed",
    "web_vitals",
]
