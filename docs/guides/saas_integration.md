# Building a SaaS on seoprobe

This guide shows how to put seoprobe into a product that serves many customers: a web API, a
background worker, a database for results and per-customer Google connections. Every pattern
here is taken from [examples/saas_fastapi_celery](../../examples/saas_fastapi_celery/README.md),
which was run end to end: Docker Compose starts Postgres and Redis, and the API and the worker
run as two processes.

A term you do not know is probably in the [glossary](../glossary.md).

## The one rule behind everything

A `seoprobe.Session` holds the reusable parts: a connection pool, the politeness limits for each
site, and the browser once one is needed. **A session belongs to the event loop that opened it.**
Any number of tasks on that loop can share it. Another loop or thread gets a clear `RuntimeError`.
Everything below follows from that.

## In the API: one session per process, in the lifespan

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

import seoprobe


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with seoprobe.Session(seoprobe.Config.from_env(), token_store=my_token_store) as session:
        app.state.seoprobe = session
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/check")
async def check(url: str, request: Request):
    result = await seoprobe.onpage(url, session=request.app.state.seoprobe, timeout_s=20)
    ...
```

Uvicorn runs the lifespan on the same loop as the request handlers, so every request can use
`app.state.seoprobe`. With several Uvicorn workers, each process opens its own session.

**What belongs in the API:** quick, single-page operations (`onpage`, `previews`, `techstack`,
`robots`), `normalize` (no network at all), and building reports from stored audits
(`report.build` makes no request).

**What does not:** audits. They visit many URLs and may render pages, so send them to a worker.

## In the worker: one session per job

Celery runs each task in its own `asyncio.run`, which is a new event loop, so the session is
opened inside the task:

```python
@celery_app.task
def run_audit(audit_id: str) -> str:
    return asyncio.run(_run(audit_id))


async def _run(audit_id: str) -> str:
    job = await load_job(audit_id)
    async with seoprobe.Session(seoprobe.Config.from_env(), on_event=save_progress) as session:
        result = await seoprobe.audit_site(
            job.url, max_pages=job.max_pages, account=job.customer_id, session=session,
            timeout_s=600, link_check_budget_s=20, visibility_budget_s=15,
        )
    await save_result(audit_id, result.to_json())
    return result.status.value
```

An audit lasts tens of seconds, so a fresh connection pool per job costs nothing that matters.
The same shape works elsewhere:

| Runner | Where the session lives |
|---|---|
| Celery, RQ | inside the task function, around the call |
| **Arq**, or any asyncio worker that keeps one loop | one session for the worker's lifetime, opened in its startup hook |
| **Temporal** | inside the activity; the workflow only calls the activity |
| a process pool (`ProcessPoolExecutor`) | inside the function the pool runs; never created before the fork |

Set the runner's own time limit **above** the `timeout_s` you give seoprobe, so seoprobe always
finishes and reports (at worst partially) before the runner kills the job. Without `timeout_s`,
the limits are 600 seconds for `audit_page` and 1800 seconds for `audit_site` and
`audit_stream`.

## Storing results

Every result serialises completely:

```python
text = result.to_json()                                     # store this (a jsonb column works well)
result = seoprobe.Result.model_validate_json(text)           # load it back
audit = seoprobe.Audit.model_validate(result.data)          # data is a dict after loading: validate it
```

Store the whole result, not just the score. Everything else can be derived from it later: the
status shown to the user, the report, the comparison with last month:

```python
report = await seoprobe.report.build(audit, previous=last_months_audit, lang="tr", session=session)
```

Results and `SeoprobeError` also pickle, so Celery's pickle serializer and process pools can
carry them directly.

## Showing progress

Pass an `on_event` hook to the session. It receives every operation's `started`, `progress` and
`finished` events. For an audit, the progress events carry the stage that finished. The hook
needs to know which job it reports for, so build it inside the job:

```python
async def _run(audit_id: str) -> str:
    async def save_progress(event: seoprobe.Event) -> None:
        if event.kind == "progress" and event.data.get("status") == "done":
            await db.execute("UPDATE audits SET stage = $2 WHERE id = $1", audit_id, event.data["stage"])

    async with seoprobe.Session(seoprobe.Config.from_env(), on_event=save_progress) as session:
        ...
```

A page audit reports the stages `validate`, `fetch_entry`, `robots`, `render`, `measure`,
`analyze` and `score`. A site audit adds `discovery`, `sample` and `fetch_pages` after
`robots`. A stage that does not apply (no browser, for example) is reported as skipped.

Inside a single request, `seoprobe.audit_stream(...)` yields the same events as an async
iterator, for server-sent events (SSE) or a WebSocket. Pass `mode=seoprobe.AuditMode.SITE` for a
site audit. The last event has `kind="finished"`, and `event.data["result"]` holds the whole
result as a dict. If the client disconnects and you leave the loop, the audit is cancelled:

```python
from fastapi.responses import StreamingResponse


@app.get("/audit/stream")
async def audit_stream(url: str, request: Request):
    async def events():
        async for event in seoprobe.audit_stream(url, session=request.app.state.seoprobe):
            yield f"data: {json.dumps({'kind': event.kind, 'data': event.data})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
```

## Per-customer Google connections

Search Console and GA4 use each customer's own Google account. seoprobe handles OAuth, but it
stores nothing. You give it a **token store**: any object with three async methods.

```python
class PostgresTokenStore:
    def __init__(self, pool, fernet):
        self.pool, self.fernet = pool, fernet

    async def get(self, account: str) -> dict | None:
        raw = await self.pool.fetchval("SELECT record FROM google_tokens WHERE account = $1", account)
        return json.loads(self.fernet.decrypt(raw)) if raw is not None else None

    async def set(self, account: str, record: dict) -> None:
        await self.pool.execute(
            "INSERT INTO google_tokens (account, record) VALUES ($1, $2) "
            "ON CONFLICT (account) DO UPDATE SET record = EXCLUDED.record",
            account, self.fernet.encrypt(json.dumps(record).encode()),
        )

    async def delete(self, account: str) -> None:
        await self.pool.execute("DELETE FROM google_tokens WHERE account = $1", account)
```

- **Use the customer's id as the `account`.** Every `gsc.*` and `ga4.*` call takes an `account=`
  argument.
- **Treat the record as opaque.** It is a JSON-ready dict that seoprobe writes and reads. Store
  it whole, and do not query or change its fields.
- **For tests and a single machine,** seoprobe ships `MemoryTokenStore` and `FileTokenStore`.
  `seoprobe.TokenStore` is the protocol to type your own store against.
- **Encrypt the records.** They hold refresh tokens. A Redis version is in the example's
  [stores.py](../../examples/saas_fastapi_celery/stores.py).

The sign-in flow is stateless, so it works across many API instances:

1. `auth = (await seoprobe.gsc.auth_url(redirect_uri, account=customer_id, services=["gsc", "ga4"], session=s)).unwrap()`.
   `services` sets what the customer is asked to share. The default is `["gsc"]`, Search Console
   only, and then `ga4.*` answers `not-connected`. Keep `auth` (it is JSON) for about ten
   minutes, for example in Redis under `auth.state`, and send the customer to `auth.url`.
2. Google redirects to `redirect_uri` with `code` and `state`.
3. Load the saved request by `state` (take it once), then call
   `await seoprobe.gsc.exchange_code(saved, code, state=state, session=s)`. The tokens land in
   your token store under the customer's id.

**Audits and Google data.** Pass the customer's id to the audit too:
`audit_site(url, account=customer_id, ...)`. The audit then reads Search Console through that
customer's connection, and `rerun` keeps the account.

**Disconnecting.** `await seoprobe.gsc.disconnect(account=customer_id, session=s)` revokes the
grant at Google and deletes the record from your token store. If Google cannot be reached or
rejects the revocation, the record is deleted anyway and a warning is logged.

The revocation covers the whole grant. Google keeps one grant per Google user and OAuth client, so revoking it ends every token of
that user for your app, not just the one in this record. If one Google user is connected to
several customer records (an agency managing many sites, for example), disconnecting one of them
disconnects all of them: their next call answers `not-connected`, and they have to sign in
again. Tell the user this before they disconnect.

## Setting up Google

Search Console and GA4 need an OAuth client of your own, created once in the Google Cloud
console:

1. **Create a project** and enable the **Google Search Console API**. For GA4, also enable the
   **Google Analytics Data API** and the **Google Analytics Admin API**. A call to an API that
   is not enabled answers `no-permission` with `reason="api-not-enabled"` (in a summary:
   `error="api-not-enabled"`), so you can tell it apart from a user without access.
2. **Configure the OAuth consent screen.** The scopes are read-only
   (`webmasters.readonly`, `analytics.readonly`). While the app is in "testing", only the test
   users you list can sign in. To open it to all customers, publish the app; Google may ask you
   to verify it.
3. **Create an OAuth client of type "Web application"** and add your callback URL (the
   `redirect_uri` you pass to `auth_url`) to its authorised redirect URIs. A "Desktop" client is
   meant for scripts on one machine, such as the examples.
4. **Give seoprobe the client,** either through the environment (`SEOPROBE_GOOGLE_CLIENT_ID`,
   `SEOPROBE_GOOGLE_CLIENT_SECRET`, read by `Config.from_env()`) or in code:
   `Config(google_app=seoprobe.GoogleApp(client_id, client_secret))`.

## One configuration, many customers

A session has one `Config`: language, API keys and limits are the same for every call through
it. In practice:

- **Language.** Run audits in one language and build each customer's report in theirs with
  `report.build(audit, lang="tr")`. The stored findings are translated when the report is built.
- **A customer's own API key** (PageSpeed, for example) needs a session with that key. Open one
  per job in the worker, where a session per job is the rule anyway.
- **Shared politeness.** All requests through one session share the per-site limits. When many
  customers check the same popular site at once, they wait for each other, so a quick check on
  that site can take longer than usual.

## Turning error codes into HTTP responses

seoprobe never raises an exception for something that went wrong on the internet. It returns a
stable code (the list is in [errors.md](../errors.md)). This mapping covers all 21 codes:

| Codes | HTTP | Meaning for the user |
|---|---|---|
| `invalid-url`, `unsupported-scheme`, `ssrf-blocked`, `invalid-input` | 400 | fix the address |
| `missing-credential`, `missing-extra`, `browser-unavailable`, `not-connected`, `no-permission`, `property-not-found` | 409 | something must be set up first |
| `api-quota-exceeded` | 429 | a Google or Bing quota is used up; try again later |
| `timeout`, `fetch-timeout` | 504 | try again later |
| `dns-failed`, `connect-failed`, `tls-error`, `too-many-redirects`, `rate-limited`, `request-budget-exceeded`, `api-error` | 502 | the site or a service did not answer as expected |
| `internal-error` | 500 | a bug in seoprobe; the traceback is in your logs at ERROR level |

Pass `error.retryable` on to the frontend: it says whether trying again can help.

An argument of the wrong type (a string where a number belongs, for example) is not an error
code. It raises pydantic's `ValidationError` before anything runs, like any programming error.
Validate request bodies with your own models (FastAPI does this for you) so it cannot reach
seoprobe.

Before you queue an audit, check the address with `seoprobe.normalize(url)`. It makes no request
and always succeeds, so look at the answer, not at `result.ok`:

```python
checked = (await seoprobe.normalize(url, session=session)).unwrap()
if not checked.allowed:
    raise HTTPException(400, detail={"code": checked.reason})   # invalid-url, ssrf-blocked, ...
```

This stops `ftp://...` or `http://10.0.0.1/` before they reach the queue. A host name that
resolves to a private address still passes, because there is no DNS lookup here. The audit then
refuses it with `ssrf-blocked`.

## Deciding how long jobs take

Two per-call budgets decide how long an audit waits for its slow, optional parts:

- `link_check_budget_s`: the link-health checks.
- `visibility_budget_s`: the `visibility` lookups, which ask free public services about the
  domain: its age and registration (RDAP), and its history in the Wayback Machine and Common
  Crawl. What does not arrive in time is missing from the off-site section of the report.

Short budgets suit a "check now" button, and a nightly report can leave them out. The score does
not depend on them. [deployment.md](deployment.md#what-an-audit-costs) has the measured numbers.

## Checklist

- [ ] One session per API process, opened in the lifespan.
- [ ] One session per job in workers (or per worker, for single-loop runners).
- [ ] The runner's time limit is above seoprobe's `timeout_s`.
- [ ] Whole results stored as JSON, and reports built from them on demand.
- [ ] Tokens encrypted at rest, keyed by customer id.
- [ ] OAuth requests kept for a few minutes and used once.
- [ ] Audits get `account=` with the customer's id, and customers can disconnect.
- [ ] Error codes mapped to HTTP, `retryable` passed on.
- [ ] Page-derived text (titles, evidence, link texts) escaped before it is shown as HTML.
- [ ] The deployment requirements in [deployment.md](deployment.md) are met, the egress policy
      first.
