"""
IndexNow: ``seoprobe.indexnow`` (notify Bing, Yandex and the other IndexNow engines of URL changes).

Protocol (https://www.indexnow.org/documentation): the site publishes a key file
``https://<host>/<key>.txt`` whose content is the key; a submission is a JSON POST of at most
10 000 URLs of that host to ``https://api.indexnow.org/indexnow``. The key is public by design
(it is served by the site), so it is not treated as a secret.

``submit`` is the only operation of the package that changes anything outside it. No other
operation, ``audit`` included, ever calls it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from seoprobe.core.errors import ErrorCode, fetch_error_code
from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.net.api import request_json

from .models import IndexNowBatch, IndexNowKeyFile, IndexNowSubmission, IndexNowVerification

ENDPOINT = "https://api.indexnow.org/indexnow"
MAX_URLS_PER_REQUEST = 10_000  # protocol limit
REJECTED_SHOWN = 100
KEY = re.compile(r"[A-Za-z0-9-]{8,128}")  # protocol: 8-128 of a-z, A-Z, 0-9 and "-"
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
RESULTS = {
    200: "ok",
    202: "accepted",  # received, key validation pending
    400: "bad-request",
    403: "key-not-valid",
    422: "unprocessable",  # URLs not of this host, or key does not match the protocol
    429: "rate-limited",
}


def valid_key(key: str) -> bool:
    return bool(KEY.fullmatch(key))


def _key(ctx: Context, key: str) -> str:
    if not valid_key(key):
        ctx.fail(ErrorCode.INVALID_INPUT, service="indexnow", reason="invalid-key")
    return key


def _host(ctx: Context, host: str) -> str:
    h = host.strip().lower().rstrip(".")
    if not _HOST.fullmatch(h):
        ctx.fail(ErrorCode.INVALID_INPUT, service="indexnow", reason="invalid-host")
    return h


def _on_host(url: str, host: str) -> bool:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return False
    return p.scheme in ("http", "https") and (p.hostname or "").rstrip(".") == host


@operation("indexnow.key_file", output=IndexNowKeyFile, requires=Requirements(network=False), timeout_s=5)
async def key_file(ctx: Context, key: str) -> Outcome[IndexNowKeyFile]:
    """The key file to publish at the site root (validates the key format)."""
    k = _key(ctx, key)
    return Outcome(data=IndexNowKeyFile(key=k, path=f"/{k}.txt", content=k))


@operation("indexnow.verify", output=IndexNowVerification, requires=Requirements(network=True), timeout_s=30)
async def verify(ctx: Context, host: str, key: str) -> Outcome[IndexNowVerification]:
    """Whether ``https://<host>/<key>.txt`` is served with the key as its content."""
    h, k = _host(ctx, host), _key(ctx, key)
    url = f"https://{h}/{k}.txt"
    raw = await ctx.http.fetch_bytes(url, max_bytes=4096)
    if raw.error:
        ctx.log.debug("key file fetch failed: %s", raw.error)
        ctx.fail(fetch_error_code(raw.error), url=url, service="indexnow")
    found = raw.status == 200
    text = raw.body.decode("utf-8", "replace").strip().lstrip("﻿") if found else ""
    data = IndexNowVerification(
        host=h,
        key_url=url,
        status=raw.status,
        found=found,
        matches=found and text == k,
        content_type=raw.headers.get("content-type"),
    )
    findings = []
    if not data.matches:
        reason = "content differs from the key" if found else f"HTTP {raw.status}"
        findings.append(
            ctx.finding(
                "indexnow-key-not-verified",
                url=url,
                evidence=f"{url}: {reason}; expected: HTTP 200 with the key as the whole body",
            )
        )
    return Outcome(data=data, findings=findings)


@operation("indexnow.submit", output=IndexNowSubmission, requires=Requirements(network=True), timeout_s=120)
async def submit(
    ctx: Context, host: str, key: str, urls: list[str], *, key_location: str | None = None
) -> Outcome[IndexNowSubmission]:
    """
    Submit changed URLs of ``host`` to IndexNow. **The only write operation of the package**:
    it notifies search engines and must be called explicitly by the user; no other operation,
    ``audit`` included, ever calls it.

    URLs not on ``host`` (exact host, http or https) are rejected, duplicates dropped, at most
    ``Config.max_batch_urls`` are sent (the rest is counted in ``skipped``), in batches of at most
    10 000. Each batch is one POST without retries; its HTTP status is reported per batch and a
    failed batch makes the result partial.
    """
    h, k = _host(ctx, host), _key(ctx, key)
    if key_location is not None and not _on_host(key_location, h):
        ctx.fail(ErrorCode.INVALID_INPUT, service="indexnow", reason="invalid-key-location")
    out = IndexNowSubmission(host=h, key_location=key_location)
    seen: set[str] = set()
    valid: list[str] = []
    for u in urls:
        u = u.strip()
        if u in seen:
            out.duplicates += 1
            continue
        seen.add(u)
        if not _on_host(u, h):
            out.rejected_count += 1
            if len(out.rejected_urls) < REJECTED_SHOWN:
                out.rejected_urls.append(u)
            continue
        valid.append(u)
    limit = ctx.config.max_batch_urls
    out.skipped = max(0, len(valid) - limit)
    valid = valid[:limit]
    session = ctx.http.api_session()
    for i in range(0, len(valid), MAX_URLS_PER_REQUEST):
        chunk = valid[i : i + MAX_URLS_PER_REQUEST]
        body: dict[str, Any] = {"host": h, "key": k, "urlList": chunk}
        if key_location is not None:
            body["keyLocation"] = key_location
        status, _resp, error = await request_json(session, "POST", ENDPOINT, json_body=body, retries=0)
        accepted = status in (200, 202)
        out.batches.append(
            IndexNowBatch(
                index=len(out.batches),
                size=len(chunk),
                status=status,
                result=RESULTS.get(status or 0, "error"),
                accepted=accepted,
                error=None if accepted else (error or f"http-{status}"),
            )
        )
        out.submitted += len(chunk)
        out.accepted += len(chunk) if accepted else 0
    return Outcome(data=out, partial=any(not b.accepted for b in out.batches))


__all__ = ["key_file", "submit", "verify"]
