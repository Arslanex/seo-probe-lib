# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`seoprobe` is an async-only Python library (3.11+) for SEO analysis. It is meant to be embedded, for
example in a SaaS back end, so it has no web server, CLI, database, background worker or LLM.
Every capability is one `async` function that returns a `Result[T]` envelope. Its behaviour (rules,
thresholds, scoring) was ported from the `seo-analyzer-mvp` project (`src/seoa`). That project is the
reference for behaviour, not for architecture.

Design docs: start at `docs/README.md`. How the library is built is in
`docs/architecture/overview.md` and `docs/architecture/security.md`; why, in the decision records
`docs/architecture/adr/` (add a new record for each new decision, never rewrite an old one). The
original plan and review are in `docs/archive/` (Turkish, historical, never edited); where they
disagree with the code or the ADRs, the ADRs win. Every folder under `docs/` has a README for
navigation, and all docs outside `docs/archive/` are in English.

## Commands

```bash
uv venv && uv pip install -e ".[all]" --group dev
python -m playwright install chromium          # needed for render/performance browser tests

ruff check src tests examples && ruff format --check src tests examples
mypy                                           # strict, src only
lint-imports                                   # layer contracts between core and features
pytest                                         # whole suite
pytest tests/features/site                     # one feature in isolation (supported by design)
pytest tests/features/site/test_robots.py::test_name
```

`examples/` holds one runnable script per area (`python examples/01_quickstart.py [url]`, see
`examples/README.md`). They hit real sites, so pytest does not run them; after changing an
operation's signature or output model, run the matching example against a real site.

Ruff's `BLE` rule is on: every `except Exception` needs `# noqa: BLE001 - <why it must not raise>`.

There is no CI yet: run the checks above yourself before handing work back. Supported Python is
3.11–3.13, and `import seoprobe` must keep working with no extras installed (without `playwright`
or `dnspython`). To check the package, build a wheel (`uv build`) and install it into a clean venv.

Regenerating snapshots. Do this only when the change is intended:
- `python -m tests.contract.test_docs` rewrites `docs/operations.md` from `seoprobe.operations()`.
- `python -m tests.contract.test_public_api` rewrites `tests/fixtures/public_api.json` (`__all__` plus
  every signature). Record API changes in `CHANGELOG.md`. Removals need a deprecation period (plan §9).
- `python -m tests.contract.test_schemas` rewrites `tests/fixtures/schemas/<feature>/<op>.json`. When
  an output model changes, first bump `schema_version=` in that operation's `@operation(...)` (the
  test fails if the schema changed but the version did not; plan §9), then regenerate.

## Architecture

```
src/seoprobe/
  __init__.py        public API: re-exports operations, namespaces (gsc, ga4, bing, indexnow, report)
  core/              shared foundation; must never import seoprobe.features
    config, errors, models, result     contract types (bottom layer)
    parse/             pure HTML / text / URL parsing, no I/O, imports no other core module
    net/               SSRF policy + resolver, HTTP client, politeness, external APIs, browser
    rules/             rule catalog loading and localized texts, deterministic scoring
    feature, session, page             runtime: @operation, Session, Page/fetch/normalize (top layer)
    data/              shared data files (core catalog, bots, stopwords), read by package name
  features/<name>/   one package per group of related operations
  features/__init__.py   explicit FEATURES list (registration is not done via import side effects)
```

**`@operation` (`core/feature.py`) holds all shared behaviour.** An operation is written as
`async def op(ctx: Context, ...) -> Outcome[T]` and decorated with
`@operation("name", output=Model, requires=Requirements(...), timeout_s=...)`. The decorator turns
it into `op(..., *, session=None, timeout_s=None) -> Result[T]` and handles input validation, a
temporary `Session` when none is passed, requirement checks (extras, credentials, browser), the
deadline, mapping exceptions to `ErrorCode`, the request log with secrets masked, events and
building the `Result`. Do not repeat any of this inside feature code. Inside an operation:
- `ctx.page(target)` accepts a URL or an already-fetched `Page`.
- `ctx.http` and `await ctx.browser()` give the session's HTTP client and browser.
- `ctx.finding(rule_id, ...)` and `ctx.not_checked(...)` record findings and skipped checks.
- `ctx.fail(ErrorCode.X, **details)` aborts the operation with an error.
- `ctx.budget_deadline(budget_s)` is the event-loop time a slow, optional stage must stop at
  (link probes, public lookups). It is capped so the operation keeps 20 % of its remaining time,
  at least 5 s. What it cuts short becomes `not_checked` with reason `TIME_BUDGET` ("time-budget"),
  never an error.

Nested operations reuse the parent's session, cannot outlive the parent's deadline, and add their
requests to the parent's `meta.requests`.

**Error model.** Failures from the outside world (network, site, external API) are never raised;
they come back as `Result.error` with a stable `ErrorCode` (`core/errors.py`, documented in
`docs/errors.md`, which a test keeps in sync). A 404 or 500 from a site is data, not an error.
Exceptions are only for programming errors and `unwrap()`. Never swallow
`asyncio.CancelledError`. `details` must not contain exception text, resolved IPs or secrets.

**Feature layering** (enforced by `import-linter` in `pyproject.toml` and by
`tests/contract/test_dependencies.py`):
`page` < `content | site | render | performance | search` < `crawl` < `monitoring | offsite` < `audit` < `report`.
- Each `FEATURE = Feature(..., depends_on=[...])` must match the feature's real imports.
- Import another feature only from its package root (`from seoprobe.features.site import robots`),
  never from its internal modules.
- Use only names in that feature's `__all__`: operations, models, and pure helper functions that
  are exported on purpose (plan §11 #3).
- `Page`, `fetch` and `normalize` live in `core/page.py`. Scoring and `PageRecord` live in
  `core/rules/scoring.py`, because both `audit` and `report` need them.
- Inside core, import-linter enforces `feature : session : page` > `net | rules` > `parse` >
  `config : errors : models : result` (`|` = independent siblings, `:` = may import each other).

**Layout inside a feature package** (plan §4.3; only these file kinds exist):
- `__init__.py`: re-exports, `FEATURE` and `__all__`; no logic.
- `models.py`: every output model (the JSON contract).
- Operation files: 1–3 related operations each.
- `catalog.json`: rule definitions plus `messages.en` / `messages.tr` texts.
- Feature-local `*.json` data files: thresholds, dictionaries, signatures.

Rules, texts and thresholds belong in data files, not in code. A file past about 400 lines is split
into a subpackage with the same name (e.g. `crawl/links/`, `site/sitemap/`, `report/sections/`).
`utils.py`, `helpers.py`, `common.py`, `misc.py`, `constants.py` and per-feature `exceptions.py` are
forbidden. A model goes into `core/models.py` only when core uses it or two or more features take it
as input. Otherwise it stays in the `models.py` of the feature that produces it.

To add a feature (plan §4.4):
1. Create the package with the layout above.
2. Add its `FEATURE` to the `FEATURES` list in `features/__init__.py`.
3. Re-export its operations in `seoprobe/__init__.py`.
4. Add `tests/features/<name>/`.
5. Update the import-linter layers.
6. Regenerate the operations doc and the public API snapshot.

**Scoring and parity.** Audit scores are deterministic. Rules the source project did not have
carry `"scored": false` in their catalog, so they are reported but never scored. This preserves
parity: `tests/features/audit/test_parity.py` requires the same findings and scores as the
snapshots in `tests/fixtures/parity/` (see the README there). Do not change scoring or the
behaviour of the ported rules unless you mean to break parity. Nothing has been released yet: the
first version is 0.1.0, which is also every rule's `since` and the `rule_version`.

**Security.** The HTTP client (`core/net/client.py` with `core/net/safety.py`) blocks SSRF at connect time and validates every redirect
hop by hand. The browser (`core/net/browser.py`) runs every request through the same policy. All outbound
requests, including calls to external APIs, must go through these clients. Strings that come from
pages are untrusted.

## Testing conventions

- `tests/conftest.py` blocks every non-loopback socket, so tests have no network. Use the fakes in
  `tests/fakes.py` (fake HTTP, browser and Google; `Session(http=..., browser=...)` accepts them),
  the per-feature `support.py` builders, HTML fixtures in `tests/fixtures/`, or the local
  `tests/demo_site.py`.
- `asyncio_mode = "auto"`: async tests need no decorator.
- `tests/contract/` checks:
  - catalogs: unique rule ids, and `en` and `tr` text for every rule;
  - the dependency rules;
  - the docs;
  - the public API snapshot and the output schema snapshots;
  - that every `ErrorCode` is named as `ErrorCode.<NAME>` in at least one behaviour test. A new
    code needs a test that produces it; a code nothing produces should be removed.

## Conventions

- A docstring that spans more than one line has its opening and closing `"""` on lines of their
  own, with the text in between; a one-line docstring stays `"""Short text."""`.
- Code, identifiers and docs are in English. The default `lang` is `en`, and user-facing finding
  texts exist in both `en` and `tr`.
- Nothing reads environment variables except `Config.from_env()` (`SEOPROBE_<FIELD>`).
- `CHANGELOG.md` uses Keep a Changelog, with entries prefixed by feature name (`site: ...`).
- Commit messages and version numbers follow the rules at the top of `CHANGELOG.md`: a
  `<area>: <summary>` line plus a long description (why, what, what not, how verified), one
  logical change per commit, `release: vX.Y.Z` commits with an annotated tag. Commits are
  authored by Arslanex only: add no co-author or tool attribution lines.
