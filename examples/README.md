# seoprobe examples

Runnable scripts, one per area, from the first call to embedding the library in a product.
Each one prints what it does, takes the target URL as its first argument (default:
`https://www.python.org/`) and talks to the real site, politely: per-host concurrency and
spacing are the library's defaults.

```bash
pip install -e ".[all]"                     # from the repository root; seoprobe is not on PyPI yet
python -m playwright install chromium       # once, for 06 and the audits in 09 / 11
python examples/01_quickstart.py https://example.com/
```

Files the examples write (screenshots, JSON audits and reports, OAuth state) go to
`examples/output/`, which git ignores.

| Script | What it shows | Network | Takes |
|---|---|---|---|
| `01_quickstart.py` | one call without a session; the `Result` envelope (`status`, `data`, `findings`, `not_checked`, `error`, `meta`); `to_json()`, `unwrap()`; an error as a stable code | yes | ~1 s |
| `02_session_config_errors.py` | `Config` (here Turkish texts), `Config.from_env()`; `normalize`; one `Session`, one fetched `Page` reused by several operations run side by side; the `on_event` hook; SSRF refusals that never leave the machine; `SeoprobeError`; `timeout_s`; `preflight`, `check_access`; `operations()`, `rules()`, `requirements()` | yes | ~5 s |
| `03_page_and_content.py` | `onpage`, `classify`, `readability` (with a keyword), `previews` (SERP and Open Graph), `schema`, `suggest_schema`, `local_seo` | one fetch | ~2 s |
| `04_site_setup.py` | `robots`, `sitemap`, `transport` (TLS, security headers, http→https, www), `site_setup` (redirect matrix, soft 404, well-known files), `ai_access`, `techstack`, `dns_health` | yes | ~10 s |
| `05_crawl_and_links.py` | `crawl` (a seeded sample), `links` with a link-check time budget, `indexability`, `duplicates`, `hreflang`, `suggest_links`, `generate_sitemap` | yes | ~20 s |
| `06_browser_and_performance.py` | `render` (JavaScript dependence), `screenshots` (saved as files), `mobile`, `accessibility` (axe-core), `lab_metrics` (desktop and mobile), `carbon`; `pagespeed`, `field_data`, `web_vitals` with or without a Google key | browser | ~20 s |
| `07_offsite.py` | `visibility` with a short and a long time budget, `authority` (Open PageRank key), `competitors` | yes | ~35 s |
| `08_monitoring.py` | `check_urls`, `check_redirects` (a migration map), `snapshot` + `diff`, `analyze_logs` (a fake Googlebot is caught by reverse DNS) | yes | ~5 s |
| `09_audit_and_report.py` | `audit_stream` (live progress), `audit_page`, `audit_site`, time budgets, `report.build` with `previous=`, `report.plan`, `report.findings`, `report.compare`, `rerun`; JSON written to disk | browser | ~60 s |
| `10_search_engines.py` | Search Console / GA4 OAuth (stateless: you keep the `AuthRequest`) and every `gsc.*` (properties, find_property, analytics, top_queries, inspect, sitemaps, rankings, opportunities, summary), `ga4.*` (properties, landing_pages, summary), `bing.*` (sites, query_stats, crawl_stats, crawl_issues, url_info) and `indexnow.*` call; without credentials each shows the code it answers, once connected the same lines print the data | yes | ~2 s |
| `11_embedding.py` | your own async `TokenStore`, threads with their own event loops, a spawned process pool carrying `Result` and `SeoprobeError`, JSON and pickle round trips, the wrong-event-loop guard | browser | ~25 s |

## A complete SaaS back end

[saas_fastapi_celery/](saas_fastapi_celery) puts it all together: a FastAPI API with one shared
`Session` opened in its lifespan, a Celery worker that runs audits with a `Session` per task,
Postgres for audits and encrypted per-tenant Google tokens, Redis for the queue and the pending
OAuth sign-ins, and seoprobe's error codes mapped to HTTP statuses. It comes with a
`docker-compose.yml` and was run end to end. Its README explains every decision.

Durations were measured against python.org. They depend on the site and on the public services
behind `visibility` (Wayback, Common Crawl, RDAP), which are the slowest part.

## Keys and accounts

Everything runs without keys: what needs one answers `missing-credential` (or reports it in
`not_checked`) and the scripts show what to set. Read by `Config.from_env()`:

| Variable | Enables |
|---|---|
| `SEOPROBE_GOOGLE_API_KEY` | `pagespeed`, `field_data` (CrUX), and their part of `web_vitals` and the audits |
| `SEOPROBE_OPENPAGERANK_KEY` | `authority` |
| `SEOPROBE_BING_API_KEY` | `bing.*` |
| `SEOPROBE_GOOGLE_CLIENT_ID`, `SEOPROBE_GOOGLE_CLIENT_SECRET` | the Search Console / GA4 OAuth flow (`10_search_engines.py`) |
| `SEOPROBE_LANG` | `tr` for Turkish finding and report texts |

## Reading the output

* `status`: `ok`, `partial` (it ran, but something could not be checked: see `not_checked`) or
  `error` (see `error.code`, and [docs/errors.md](../docs/errors.md) for the list of codes).
* Findings: `[severity] rule-id: message`, with `evidence` taken from the page.
  `seoprobe.rules()` lists every rule.
* `not_checked`: what could not be measured and why (`time-budget`, `no-api-key`,
  `not-applicable`, ...). It never lowers a score: the score covers what was measured.
* Page-derived strings (titles, evidence, anchors) are untrusted: escape them when you render
  HTML.
