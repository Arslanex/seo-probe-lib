# 0011. Wayback dates from fast exact-host queries

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §11 #30

## Context

`visibility` reports when a site was first and last archived. The original queries asked the
Wayback CDX server about the whole domain. Measured, the domain-wide "last capture" query
(`sort=reverse`) took 25-52 s and timed out after 30 s, so large sites never got an answer. The
alternative `fastLatest` returned a wrong date (2013 instead of the same day) for domain-wide
queries.

## Decision

- The first and last capture come from two exact-host queries (`limit=1` and `limit=-1`), which
  answer in about a second.
- The sample of the domain's archived URLs (a count capped at 100) still needs the domain-wide
  query. It runs alongside, with its own 12 s limit. If it is late, the dates stay valid,
  `urls_archived_sample` is `None` and the error is `sample-timeout`.
- The report shows such a partial answer instead of hiding it.

## Consequences

- python.org went from no Wayback data to first capture 1997 and last capture the same day, in
  about 12 s.
- On large sites the URL-count sample is usually missing. The dates are not affected.
