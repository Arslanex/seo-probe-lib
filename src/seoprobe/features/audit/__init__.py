"""The audit pipeline: every analysis of a page or a sampled site in one deterministic run, scored."""

from seoprobe.core.feature import Feature

from .audit import audit_page, audit_site, rerun
from .models import Audit, AuditOptions, AuditStatus, Stage, StageRecord, StageStatus
from .stream import audit_stream

FEATURE = Feature(
    name="audit",
    package=__name__,
    operations=[audit_page, audit_site, rerun],
    depends_on=["page", "content", "site", "crawl", "render", "performance", "search", "offsite"],
)

__all__ = [
    "audit_page",
    "audit_site",
    "audit_stream",
    "rerun",
    "AuditStatus",
    "Stage",
    "StageStatus",
    "StageRecord",
    "AuditOptions",
    "Audit",
]
