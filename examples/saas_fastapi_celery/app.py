"""
The FastAPI side of a SaaS built on seoprobe.

    uvicorn app:app --port 8000

* One seoprobe Session per API process, opened in the lifespan and shared by every request: it
  keeps the connection pool, the politeness limits per site and (if used) the browser warm.
* Quick, synchronous checks run right here; full audits go to the Celery worker (worker.py).
* Each tenant connects their own Google account; tokens live encrypted in Postgres.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, NoReturn

import redis.asyncio as redis
from common import PUBLIC_URL, QUICK_CHECK_TIMEOUT_S, REDIS_URL, TOKEN_KEY, connect, fernet, seoprobe_config
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from stores import Audits, PendingAuthRequests, PostgresTokenStore
from worker import run_audit

import seoprobe
from seoprobe import ErrorCode


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await connect()
    client = redis.from_url(REDIS_URL)
    token_store = PostgresTokenStore(pool, fernet()) if TOKEN_KEY else None
    # The session opens on the server's event loop, the same loop the request handlers run on.
    async with seoprobe.Session(seoprobe_config(), token_store=token_store) as session:
        app.state.session = session
        app.state.audits = Audits(pool)
        app.state.pending = PendingAuthRequests(client)
        yield
    await client.aclose()
    await pool.close()


app = FastAPI(title="SEO checks (seoprobe example)", lifespan=lifespan)

# --------------------------------------------------------------------------- errors

INPUT_ERRORS = {ErrorCode.INVALID_URL, ErrorCode.UNSUPPORTED_SCHEME, ErrorCode.SSRF_BLOCKED, ErrorCode.INVALID_INPUT}
SETUP_ERRORS = {
    ErrorCode.MISSING_EXTRA,
    ErrorCode.MISSING_CREDENTIAL,
    ErrorCode.BROWSER_UNAVAILABLE,
    ErrorCode.NOT_CONNECTED,
    ErrorCode.NO_PERMISSION,
    ErrorCode.PROPERTY_NOT_FOUND,
}


def fail(result: seoprobe.Result) -> NoReturn:  # type: ignore[type-arg]
    """seoprobe's stable error codes become HTTP statuses your frontend can act on."""
    assert result.error is not None
    code = result.error.code
    if code in INPUT_ERRORS:
        status = 400
    elif code in SETUP_ERRORS:
        status = 409
    elif code == seoprobe.ErrorCode.API_QUOTA_EXCEEDED:
        status = 429
    elif code == seoprobe.ErrorCode.INTERNAL_ERROR:
        status = 500  # a bug in seoprobe, not a problem with the site
    elif "timeout" in code:
        status = 504
    else:
        status = 502
    raise HTTPException(
        status, detail={"code": code.value, "message": result.error.message, "retryable": result.error.retryable}
    )


# --------------------------------------------------------------------------- audits (worker)


class AuditRequest(BaseModel):
    url: str
    mode: Literal["page", "site"] = "page"
    max_pages: int | None = Field(default=None, ge=1, le=50)


@app.post("/tenants/{tenant}/audits", status_code=202)
async def start_audit(tenant: str, body: AuditRequest, request: Request) -> dict[str, Any]:
    session: seoprobe.Session = request.app.state.session
    # normalize makes no request: reject what can never be audited before queueing anything
    checked = (await seoprobe.normalize(body.url, session=session)).unwrap()
    if not checked.allowed or checked.url is None:
        raise HTTPException(400, detail={"code": checked.reason, "message": "this address cannot be audited"})
    audit_id = await request.app.state.audits.create(tenant, checked.url, body.mode, body.max_pages)
    run_audit.delay(audit_id)
    return {"id": audit_id, "status_url": f"/audits/{audit_id}"}


@app.get("/audits/{audit_id}")
async def audit_status(audit_id: str, request: Request) -> dict[str, Any]:
    row = await request.app.state.audits.get(audit_id)
    if row is None:
        raise HTTPException(404)
    out: dict[str, Any] = {"id": row.id, "url": row.url, "status": row.status, "stage": row.stage}
    if row.status == "done" and row.result:
        data = row.result["data"]
        out["score"] = (data.get("scores") or {}).get("overall")
        out["findings"] = len(data.get("findings") or [])
        out["report_url"] = f"/audits/{audit_id}/report"
    if row.status == "failed":
        out["error_code"] = row.error_code
    return out


@app.get("/audits/{audit_id}/report")
async def audit_report(audit_id: str, request: Request, lang: Literal["en", "tr"] = "en") -> dict[str, Any]:
    audits: Audits = request.app.state.audits
    row = await audits.get(audit_id)
    if row is None:
        raise HTTPException(404)
    if row.status != "done" or not row.result:
        raise HTTPException(409, detail={"code": "not-ready", "status": row.status})
    audit = seoprobe.Audit.model_validate(row.result["data"])
    prev_row = await audits.previous(row)
    previous = seoprobe.Audit.model_validate(prev_row.result["data"]) if prev_row and prev_row.result else None
    # Building a report makes no request: it is fine on the API, from stored data.
    res = await seoprobe.report.build(audit, lang=lang, previous=previous, session=request.app.state.session)
    if not res.ok:
        fail(res)
    return res.unwrap().model_dump(mode="json")


# --------------------------------------------------------------------------- a quick synchronous check


@app.get("/check")
async def quick_check(url: str, request: Request) -> dict[str, Any]:
    """One page, the on-page rules only: a second or two, fine inside a request."""
    res = await seoprobe.onpage(url, session=request.app.state.session, timeout_s=QUICK_CHECK_TIMEOUT_S)
    if not res.ok:
        fail(res)
    return {
        "url": url,
        "status": res.status.value,
        "findings": [{"id": f.id, "severity": f.severity, "message": f.message} for f in res.findings],
    }


# --------------------------------------------------------------------------- Google, per tenant


@app.get("/tenants/{tenant}/google")
async def google_connection(tenant: str, request: Request) -> dict[str, Any]:
    res = await seoprobe.gsc.connection(account=tenant, session=request.app.state.session)
    return res.unwrap().model_dump()


@app.get("/tenants/{tenant}/google/connect")
async def google_connect(tenant: str, request: Request) -> dict[str, str]:
    """Step 1: the URL to send the tenant to. The frontend redirects the browser there."""
    res = await seoprobe.gsc.auth_url(
        f"{PUBLIC_URL}/google/callback", account=tenant, services=["gsc", "ga4"], session=request.app.state.session
    )
    if not res.ok:
        fail(res)
    auth = res.unwrap()
    await request.app.state.pending.save(auth)  # kept until Google sends the tenant back
    return {"authorize_url": auth.url}


@app.delete("/tenants/{tenant}/google")
async def google_disconnect(tenant: str, request: Request) -> dict[str, Any]:
    """Revoke the tenant's grant at Google and delete their encrypted tokens."""
    res = await seoprobe.gsc.disconnect(account=tenant, session=request.app.state.session)
    if not res.ok:
        fail(res)
    return res.unwrap().model_dump()


@app.get("/google/callback")
async def google_callback(code: str, state: str, request: Request) -> dict[str, Any]:
    """Step 2: Google redirects here; the tokens go to the tenant's encrypted row."""
    pending = await request.app.state.pending.take(state)
    if pending is None:
        raise HTTPException(400, detail={"code": "unknown-or-expired-state"})
    res = await seoprobe.gsc.exchange_code(pending, code, state=state, session=request.app.state.session)
    if not res.ok:
        fail(res)
    return res.unwrap().model_dump()


@app.get("/tenants/{tenant}/search-console")
async def search_console(tenant: str, url: str, request: Request) -> dict[str, Any]:
    """Clicks, impressions and top queries of the tenant's site, from their own account."""
    res = await seoprobe.gsc.summary(url, account=tenant, session=request.app.state.session)
    if not res.ok:
        fail(res)
    return res.unwrap().model_dump(mode="json")
