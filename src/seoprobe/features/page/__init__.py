"""Single-page on-page checks and page classification (language, business type)."""

from seoprobe.core.feature import Feature

from .classify import classify
from .models import Classification, OnPageReport
from .onpage import onpage, run_page_checks

FEATURE = Feature(name="page", package=__name__, operations=[onpage, classify], depends_on=[])

__all__ = ["Classification", "OnPageReport", "classify", "onpage", "run_page_checks"]
