# SaaS recipe: FastAPI + Celery + Postgres + Redis

A small but complete back end that offers SEO audits to many customers (called `tenant` in the
code and the URLs), built on
seoprobe. It answers the questions that come up first when you put the library into a product:

- Where does the `seoprobe.Session` live in a FastAPI app, and in a worker?
- How do long audits run without blocking the API, and how does the frontend follow them?
- How do customers connect their own Google account, and where do the tokens go?
- How do seoprobe's error codes become HTTP responses?

```text
 browser / frontend
        │  POST /tenants/{t}/audits      GET /audits/{id}      GET /audits/{id}/report
        ▼
 ┌──────────────┐   job id (Celery)   ┌──────────────┐
 │  FastAPI     │ ──────────────────► │ Celery worker│  seoprobe.audit_page / audit_site
 │  app.py      │      Redis          │ worker.py    │  one Session per task
 │  1 Session   │                     └──────┬───────┘
 │  per process │                            │ progress, result (JSON)
 └──────┬───────┘                            ▼
        │  reports, quick checks      ┌──────────────┐
        └───────────────────────────► │  Postgres    │  audits, encrypted Google tokens
                                      └──────────────┘
```

| File | What it holds |
|---|---|
| [app.py](app.py) | the API: lifespan with one shared `Session`, audit jobs, reports, a quick check, the Google sign-in flow |
| [worker.py](worker.py) | the Celery task that runs an audit, writes progress and stores the result |
| [stores.py](stores.py) | a Postgres token store (encrypted), a Redis token store you can swap in, pending OAuth requests in Redis, the audits table |
| [common.py](common.py) | settings from environment variables, the database schema, time limits |
| [docker-compose.yml](docker-compose.yml) | Postgres and Redis for running it locally |

## Run it

From this folder, with Docker running:

```bash
docker compose up -d                       # Postgres on 15432, Redis on 16379

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # seoprobe from this repository, plus FastAPI, Celery, ...
python -m playwright install chromium      # the audits render pages in a browser

export SAAS_TOKEN_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# optional, for PageSpeed / CrUX in the audits: export SEOPROBE_GOOGLE_API_KEY=...

celery -A worker worker --loglevel=info    # terminal 1
uvicorn app:app --port 8000                # terminal 2
```

Then, from a third terminal:

```bash
curl "http://127.0.0.1:8000/check?url=https://example.com/"

curl -X POST http://127.0.0.1:8000/tenants/acme/audits \
     -H 'content-type: application/json' -d '{"url": "https://example.com/"}'
# {"id": "8a01a391-...", "status_url": "/audits/8a01a391-..."}

curl http://127.0.0.1:8000/audits/8a01a391-...          # queued → running (stage) → done (score)
curl "http://127.0.0.1:8000/audits/8a01a391-.../report?lang=tr"
```

FastAPI's interactive docs are at http://127.0.0.1:8000/docs. When you are done:
`docker compose down -v`.

## Endpoints

| Method and path | What it does | Typical answer |
|---|---|---|
| `GET /check?url=` | on-page rules for one page, inside the request | 200 with findings, in a second or two |
| `POST /tenants/{tenant}/audits` | queues a page (`"mode": "page"`) or site (`"mode": "site", "max_pages": 10`) audit | 202 with the job id; 400 for an address that can never be audited (`invalid-url`, `ssrf-blocked`) |
| `GET /audits/{id}` | the job: `queued`, `running` with the last finished stage, `done` with the score, or `failed` with the error code | 200 |
| `GET /audits/{id}/report?lang=en\|tr` | the full report, compared with the customer's previous audit of the same URL | 200, or 409 while it is not done |
| `GET /tenants/{tenant}/google` | whether the customer has connected Google | 200 |
| `DELETE /tenants/{tenant}/google` | disconnects: revokes the grant at Google and deletes the customer's tokens | 200 |
| `GET /tenants/{tenant}/google/connect` | the Google sign-in URL for the customer | 200, or 409 `missing-credential` without an OAuth client |
| `GET /google/callback?code&state` | where Google sends the customer back; stores the tokens | 200, or 400 for an unknown or expired sign-in |
| `GET /tenants/{tenant}/search-console?url=` | clicks, impressions and top queries from the customer's own account | 200 |

## The decisions, and why

**One Session per API process, opened in the lifespan.** A `Session` keeps the connection pool,
the per-site politeness limits and the browser warm, and any number of requests can share it.
It belongs to the event loop that opened it, which in FastAPI is the server's loop, the same one
the request handlers run on. With several Uvicorn workers, each process opens its own.

**One Session per Celery task.** Celery runs each task in its own `asyncio.run`, that is a new
event loop, so the session is opened inside the task. An audit lasts tens of seconds, so a fresh
connection pool costs nothing that matters.

**Quick checks in the API, audits in the worker.** `onpage` on one page takes a second or two
and fits in a request. A full audit visits many URLs and may render pages, so it is queued. The
report is built in the API from the stored audit, because building it makes no request.

**Reject bad URLs before queueing.** `seoprobe.normalize` checks an address without any network
access. It succeeds even for a bad address, so the endpoint looks at `data.allowed`, and
`ftp://...` or `http://10.0.0.1/` never reach the queue. A host name that resolves to a private
address passes this check, because it does no DNS lookup. The audit then refuses it with
`ssrf-blocked`.

**Store the whole result.** The worker saves `result.to_json()` in a `jsonb` column. Everything
else (the score shown in the status, the report, the comparison with the previous audit) is
derived from it later, so you can change how you present results without re-running audits.

**Progress from events.** The worker passes an `on_event` hook to its session and writes the last
finished stage of the audit to the job row, which is what `GET /audits/{id}` shows.

**Time limits that nest.** The audit gets `timeout_s=600` and link and archive budgets of 20 and
15 seconds. Celery's own limits sit above that, so seoprobe always finishes and reports (at worst
partially) before Celery would kill the task. `task_acks_late` hands the job of a crashed worker
to another one, and the task skips jobs that already finished, so a redelivery does no harm.

**Tokens encrypted, per customer.** The token store is the only thing seoprobe asks you to provide
for Google. Here every customer has one row in `google_tokens`, encrypted with Fernet
(`SAAS_TOKEN_KEY`). `RedisTokenStore` in [stores.py](stores.py) is the same thing on Redis.

**The OAuth flow is stateless.** `gsc.auth_url` returns a small request object. It is kept in
Redis for ten minutes under its `state`, and taken out (once) when Google sends the user back, so
the callback can land on any API instance, and an abandoned sign-in cleans itself up.

**Audits see the customer's Google account.** The worker passes `account=job.tenant` to the
audit, so it reads Search Console through that customer's own connection. `rerun` keeps the
account, because it is stored in the audit's options.

**Errors become HTTP statuses.** When something goes wrong on the internet, seoprobe does not
raise an exception. It returns a stable error code. `fail()` in [app.py](app.py) turns that code
into an HTTP status:

| What went wrong | HTTP status |
|---|---|
| the address cannot be audited (`invalid-url`, `ssrf-blocked`, ...) | 400 |
| something must be set up first (an API key, a Google connection) | 409 |
| a Google or Bing API quota is used up | 429 |
| a time limit was reached | 504 |
| the site or a service did not answer | 502 |
| a bug in seoprobe (`internal-error`) | 500 |

The response also carries the code and a `retryable` flag, so the frontend knows whether trying
again can help.

## Before production

- **Run the worker behind an egress firewall.** The browser that renders pages resolves
  addresses itself, so the worker's network must not reach private ranges, `localhost` or cloud
  metadata addresses. The API's own checks need no such rule.
- **Keep the token key in a secret manager.** Losing `SAAS_TOKEN_KEY` means every customer has to
  reconnect Google. Rotating it means re-encrypting the rows.
- **Use real migrations** (Alembic, for example) instead of `CREATE TABLE IF NOT EXISTS`.
- **Size the politeness settings for your worker count.** Each worker process keeps its own
  per-site limits, so N workers auditing one site may send N times as many requests at once.
- **Add what a product needs around it:** authentication (the `tenant` in the path is trusted
  here), per-customer rate limits on `POST /audits`, and escaping of page-derived text before you
  show it in HTML.

## Using Temporal instead of Celery

The shape stays the same. The body of `_run` in [worker.py](worker.py) becomes an activity, with
the session opened inside it. The workflow calls the activity with a start-to-close timeout a
little above `AUDIT_TIMEOUT_S` and a retry policy that does not retry `invalid-url` or
`ssrf-blocked`. Progress can go to the database as here, or to a workflow query.

## How this example was checked

On 23 November 2026, with Postgres 16 and Redis 7 in Docker, FastAPI 0.165, Celery 5.7 and
Python 3.12:

- **Every endpoint above** was called over HTTP.
- **Page audits** finished in about 20 seconds, and a three-page site audit in about 35 seconds.
- **The second audit's report** was compared with the first.
- **Both token stores** were tested with set, get and delete. The Postgres row holds no plain
  text.
- **The OAuth request** expires and can be taken only once.
- **A redelivered task** for a finished audit is skipped.

On 24 November 2026, after audits gained `account=` and `gsc.disconnect` was added, it was run
again in the same way:

- **Audits read the customer's own account.** A customer with a stored token got an audit whose
  Search Console section came from that token; a customer without one got `not-connected`.
- **`DELETE /tenants/{tenant}/google`** sent the revocation to Google, deleted the encrypted row,
  and answered the same way when called a second time.
- **Every one of the 21 error codes** maps to the HTTP status in the table above.

The full Google sign-in was not run, because it needs a real OAuth client. The tokens used above
were made up, so Google rejected them, which is the path the test needed.
