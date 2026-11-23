# How the score is calculated

Every audit ends with a score from 0 to 100 for the whole site (or page) and one per category.
This page explains where those numbers come from, so you can explain them to your customers. The
code is in `src/seoprobe/core/rules/scoring.py`. It is deterministic: the same findings always
give the same score.

A term you do not know is probably in the [glossary](../glossary.md).

## Categories and their weights

| Category | Weight | What it covers |
|---|---|---|
| `technical` | 20 | HTTPS, redirects, robots.txt, sitemaps, indexability, security headers |
| `content` | 20 | text quality, readability, keywords, duplicate and thin content |
| `onpage` | 15 | titles, descriptions, headings, canonical tags |
| `performance` | 12 | Core Web Vitals and page weight |
| `links` | 10 | broken and redirecting links, link structure |
| `schema` | 8 | structured data (JSON-LD) |
| `ai_readiness` | 8 | how AI crawlers and agents can read the site |
| `images` | 7 | alt text, dimensions, formats, lazy loading |

The overall score is the weighted average of the categories that could be measured. The weights
of the others are shared out among them.

A **page audit** always scores `onpage`, `content`, `technical`, `schema` and `images`, and adds
`performance`, `ai_readiness` and `links` only when it has the data for them. A **site audit**
can score all eight.

## How a category is scored

A category's score is the share of its applicable checks that passed, weighted by severity:

| Severity | Weight |
|---|---|
| Critical | 8 |
| High | 4 |
| Medium | 2 |
| Low | 1 |
| Info | 0, never scored |

For each check:

- **A page-level rule** (for example "missing meta description") passes in proportion to the
  pages it did not hit. If 2 of 10 sampled pages miss a description, that check passes at 0.8.
- **A site-level rule** (for example "no XML sitemap") passes or fails as a whole.
- **Rule families** that ask one question in several ways count as one check, so a family with
  seven variants does not dilute the score. Examples are the TLS problems, the HSTS problems and
  the Core Web Vitals thresholds. The family weighs as much as its most severe
  member, and only the worst finding present counts. One High and two Low TLS findings cost the
  family's full weight once, not three times. Two Low findings alone cost only part of it.

Then:

```text
category score = 100 × Σ(weight × pass ratio) / Σ(weight)
penalty of a check = 100 × weight × (1 − pass ratio) / Σ(weight)
```

The penalties are listed with the score, so every lost point can be traced to a finding.

## What does not count

- **Rules that could not run.** If a check is reported in `not_checked` (no API key, a page
  that could not be rendered, a time budget that ran out), it is left out of the calculation
  instead of being counted as passed. The score covers only what was measured.
- **Info findings.** They are shown but carry no weight.
- **Rules the source project did not have.** These are monitoring, mobile, accessibility, DNS,
  carbon, search opportunities and link suggestions. They have `"scored": false` in their
  catalogue: they are reported, but they do not move the score. This keeps the score identical
  to the project seoprobe was ported from (see
  [ADR 0005](../architecture/adr/0005-parity-and-unscored-rules.md)).
  `seoprobe.rules()` lists every rule with its category, severity and scope.

## Caps for show-stoppers

Some problems make a site unusable in search however good the rest is. When one of them affects
the audited page, the overall score is capped:

| Problem | Cap |
|---|---|
| the page answers 4xx or 5xx, or HTTPS fails to connect | 40 |
| `noindex` in the page or its headers; an expired, untrusted or wrong-host certificate | 40 |
| the canonical tag points to another page; robots.txt blocks the whole site | 70 |

The cap and its reason are part of the result (`scores.gate`), so you can show why a site with
few findings still scores 40.

## Coverage, confidence and "insufficient data"

- **`coverage_pct`** is the share of the category weight that could be measured. Below 60 %, the
  overall score is withheld (`insufficient_data: true`): a number based on too little would
  mislead.
- **`confidence`** is a hint for how much the score can be trusted:
  - `low` when coverage is under 75 % or more than a third of the checks could not run;
  - `medium` for a site audit of fewer than 30 pages, or a page audit with less than 90 %
    coverage;
  - `high` otherwise.
- **`core_overall`** (site audits) is the score of the core pages only: the sample pages chosen to
  represent each section of the site. It is the number to track over time. **`p25`** is the page score that a quarter of the pages fall below.

## Bands in the report

The report turns the overall score into a band for display:

| Score | Band |
|---|---|
| 80 and above | `good` |
| 50 to 79.9 | `attention` |
| below 50 | `poor` |

## Languages and the rule catalogue

Every rule lives in a `catalog.json` next to the code that checks it. The catalogue holds the
rule's id, category, severity, scope (page or site), whether it is scored, and the keys of its
texts. The texts themselves (the message, the recommendation, how to confirm the fix) exist in
English and Turkish, and a test fails if one is missing. `Config(lang="tr")` switches every
finding and report text to Turkish. The rule ids and the score are the same in both languages.

## Versions

`scores.score_version` (currently `mvp-0.2`) names the scoring formula and `rule_version` (0.1.0)
the rule set. Scores are comparable when both are the same, and `report.compare` warns when they
differ.
