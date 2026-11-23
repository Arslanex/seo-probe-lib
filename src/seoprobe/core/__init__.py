"""The foundation every feature builds on: configuration, session, result contract and infrastructure."""

from seoprobe.core.config import Config, GoogleApp
from seoprobe.core.errors import ErrorCode, ErrorInfo, SeoprobeError
from seoprobe.core.models import (
    AuditMode,
    Category,
    FetchResult,
    Finding,
    Hop,
    Layer,
    NotChecked,
    Preflight,
    Requirements,
    Severity,
)
from seoprobe.core.page import NormalizedUrl, Page, fetch, normalize
from seoprobe.core.result import Event, Meta, RequestLog, Result, Status
from seoprobe.core.session import Session

__all__ = [
    "AuditMode",
    "Category",
    "Config",
    "ErrorCode",
    "ErrorInfo",
    "Event",
    "FetchResult",
    "Finding",
    "GoogleApp",
    "Hop",
    "Layer",
    "Meta",
    "NormalizedUrl",
    "NotChecked",
    "Page",
    "Preflight",
    "RequestLog",
    "Requirements",
    "Result",
    "SeoprobeError",
    "Session",
    "Severity",
    "Status",
    "fetch",
    "normalize",
]
