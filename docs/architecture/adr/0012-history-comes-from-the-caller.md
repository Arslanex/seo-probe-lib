# 0012. The library keeps no history; the caller passes earlier results back

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §11 #5, #6, #7, #8

## Context

The source project stored every audit in its database and read previous ones when building
reports and re-running audits. A library has no database, and must not invent one.

## Decision

- `report.build(audit, previous=...)` fills the history section from an earlier audit the
  caller provides. `report.compare(old, new)` and `diff(old_snapshot, new_snapshot)` work the
  same way.
- `rerun(audit)` starts a fresh audit with the same URL, mode and options, budgets included.
- A fatal audit failure (an unusable or blocked URL, an unreachable entry page) still returns an
  audit with `status=failed` and a stable `error_code`, inside an `ok` result, as the source did.
  The details stay in the DEBUG log, because they may reveal internal network information.
- A nested operation's requests also appear in its caller's `meta.requests`, so the record of an
  audit lists everything it fetched.

## Consequences

- Integrators store results (as JSON) and decide how long to keep them.
- Every result is self-contained: a report can be rebuilt later, in another language, from the
  stored audit alone.
