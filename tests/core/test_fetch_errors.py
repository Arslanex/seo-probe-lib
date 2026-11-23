"""
What an operation returns when its page cannot be fetched: every HTTP client error string
becomes its stable ``ErrorCode``, with the retry flag of ``docs/errors.md`` and no exception text.
"""

from __future__ import annotations

import pytest

import seoprobe
from seoprobe.core.errors import FETCH_ERRORS, RETRYABLE, ErrorCode
from tests import fakes

URL = "https://example.com/"
CLIENT_ERRORS = [
    ("target-rejected", ErrorCode.SSRF_BLOCKED),
    ("unreachable", ErrorCode.CONNECT_FAILED),
    ("dns-failed", ErrorCode.DNS_FAILED),
    ("fetch-error", ErrorCode.CONNECT_FAILED),
    ("timeout", ErrorCode.FETCH_TIMEOUT),
    ("tls-handshake-failed", ErrorCode.TLS_ERROR),
    ("circuit-open", ErrorCode.RATE_LIMITED),
    ("redirect-loop", ErrorCode.TOO_MANY_REDIRECTS),
    ("budget-exceeded", ErrorCode.REQUEST_BUDGET_EXCEEDED),
]


@pytest.mark.parametrize(("client_error", "code"), CLIENT_ERRORS)
async def test_client_error_becomes_its_error_code(client_error: str, code: ErrorCode) -> None:
    async with fakes.session(errors={URL: client_error}) as s:
        res = await seoprobe.fetch(URL, session=s)
    assert res.error is not None and res.error.code is code
    assert res.error.retryable is (code in RETRYABLE)
    assert res.error.details == {"url": URL}  # the client's detail text ("fixture") never leaks
    assert res.error.message and res.error.message != f"error.{code.value}"


def test_every_client_error_string_is_covered_above() -> None:
    assert dict(CLIENT_ERRORS) == {
        k: v for k, v in FETCH_ERRORS.items() if k not in ("invalid-url", "unsupported-scheme")
    }


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.com/file", ErrorCode.UNSUPPORTED_SCHEME),
        ("javascript:alert(1)", ErrorCode.UNSUPPORTED_SCHEME),
        ("not a url", ErrorCode.INVALID_URL),
        ("https://example.com:99999/", ErrorCode.INVALID_URL),
    ],
)
async def test_unusable_input_url_fails_before_any_request(url: str, code: ErrorCode) -> None:
    # (an SSRF refusal stays with the HTTP client, which judges every redirect hop too: test_client.py)
    async with fakes.session() as s:
        res = await seoprobe.fetch(url, session=s)
        requested = s.http.requested  # type: ignore[attr-defined]
    assert res.error is not None and res.error.code is code
    assert requested == []


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/", "invalid-url"),  # normalize_url drops non-web schemes first
        ("http://10.0.0.1/", "ssrf-blocked"),
        ("example.com:99999", "invalid-url"),
    ],
)
async def test_normalize_reports_why_a_url_cannot_be_fetched(url: str, reason: str) -> None:
    res = await seoprobe.normalize(url)
    assert res.ok and res.data is not None
    assert res.data.allowed is False and res.data.reason == reason
