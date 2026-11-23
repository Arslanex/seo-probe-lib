"""
Near-duplicate content.

Word-shingle Jaccard similarity over the *main* text (``ParsedPage.main_text``, falling back to
``visible_text``) so that a shared navigation/footer does not make pages look alike.

* shingle = 5 consecutive words, folded for case and for the Turkish dotted/dotless i;
* a text with more than ``MAX_SHINGLES`` shingles is reduced to its ``MAX_SHINGLES`` smallest
  hashes (a bottom-k sketch; Jaccard on sketches approximates the exact value);
* the comparison is O(n^2): at most ``MAX_PAGES`` pages are compared, the rest is reported as
  ``not_checked`` (``sample-too-large``);
* the threshold (0.85) is a heuristic, not a search engine rule.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import CheckOutput, NotChecked, Requirements
from seoprobe.core.page import Page
from seoprobe.core.parse.urls import url_key
from seoprobe.core.rules.catalog import make_finding

from .models import DuplicateReport

SHINGLE = 5
MAX_SHINGLES = 5000
MAX_PAGES = 200
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def fold(text: str) -> str:
    """Case-insensitive form that also treats Turkish ``İ``/``ı`` like ``i``."""
    t = unicodedata.normalize("NFKC", text).casefold()
    return t.replace("̇", "").replace("ı", "i")


def words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def shingles(tokens: list[str]) -> set[int]:
    if len(tokens) < SHINGLE:
        return set()
    hashes = {
        int.from_bytes(
            hashlib.blake2b(" ".join(tokens[i : i + SHINGLE]).encode(), digest_size=8).digest(),
            "big",
        )
        for i in range(len(tokens) - SHINGLE + 1)
    }
    if len(hashes) > MAX_SHINGLES:
        hashes = set(sorted(hashes)[:MAX_SHINGLES])
    return hashes


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicates(pages: list[Page], *, threshold: float = 0.85, min_words: int = 80) -> CheckOutput:
    out = CheckOutput()
    candidates: list[tuple[Page, set[int]]] = []
    seen: set[str] = set()
    for p in pages:
        if p.parsed is None or p.error or p.status is None or p.status >= 400:
            continue
        key = url_key(p.effective_url)
        if key in seen:  # two sampled URLs that ended on the same page
            continue
        seen.add(key)
        tokens = words(p.parsed.main_text or p.parsed.visible_text)
        if len(tokens) < min_words:
            continue
        candidates.append((p, shingles(tokens)))

    # core pages first: they are the ones the trend score is built on
    candidates.sort(key=lambda c: 0 if c[0].sample_kind == "core" else 1)
    for p, _sh in candidates[MAX_PAGES:]:
        out.not_checked.append(NotChecked(check_id="duplicate-content", reason_code="sample-too-large", url=p.url))
    candidates = candidates[:MAX_PAGES]

    matches: dict[str, list[tuple[float, str]]] = {}
    for i in range(len(candidates)):
        pi, si = candidates[i]
        for j in range(i + 1, len(candidates)):
            pj, sj = candidates[j]
            sim = jaccard(si, sj)
            if sim >= threshold:
                matches.setdefault(pi.url, []).append((sim, pj.url))
                matches.setdefault(pj.url, []).append((sim, pi.url))

    for p, _sh in candidates:
        found = matches.get(p.url)
        if not found:
            continue
        best_sim, best_url = max(found, key=lambda m: (m[0], m[1]))
        out.findings.append(
            make_finding(
                "duplicate-content",
                url=p.url,
                evidence=f"{round(best_sim * 100)}% similar to {best_url}",
                count=len(found),
            )
        )
    out.facts["duplicate_pairs"] = sum(len(v) for v in matches.values()) // 2
    return out


@operation("duplicates", output=DuplicateReport, requires=Requirements(network=False), timeout_s=60.0)
async def duplicates(ctx: Context, pages: list[Page]) -> Outcome[DuplicateReport]:
    """Near-duplicate main text among the sampled pages (no network)."""
    out = find_duplicates(pages)
    return Outcome(
        data=DuplicateReport(duplicate_pairs=out.facts["duplicate_pairs"]),
        findings=list(out.findings),
        not_checked=list(out.not_checked),
    )
