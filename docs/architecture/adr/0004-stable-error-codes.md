# 0004. Outside-world failures are stable codes, never exceptions

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §6.3, §11 #11, #18, #19, #20

## Context

In a service that audits arbitrary sites, unreachable hosts, bad certificates and missing keys
are everyday events. If they were exceptions, every caller would need the same try/except around
every call, and the exception text could leak internal details (resolved addresses, paths).

## Decision

- Failures from the outside world (network, site, external API, missing configuration) come back
  in `Result.error` as an `ErrorCode` with a localised message, a `retryable` flag and safe
  `details`. The codes change only in a major version and are listed in `docs/errors.md`, which a
  test keeps in sync.
- Exceptions are raised only for programming errors (wrong argument types) and by
  `Result.unwrap()`. `asyncio.CancelledError` is always re-raised.
- An HTTP status from the audited site (404, 500) is data, not an error.
- Codes are specific enough to act on: `dns-failed` is separate from `connect-failed`,
  `too-many-redirects` from `fetch-timeout`, and an unusable input is `invalid-url` or
  `unsupported-scheme` rather than `ssrf-blocked`.
- A code that nothing produces is removed. A contract test requires every code to be produced by
  at least one behaviour test.
- `details` accepts only safe keys (URL, domain, status, service, exception type name, missing
  requirement, reason code). Exception texts and addresses go to the DEBUG log only.

## Consequences

- Integrators map a short list of codes to HTTP statuses or messages once
  ([saas_integration.md](../../guides/saas_integration.md#turning-error-codes-into-http-responses)).
- Five codes from the original plan (`http-error`, `response-too-large`, `not-html`,
  `blocked-by-robots`, `cancelled`) were dropped before 0.1.0 because nothing returned them.
