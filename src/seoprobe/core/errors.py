"""
Stable error codes and the two exceptions of the library.

Failures that come from the outside world (network, site, external API) are returned inside a
``Result`` with an ``ErrorCode``. Exceptions are raised only for programming errors and by
``Result.unwrap()``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    # input
    INVALID_URL = "invalid-url"
    UNSUPPORTED_SCHEME = "unsupported-scheme"
    SSRF_BLOCKED = "ssrf-blocked"
    INVALID_INPUT = "invalid-input"
    # network
    DNS_FAILED = "dns-failed"
    CONNECT_FAILED = "connect-failed"
    TLS_ERROR = "tls-error"
    FETCH_TIMEOUT = "fetch-timeout"
    TOO_MANY_REDIRECTS = "too-many-redirects"
    # policy
    RATE_LIMITED = "rate-limited"
    REQUEST_BUDGET_EXCEEDED = "request-budget-exceeded"
    # requirements
    MISSING_EXTRA = "missing-extra"
    MISSING_CREDENTIAL = "missing-credential"
    BROWSER_UNAVAILABLE = "browser-unavailable"
    # external services
    NOT_CONNECTED = "not-connected"
    NO_PERMISSION = "no-permission"
    PROPERTY_NOT_FOUND = "property-not-found"
    API_QUOTA_EXCEEDED = "api-quota-exceeded"
    API_ERROR = "api-error"
    # general
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal-error"


RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.DNS_FAILED,
        ErrorCode.CONNECT_FAILED,
        ErrorCode.FETCH_TIMEOUT,
        ErrorCode.RATE_LIMITED,
        ErrorCode.API_QUOTA_EXCEEDED,
        ErrorCode.API_ERROR,
        ErrorCode.TIMEOUT,
        ErrorCode.BROWSER_UNAVAILABLE,
    }
)

# Fields allowed in ErrorInfo.details: never exception text, resolved addresses or secrets.
SAFE_DETAIL_KEYS = frozenset(
    {"url", "domain", "field", "status", "service", "exception", "missing", "operation", "limit", "reason"}
)


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool = False
    details: dict[str, str] = Field(default_factory=dict)


class OperationError(Exception):
    """Raised inside an operation to end it with an error result (never escapes ``@operation``)."""

    def __init__(self, code: ErrorCode, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.details = {k: str(v) for k, v in (details or {}).items() if k in SAFE_DETAIL_KEYS}

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.code,), {"details": self.details})


class SeoprobeError(Exception):
    """Raised by ``Result.unwrap()`` on an error result."""

    def __init__(self, code: ErrorCode, message: str, result: Any = None) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.result = result

    def __reduce__(self) -> tuple[Any, ...]:  # picklable: process pools re-raise it in the parent
        return (type(self), (self.code, self.message, self.result))


# Stable fetch error strings of the HTTP client -> ErrorCode.
FETCH_ERRORS: dict[str, ErrorCode] = {
    "target-rejected": ErrorCode.SSRF_BLOCKED,
    "invalid-url": ErrorCode.INVALID_URL,
    "unsupported-scheme": ErrorCode.UNSUPPORTED_SCHEME,
    "unreachable": ErrorCode.CONNECT_FAILED,
    "dns-failed": ErrorCode.DNS_FAILED,
    "timeout": ErrorCode.FETCH_TIMEOUT,
    "tls-handshake-failed": ErrorCode.TLS_ERROR,
    "circuit-open": ErrorCode.RATE_LIMITED,
    "fetch-error": ErrorCode.CONNECT_FAILED,
    "redirect-loop": ErrorCode.TOO_MANY_REDIRECTS,
    "budget-exceeded": ErrorCode.REQUEST_BUDGET_EXCEEDED,
}


def fetch_error_code(error: str | None) -> ErrorCode:
    """Map an HTTP client error string ("timeout", "target-rejected: ...") to an ErrorCode."""
    head = (error or "").partition(":")[0].strip()
    return FETCH_ERRORS.get(head, ErrorCode.CONNECT_FAILED)
