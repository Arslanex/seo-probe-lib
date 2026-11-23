# 0007. Results cross process boundaries; sessions stay on their event loop

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §11 #22, #23, #25

## Context

Integrators run seoprobe in thread pools, process pools and task queues (Celery, RQ). Two things
broke there. Results could not be pickled, because the generic `Result[Any]` class was created at
runtime. A session used from another thread's event loop failed with obscure aiohttp errors or
hung.

## Decision

- Operations build their result with the module-level `Result` class. An error keeps the
  original exception for debugging. Pickling drops it: it might not pickle, and its traceback
  means nothing in another process anyway.
  `SeoprobeError` and `OperationError` define `__reduce__`.
- Every result also round-trips through JSON (`to_json()`, `model_validate_json()`).
- A `Session` remembers the event loop that opened it. Using it from another loop, or opening it
  twice, raises a clear `RuntimeError`.
- `Session.listening(hook)` adds an event listener for a block without replacing `on_event`, so
  concurrent `audit_stream` runs on one session do not interfere.
- The library changes no global setting of the host. The one exception is the `trafilatura`
  logger, which is quietened only when the application has not set its level.

## Consequences

- A process pool or task queue can carry results and errors directly.
- The rule for integrators is simple: one session per event loop (per API process, per worker
  job). See [saas_integration.md](../../guides/saas_integration.md).
