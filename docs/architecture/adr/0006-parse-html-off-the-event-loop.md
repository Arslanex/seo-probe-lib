# 0006. Parse HTML on a worker thread, never on the caller's event loop

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §11 #21

## Context

seoprobe runs inside other applications' event loops, for example a web server. Parsing a page
with BeautifulSoup, lxml and trafilatura is CPU work: measured, a 1 MB page blocked the loop for
about 140 ms and a 5 MB page for about 670 ms. During that time the host application could not
answer anyone.

## Decision

- `Page.parse()` computes the parsed page on a worker thread (`asyncio.to_thread`) and caches it.
  `Context.page`, the crawl, hreflang, audit and monitoring paths, and the operations that parse
  directly (`classify`, `competitors`) all use it.
- Other CPU-heavy work runs the same way: log analysis, link-suggestion similarity and the
  blocking TLS handshake check.
- Anything that changes process-wide state stays out of the hot path. The warnings filter around
  trafilatura's import runs once, not on every parse, because `warnings.catch_warnings` is not
  thread-safe.

## Consequences

- The host's event loop stays responsive whatever the page size.
- Parsing shares the loop's default thread pool with the host. Very parallel workloads may want a
  larger default executor.
