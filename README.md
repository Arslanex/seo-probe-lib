<h1 align="center">SEOPROBE</h1>

<p align="center">
  <strong>SEO analysis you can build into your own Python application.</strong><br>
  Audit a page or a whole site, get scored findings with evidence, as clean JSON.
</p>

<p align="center">
  <a href="#what-is-seoprobe">About</a> |
  <a href="#features">Features</a> |
  <a href="#requirements">Requirements</a> |
  <a href="#installation">Setup</a> |
  <a href="#quick-start">Quick start</a> |
  <a href="#usage">Usage</a> |
  <a href="#examples">Examples</a> |
  <a href="#using-it-in-your-application">Integration</a> |
  <a href="#troubleshooting">Troubleshooting</a> |
  <a href="#license">License</a>
</p>

---

## What is seoprobe?

seoprobe looks at a website the way a careful SEO consultant would, and tells you what it found:
a missing canonical tag, a slow page, broken links, a robots.txt that blocks Google, images
without alt text, an expired certificate. Every finding comes with the evidence it is based on and
a suggestion for the fix, and the whole site gets a score from 0 to 100.

It is a **library**, not an app. There is no website, command line tool or database inside: you
call it from your own Python code, and it hands you the results. That makes it a good fit for:

- a SaaS product that offers SEO reports to its customers,
- an agency's internal tool that checks client sites every night,
- a script that watches your own site for regressions after each deploy.

A few things it cares about:

- **Plain, predictable results.** Every analysis returns the same kind of result object, which
  turns into JSON with one call. When something goes wrong on the internet (a site is down, a key
  is missing), you get a clear error code, not an exception.
- **Safe to point at any URL.** It refuses to touch private networks, `localhost` or cloud
  metadata addresses, so your users cannot use it to probe your servers.
- **Polite to the sites it checks.** It spaces its requests, limits how many run at once per
  site, and backs off when a site struggles.
- **The same answer twice.** The same pages give the same findings and the same score.
- **English and Turkish.** Findings and reports come in either language.

## Features

| Area | What it checks |
|---|---|
| **Pages** | titles, descriptions, headings, canonical tags, images, links, language, page type |
| **Content** | readability, keyword use, Google and social media previews, structured data (JSON-LD) and what is missing, local business details (name, address, phone) |
| **Site setup** | robots.txt, sitemaps, HTTPS and security headers, redirects, 404 handling, technologies used, DNS and email security, how AI crawlers see the site |
| **Crawling** | a sample of the site's pages, broken links, click depth, orphan pages, duplicate content, hreflang, internal linking ideas, a generated sitemap |
| **In a real browser** | JavaScript dependence, screenshots, mobile usability, accessibility (axe-core), speed metrics, carbon footprint |
| **Google data** | PageSpeed Insights, Chrome UX Report (real-user Core Web Vitals) |
| **Off-site** | domain age, archive history, Open PageRank, a side-by-side comparison with competitors |
| **Monitoring** | URL lists, redirect maps after a migration, page snapshots and what changed, server log analysis (which bots crawl what, fake Googlebots) |
| **Search engines** | Google Search Console and Google Analytics 4 (per-customer sign-in), Bing Webmaster Tools, IndexNow |
| **Audits and reports** | everything above in one call, a 0-100 score per category, a prioritised action plan, a comparison with the previous audit |

All 71 operations, with what each needs, are listed in [docs/operations.md](docs/operations.md). Terms
such as SSRF, coverage or time budget are explained in the [glossary](docs/glossary.md).

## Requirements

- **Python 3.11, 3.12 or 3.13.**
- **macOS or Linux.** It has been tested on macOS and in a Linux Docker image
  ([deployment guide](docs/guides/deployment.md)). Windows has not been tested.
- **For the browser features** (rendering, screenshots, mobile, accessibility, speed metrics):
  Chromium, downloaded once by Playwright (about 200 MB). Everything else works without it.
  Audits work without it too, but they skip rendering and speed measurement, so the performance
  category then needs PageSpeed data (a Google API key) to be scored.
- **Optional API keys**, only for the features that need them:

  | Key | Unlocks | Where to get it |
  |---|---|---|
  | Google API key | PageSpeed Insights, Chrome UX Report | Google Cloud console, with the "PageSpeed Insights API" and "Chrome UX Report API" enabled |
  | Open PageRank key | domain authority | openpagerank.keywordseverywhere.com (old DomCop keys no longer work) |
  | Bing Webmaster key | Bing Webmaster Tools data | Bing Webmaster Tools, Settings → API access |
  | Google OAuth client | Search Console and GA4 for your users' sites | Google Cloud console: a "Web application" client for a product, a "Desktop" one for scripts ([steps](docs/guides/saas_integration.md#setting-up-google)) |

  Without a key, the features that need it simply report that the key is missing. Nothing else is
  affected.

## Installation

seoprobe is not on PyPI yet, so you install it from its source code.

**1. Get the code.**

```bash
git clone https://github.com/Arslanex/seo-probe-lib.git
cd seo-probe-lib
```

**2. Create a virtual environment** (recommended, so it does not mix with other projects).

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**3. Install it.** Pick the line that fits. If unsure, choose `all`.

```bash
pip install ".[all]"        # everything
pip install "."             # the core only: no browser features, no DNS checks
pip install ".[browser]"    # core + browser features
pip install ".[dns]"        # core + DNS and email security checks
```

**4. Download the browser** (only if you installed `all` or `browser`):

```bash
python -m playwright install chromium
```

On a Linux server, add the system libraries Chromium needs:
`python -m playwright install --with-deps chromium`.

**5. Check that it works:**

```bash
python -c "import seoprobe; print('seoprobe', seoprobe.__version__, 'is ready')"
```

### Using it from another project

Keep the code somewhere on disk and point your project at it:

```bash
pip install "/path/to/seo-probe-lib[all]"       # a copy, fixed at today's version
pip install -e "/path/to/seo-probe-lib[all]"    # linked: changes to the code apply at once
```

Or add a line to your project's `requirements.txt`:

```text
seoprobe[all] @ file:///absolute/path/to/seo-probe-lib
```

Or install it straight from GitHub, without cloning:

```bash
pip install "seoprobe[all] @ git+https://github.com/Arslanex/seo-probe-lib.git"
```

To hand someone a single file instead, build a wheel with `pip install build && python -m build`;
it lands in `dist/` and installs with `pip install "dist/seoprobe-0.1.0-py3-none-any.whl[all]"`.

## Quick start

This runs a full audit of one page. It takes about 20 seconds and makes a few dozen requests to
the site, so point it at a site you are allowed to test. Save it as `try_seoprobe.py` and run it
with `python try_seoprobe.py`:

```python
import asyncio

import seoprobe


async def main() -> None:
    result = await seoprobe.audit_page("https://example.com/")

    if not result.ok:
        print("The audit could not run:", result.error.code, "-", result.error.message)
        return

    audit = result.data
    score = audit.scores.overall if audit.scores else None
    print("Score:", score if score is not None else "not enough data", "out of 100")
    for finding in audit.findings[:5]:
        print(f"- [{finding.severity}] {finding.message}")


asyncio.run(main())
```

You will see something like:

```text
Score: 90.5 out of 100
- [Info] No robots.txt file was found (the server answered with a 4xx status), so no crawl restrictions apply.
- [Info] No /llms.txt file was found. It is an optional community proposal that Google Search ignores.
- [Medium] The title is shorter than 30 characters. A very short title rarely describes the page ...
- [Medium] The page has no meta description. Search engines write their own snippet when none is given.
- [Medium] No H1 heading was found on the page.
```

That is the whole idea: call a function, get a result. The rest of this page shows how to use the
result, how to set things up for production, and what else is in the box.

## Usage

### Every function works the same way

Each capability is one `async` function, called an operation: `seoprobe.onpage(url)`,
`seoprobe.robots(url)`, `seoprobe.audit_site(url)` and so on (71 in all). Each returns a result
with the same fields:

| Field | What it holds |
|---|---|
| `status` | `"ok"`, `"partial"` (it ran, but something could not be checked) or `"error"` |
| `data` | the answer itself, for example the audit, the robots.txt report or the list of technologies |
| `findings` | the problems found: an id, a severity, a category, the message, the evidence and the fix |
| `not_checked` | what could not be checked and why (a missing key, a time limit, not applicable) |
| `error` | when `status` is `"error"`: a stable `code`, a readable `message`, and whether a retry may help |
| `meta` | a trace id for your logs, the duration and every request that was made (secrets hidden) |

```python
result = await seoprobe.robots("https://example.com/")
result.to_json()      # everything as JSON, ready to store or send
result.to_dict()      # the same as a Python dict
result.unwrap()       # just the data, or raise seoprobe.SeoprobeError if it failed
```

Two helpers are not operations and return their own small objects: `preflight` (what a feature
still needs) and `check_access` (whether a key works). Calling a function with an argument of the
wrong type is a programming error and raises pydantic's `ValidationError` at once.

Things going wrong on the internet are normal, so they are never exceptions: an unreachable site,
a DNS failure, a bad certificate or a missing key comes back as an error code (the full list is
in [docs/errors.md](docs/errors.md)). A site that answers "404 Not Found" is not an error at all:
it is a finding.

### One session for many calls

Called on their own, the functions open and close everything they need. When you make several
calls, share one `Session`: it keeps the connections, the politeness limits and the browser open
between calls, and you can fetch a page once and analyse it many times.

```python
async with seoprobe.Session() as session:
    page = (await seoprobe.fetch("https://example.com/", session=session)).unwrap()

    tech, previews, checks = await asyncio.gather(
        seoprobe.techstack(page, session=session),
        seoprobe.previews(page, session=session),
        seoprobe.onpage(page, session=session),
    )
```

Use one session per process (or per event loop). Any number of tasks can share it.

### Settings and API keys

Every setting lives in `Config`. Pass it to the session:

```python
config = seoprobe.Config(
    lang="tr",                       # findings and reports in Turkish ("en" by default)
    google_api_key="...",            # PageSpeed Insights and Chrome UX Report
    openpagerank_key="...",          # domain authority
)
async with seoprobe.Session(config) as session:
    ...
```

Or read the settings from environment variables named `SEOPROBE_<SETTING>`:

```bash
export SEOPROBE_GOOGLE_API_KEY="..."
export SEOPROBE_LANG="tr"
```

```python
config = seoprobe.Config.from_env()
```

If you keep the keys in a `.env` file, load it into the environment first
(`set -a; . ./.env; set +a`), or use a package such as `python-dotenv`: seoprobe itself never
reads files or the environment unless you call `Config.from_env()`.

Before showing a feature to a user, you can ask whether it is ready, without any network access:

```python
ready = await seoprobe.preflight("pagespeed", session=session)
print(ready.ready, ready.missing)          # False ['credential:google_api_key']
```

### Audits and reports

```python
async with seoprobe.Session(config) as session:
    audit = (await seoprobe.audit_site("https://example.com/", max_pages=25, session=session)).unwrap()
    report = (await seoprobe.report.build(audit, session=session)).unwrap()

    print(report.scores.overall.value)               # the site score
    for recommendation in report.recommendations[:5]:
        print(recommendation.title)                    # what to fix first
```

The score lives in two places, in two shapes:

| Where | Expression | Value |
|---|---|---|
| the audit | `audit.scores.overall` | a number, or `None` when too little could be measured |
| the report | `report.scores.overall.value` | the same number, with `band` (`good`, `attention`, `poor`) next to it |

`audit_page` checks one page, `audit_site` takes a sample of the whole site. The report contains
the scores per category, every finding grouped and explained, a phased action plan
(`seoprobe.report.plan`) and, if you pass the previous audit with `previous=`, what changed since
then. Both the audit and the report are plain data you can store as JSON and load back later.

To show a progress bar while an audit runs, use `audit_stream`. It yields events as the stages
finish, and the last event carries the whole result:

```python
async for event in seoprobe.audit_stream("https://example.com/", session=session):
    if event.kind == "progress" and event.data.get("status") == "done":
        print("finished stage:", event.data["stage"])
    elif event.kind == "finished":
        audit = seoprobe.Audit.model_validate(event.data["result"]["data"])
```

A page audit goes through the stages `validate`, `fetch_entry`, `robots`, `render`, `measure`,
`analyze` and `score`. A site audit adds `discovery`, `sample` and `fetch_pages` after `robots`.
For a site audit pass `mode=seoprobe.AuditMode.SITE`. Leaving the loop early cancels the audit.

### Deciding how long to wait

A full audit visits many links and asks a few public archives about the site. On a large site
that can take 30-50 seconds. You decide how much of that you want to wait for:

```python
await seoprobe.audit_page(url, link_check_budget_s=5, visibility_budget_s=5, session=session)
```

With both budgets at 5 seconds, a typical page audit takes about 18 seconds instead of 40. The
score does not change: links that were not checked in time are simply reported as not checked,
never as broken. A "check now" button wants short budgets. A nightly report can wait as long as it
takes (leave them out, or pass `math.inf`). The [cost table](#what-an-audit-costs) has the
details.

### Google Search Console and Analytics

These use your customers' own Google accounts, so each customer signs in once. You need a Google
OAuth client first (`Config(google_app=seoprobe.GoogleApp(client_id, client_secret))`, or the
environment variables); the [SaaS guide](docs/guides/saas_integration.md#setting-up-google) has
the Google Cloud steps. Then:

```python
# 1. Ask for the sign-in link. Keep `auth` (it is JSON) for about ten minutes, keyed by auth.state.
auth = (await seoprobe.gsc.auth_url(redirect_uri, account=customer_id, services=["gsc", "ga4"],
                                    session=session)).unwrap()
# ... send the customer to auth.url; Google redirects back to redirect_uri with code and state ...

# 2. On the callback: load the saved request by state (use it once) and finish the sign-in.
await seoprobe.gsc.exchange_code(saved_auth, code, state=state, session=session)
```

`services` decides what the customer is asked to share: `["gsc"]` (the default) for Search
Console only, `["gsc", "ga4"]` to also read Google Analytics 4. From then on, `gsc.*` and `ga4.*`
work for that customer with `account=customer_id`, and so do the audits:
`audit_site(url, account=customer_id)` adds the customer's Search Console data to the audit.
`seoprobe.gsc.disconnect(account=customer_id)` revokes access at Google and deletes the tokens.
Google revokes the user's whole grant to your app, so other records connected with the same
Google user stop working too.

The tokens go to the token store you gave the session. seoprobe ships `MemoryTokenStore` (tests)
and `FileTokenStore` (one machine). In production, write your own: any object with three async
methods (`get`, `set`, `delete`) will do, so the tokens can live encrypted in your database. See
[examples/10_search_engines.py](examples/10_search_engines.py) for the whole flow.

## Examples

The [examples](examples) folder has one ready-to-run script per area. Each one explains itself as
it runs and works against any site you give it:

```bash
python examples/01_quickstart.py https://your-site.com/
```

| Script | What it shows |
|---|---|
| [01_quickstart.py](examples/01_quickstart.py) | your first call and how to read the result |
| [02_session_config_errors.py](examples/02_session_config_errors.py) | sessions, settings, languages, errors, safety checks |
| [03_page_and_content.py](examples/03_page_and_content.py) | on-page checks, readability, previews, structured data, local business details |
| [04_site_setup.py](examples/04_site_setup.py) | robots.txt, sitemaps, HTTPS, redirects, AI crawlers, DNS |
| [05_crawl_and_links.py](examples/05_crawl_and_links.py) | crawling a sample, broken links, duplicates, hreflang, link ideas |
| [06_browser_and_performance.py](examples/06_browser_and_performance.py) | rendering, screenshots, mobile, accessibility, speed, carbon |
| [07_offsite.py](examples/07_offsite.py) | domain history, authority, competitor comparison |
| [08_monitoring.py](examples/08_monitoring.py) | URL lists, redirect maps, snapshots, log analysis |
| [09_audit_and_report.py](examples/09_audit_and_report.py) | full audits with live progress, reports, action plans |
| [10_search_engines.py](examples/10_search_engines.py) | Search Console, GA4, Bing, IndexNow |
| [11_embedding.py](examples/11_embedding.py) | using it inside your own system: threads, processes, your own token store |

For a complete back end, see [examples/saas_fastapi_celery](examples/saas_fastapi_celery): FastAPI
with the session in its lifespan, a Celery worker for the audits, Postgres for results and
encrypted per-customer Google tokens, Redis for the queue. Docker Compose starts Postgres and
Redis; the API and the worker are two more commands.

Screenshots and JSON reports the examples create are saved in `examples/output/`. More detail is
in [examples/README.md](examples/README.md).

## Using it in your application

seoprobe does nothing on its own and remembers nothing between calls, so it fits into whatever
you already run:

- **Web apps** (FastAPI, aiohttp, Django async views): call the functions from your async code.
  Heavy work such as parsing large pages runs on worker threads, so your app stays responsive.
  From synchronous code, wrap the call in `asyncio.run(...)` (or `async_to_sync` in Django) and
  pass no session, so each call gets its own.
- **Cancelling is safe.** Cancelling the task that awaits an operation stops it, together with
  its open requests. seoprobe never swallows `asyncio.CancelledError`.
- **Background workers** (Celery, RQ, a process pool): results and errors can be passed between
  processes, and every result turns into JSON. Open the session inside the worker, not before the
  workers start.
- **Threads:** each thread runs its own event loop with its own session.
- **Storage:** keep `result.to_json()` wherever you like and hand earlier results back when you
  need them (`report.build(audit, previous=...)`, `diff(old, new)`, `rerun(audit)`).

### What an audit costs

Measured on public sites in November 2026:

| Run | Requests | Time |
|---|---|---|
| one page, no time limits | 100-145 | 35-45 s |
| one page, `link_check_budget_s=5, visibility_budget_s=5` | about 50 | about 18 s |
| a site sample of 6 pages | about 190 | about 65 s |

Most of the time goes to checking links one by one and to waiting politely between requests to
the same site. The settings that shape it, all in `Config`:

| Setting | Default | What it does |
|---|---|---|
| `link_check_budget_s` | none | how long link checks may take (also per call) |
| `visibility_budget_s` | none | how long the public archive lookups may take (also per call) |
| `max_internal_link_checks` / `max_external_link_checks` | 60 / 40 | how many links are checked per audit |
| `per_host_concurrency` / `request_delay_s` | 2 / 0.4 s | how gently each site is treated; lowering them speeds things up but may get you blocked by small sites |
| `visibility` | on | the public archive lookups at all |
| `render` | on | rendering pages in Chromium. Off, or without Chromium, audits skip rendering and lab metrics, and the performance category needs PageSpeed data |
| `max_pages` | 25 | pages in a site sample |

### Running on a server

- **Restrict outgoing network access for browser features.** Chromium resolves addresses itself,
  so give the process a firewall or container network policy that blocks private ranges,
  `localhost` and cloud metadata addresses. The plain HTTP checks are protected without this.
- **Several worker processes each keep their own politeness limits**, so N workers auditing the
  same site may send N times as many requests at once. Size `per_host_concurrency` accordingly.
- **Treat text taken from pages as untrusted** (titles, evidence, link texts): escape it before
  you show it in HTML.
- **Nothing is sent anywhere on your behalf**, with one exception you call yourself:
  `indexnow.submit` notifies search engines about URLs.

## Known limitations

Things version 0.1.0 does not do yet, so you can plan around them:

- **Phone numbers in page text** are recognised in Turkish and `+international` form only. Other
  numbers are found through `tel:` links.
- **One `Config` per session.** Language, keys and limits are per session. For a customer who
  needs another language, build the report with `report.build(audit, lang="tr")`: the stored
  findings are translated when the report is built.
- **Public archive data** (Wayback Machine, Common Crawl) depends on free services that are
  sometimes slow or down. The rest of an audit is not affected.

## Troubleshooting

**A browser feature answers `missing-extra`.**
Install the browser extra (`pip install ".[browser]"`) and download Chromium
(`python -m playwright install chromium`).

**A browser feature answers `browser-unavailable`.**
Chromium is installed for a different Playwright version, or a system library is missing on
Linux. Run `python -m playwright install --with-deps chromium` again inside the same environment.

**PageSpeed, CrUX or authority answers `missing-credential`, although the key is in `.env`.**
seoprobe does not read `.env` files. Load the file into the environment and use
`Config.from_env()`, or pass the key to `Config(...)` directly. The variables must be named
`SEOPROBE_GOOGLE_API_KEY`, `SEOPROBE_OPENPAGERANK_KEY`, and so on.

**Authority answers `authority-unauthorized`.**
Open PageRank moved to Keywords Everywhere and old DomCop keys stopped working. Get a new key at
openpagerank.keywordseverywhere.com.

**An audit takes too long.**
Pass `link_check_budget_s` and `visibility_budget_s` (see [Deciding how long to wait](#deciding-how-long-to-wait)),
or a smaller `max_pages` for site audits.

**A site blocks the checks or answers `rate-limited`.**
Some sites protect themselves from bots. Keep the default politeness settings, and if it is your
own site, allow the `seoprobe` user agent in your firewall.

**The archive information shows "time budget" or is missing.**
The Wayback Machine and Common Crawl are free public services and are sometimes slow or down.
The rest of the audit is not affected.

## Development

For working on seoprobe itself:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[all]" pytest pytest-asyncio mypy ruff import-linter lxml-stubs
python -m playwright install chromium

pytest                                  # the test suite, fully offline
ruff check src tests examples && ruff format --check src tests examples
mypy
lint-imports                            # keeps the internal structure in shape
```

How the code is organised and why is described in [docs/](docs/README.md) (the architecture overview
and the decision records); the changes are
in [CHANGELOG.md](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE). The bundled axe-core (MPL-2.0) ships with its own license in
`src/seoprobe/features/render/axe/LICENSE`.
