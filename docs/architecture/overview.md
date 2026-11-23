# Architecture overview

seoprobe is an async Python library: no server, command line tool, database or background task
of its own. Every capability is one `async` function that returns the same `Result` envelope.
This page explains how that is put together. It is written for people who change the library.
Names such as `Context`, `Outcome` and `budget_deadline` are internal: an integrator never
touches them.

A term you do not know is probably in the [glossary](../glossary.md).

## Concepts

| Concept | What it is | Lifetime |
|---|---|---|
| `Config` | every setting: language, limits, timeouts, API keys. Immutable (a frozen dataclass) | as long as you like; share it freely |
| `Session` | the reusable resources: the HTTP client and its connection pool, per-host politeness limits, the browser (started on first use), the token store, the event hook | opened with `async with`, bound to the event loop that opened it; many tasks may share it |
| operation | a public function such as `seoprobe.onpage`; 71 of them, listed in [operations.md](../operations.md) | called per request |
| `Context` | what an operation's body receives: the session's resources, the language, helpers for findings and errors, the time left | one per call |
| `Result` | what every operation returns: `status`, `data`, `findings`, `not_checked`, `error`, `meta` | plain data; JSON and pickle round-trip |
| `Page` | one fetched page: URL, status, headers, HTML, timings; parsed into a `ParsedPage` on demand | plain data; pass it to several operations to fetch only once |

## What every operation goes through

Operations are written as `async def op(ctx: Context, ...) -> Outcome[T]` and decorated with
`@operation(...)` (in `core/feature.py`). The decorator turns that into the public signature
`op(..., *, session=None, timeout_s=None) -> Result[T]` and runs the same stages for every call:

1. **Validate the arguments** with pydantic. A wrong type is a programming error and raises.
2. **Take a session.** It uses the one passed in, the caller's own if this call is nested in
   another operation, or a temporary one that is closed afterwards.
3. **Check requirements:** an optional extra, an API key, the browser. If one is missing, the
   result is `missing-extra`, `missing-credential` or `browser-unavailable`, without running.
4. **Set up the context:** a trace id, the deadline, a request log and a byte budget.
5. **Run the body under the deadline.** A nested call never outlives its caller's deadline.
6. **Map exceptions to codes.** An `OperationError` becomes its `ErrorCode`, a timeout becomes
   `timeout`, and an unexpected exception becomes `internal-error`.
   `asyncio.CancelledError` is always re-raised.
7. **Emit events** (`started`, `progress`, `finished`) to the session's hook and listeners.
8. **Build the `Result`.** Findings are localised, and every request made, nested ones included,
   is listed with its secrets masked.

Because all of this lives in one place, feature code contains only the analysis itself.

## Packages and the rules between them

```text
src/seoprobe/
  core/                    shared foundation; imports no feature
    config, errors, models, result      contract types (bottom layer)
    parse/                 HTML, text and URL parsing; pure, imports no other core module
    net/                   SSRF policy and resolver, HTTP client, politeness, external APIs, browser
    rules/                 rule catalogues, localised texts, scoring
    feature, session, page              runtime (top layer)
  features/<name>/         one package per group of related operations
```

The features are layered. A higher layer may use a lower one, never the reverse:

```text
report  >  audit  >  monitoring | offsite  >  crawl  >  content | site | render | performance | search  >  page
```

Inside `core`, the layers are `feature : session : page` > `net | rules` > `parse` > contract
types. Three things enforce these rules:

- **`lint-imports`** checks the import-linter contracts in `pyproject.toml`.
- **`tests/contract/test_dependencies.py`** checks that each feature's declared `depends_on`
  matches its real imports, and that features use one another only through the package root and
  its `__all__`.
- **The layout inside a feature** is the same everywhere:
  - `__init__.py` with the exports and the `FEATURE` declaration,
  - `models.py` with every output model,
  - operation files,
  - `catalog.json` with the rules and texts,
  - data files.

See [ADR 0002](adr/0002-core-and-feature-layers.md).

## Keeping the caller's event loop free

seoprobe often runs inside someone else's event loop, for example a web server. So:

- **Network I/O is async.**
- **CPU-heavy work runs on worker threads** (`asyncio.to_thread`): HTML parsing
  (`await page.parse()`), log analysis, link-suggestion similarity and the TLS handshake check.
  A 5 MB page takes about 0.7 s to parse, and none of it blocks the loop.
- **Nothing global changes.** seoprobe leaves the host's logging and warning settings alone.

See [ADR 0006](adr/0006-parse-html-off-the-event-loop.md) and
[ADR 0007](adr/0007-picklable-results-and-loop-bound-sessions.md).

## Time budgets

Two parts of an audit are slow and optional: the link checks and the public archive lookups.
The caller decides how long to wait for them (`link_check_budget_s`, `visibility_budget_s`, in
`Config` or per call). One rule, `Context.budget_deadline`, turns a budget into a stopping time:

- **It is never later than the operation's own deadline.** It also leaves a reserve for the
  stages that follow: 20 % of the operation's remaining time when the budget starts, at least
  5 s.
- **What is cut short is `not_checked`** with the reason `time-budget`, never a failure.
- **The score does not depend on the budget**, because rules are judged over what was checked.

See [ADR 0008](adr/0008-dynamic-time-budgets.md).

## State

The library keeps no state between calls that you have to manage:

- **Results are plain data.** Store them, and pass earlier ones back in:
  `report.build(audit, previous=...)`, `diff(old, new)`, `rerun(audit)`.
- **OAuth is stateless.** `gsc.auth_url` returns a request object that you keep until the
  callback.
- **Google tokens go to a token store you provide.**

Inside a session, only reusable resources live: connections, per-host politeness counters and
the browser. See [ADR 0009](adr/0009-stateless-oauth-and-async-token-store.md) and
[ADR 0012](adr/0012-history-comes-from-the-caller.md).

## Versioning

seoprobe follows semantic versioning. The public contract is:

- the names in `seoprobe.__all__` and the signatures of the public functions
  (`tests/contract/test_public_api.py` keeps a snapshot);
- the `ErrorCode` values ([errors.md](../errors.md));
- every output model's JSON schema (`tests/contract/test_schemas.py` keeps one snapshot per
  operation).

When an output model changes, bump that operation's `schema_version` in `@operation(...)`. A new
field is a minor change. A removed field, or one whose meaning changes, is a major change. Then
regenerate the snapshots and record the change in the changelog. A public name that goes away
first keeps working for at least one minor version with a `DeprecationWarning`.

**Stored results across upgrades.** Every result records the versions it was made with in
`meta`: `package_version`, `schema_version` (of the operation's output) and `rule_version`.
Within one major version, output models only gain fields, so JSON stored by an older version
still loads with `Result.model_validate_json`. Before 1.0, minor versions may still break this,
and the changelog says when they do. Until seoprobe is on PyPI, pin a version by installing from
a fixed commit or tag (`...seo-probe-lib.git@<tag>`).

The rule set and the scoring formula have their own versions (`rule_version`, `score_version`),
reported in every result: the same versions and the same input always give the same score.

## Tests

The test suite runs without network: a fixture blocks every non-loopback socket, and the
operations are tested against fakes (`tests/fakes.py`) or a local demo site. Contract tests guard
the public API, the output schemas, the docs, the error codes, the catalogues and the dependency
rules. The parity tests compare audits with the source project's snapshots. Behaviour on real
sites is checked with the runnable [examples](../../examples/README.md).
