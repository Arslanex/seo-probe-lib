# 0010. Extract the main text without trafilatura's fallback extractors

**Status:** Accepted · **Date:** 2026-11-23 · **Supersedes:** archive/plan.md §11 #27

## Context

Duplicate detection, readability and keyword checks use a page's main text, extracted with
trafilatura. trafilatura 2.4.0 changed its fallback path: on pages with a short article, it
returned the whole page, a site-wide menu included. Every page of a site then looked like a
duplicate of the others. A fresh install picked up 2.4.0, and a test caught the false duplicates.

## Decision

- Main text is extracted with `trafilatura.extract(..., fast=True)`, which skips the fallback
  extractors. If nothing is extracted, the page's visible text is used, as before.
- Two other options were measured and rejected. `favor_precision=True` broke the parity with the
  source project. An upper bound `trafilatura<2.4` would have forced a dependency conflict on
  every application that needs a newer trafilatura.

## Consequences

- Results are the same with trafilatura 2.3 and 2.4. On 39 test pages and 6 real sites the text
  was identical to the default extraction, and extraction was up to seven times faster.
- Pages with unusual markup, where the main extractor finds little, lose the fallback's second
  opinion.
