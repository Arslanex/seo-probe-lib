"""
Content language: readability, top terms, target keyword audit.

Pure computation over the page's main text. The numbers are descriptive; every finding here is
either a plain presence check (keyword missing in title/H1/description) or a heuristic
(``readability-hard``, ``long-sentences``, ``keyword-stuffing``).

Limits, stated plainly:

* Readability exists for English (Flesch Reading Ease, Flesch-Kincaid grade) and Turkish
  (Atesman 1997). Other languages get NO number.
* English syllables are counted heuristically (vowel groups, silent final ``e``), Turkish syllables
  as the number of vowels (a e i i o o u u), which is exact for native words and only approximate
  for loanwords with consonant clusters. The Atesman *formula* is documented; the syllable counter
  is ours and not validated against a corpus.
* Terms are counted as they appear after case folding (Turkish dotted/dotless i folded). Turkish is
  agglutinative and NO stemming or suffix matching is done: ``araba`` and ``arabanin`` are two terms.
* Keyword matching is exact on folded word sequences, so the same limit applies to Turkish keywords.
* Text is capped at ``MAX_WORDS`` words so that a hostile page cannot make this slow.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from importlib.resources import files
from typing import Any

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Finding, NotChecked, Requirements
from seoprobe.core.page import Page
from seoprobe.core.rules.catalog import make_finding

from .models import KeywordCheck, ReadabilityData

STUFFING_PERCENT = 3.0
LONG_SENTENCE_SHARE = 0.30
MIN_WORDS = 50  # below this a readability score says nothing

RULE_IDS = (
    "readability-hard",
    "long-sentences",
    "keyword-missing-in-title",
    "keyword-missing-in-h1",
    "keyword-missing-in-description",
    "keyword-stuffing",
)

# ----------------------------------------------------------------------------- text statistics

MAX_WORDS = 100_000
LONG_SENTENCE_WORDS = 25
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n{2,}|\n")
_EN_VOWELS = re.compile(r"[aeiouy]+")
_TR_VOWELS = frozenset("aeıioöuü")


def fold(text: str) -> str:
    """Case-insensitive form that treats Turkish ``İ``/``ı`` like ``i``."""
    t = unicodedata.normalize("NFKC", text).casefold()
    return t.replace("̇", "").replace("ı", "i")


def tokens(text: str, limit: int = MAX_WORDS) -> list[str]:
    out: list[str] = []
    for m in _WORD.finditer(fold(text)):
        out.append(m.group(0))
        if len(out) >= limit:
            break
    return out


def display_forms(text: str, limit: int = MAX_WORDS) -> dict[str, str]:
    """Folded token -> its most common spelling on the page, so ``programi`` shows as ``programı``."""
    seen: dict[str, Counter[str]] = {}
    for n, m in enumerate(_WORD.finditer(unicodedata.normalize("NFKC", text))):
        if n >= limit:
            break
        surface = m.group(0).lower().replace("̇", "")
        seen.setdefault(fold(surface), Counter())[surface] += 1
    return {k: c.most_common(1)[0][0] for k, c in seen.items()}


def show(term: str, forms: dict[str, str]) -> str:
    """Display spelling of a folded term or phrase (words missing from ``forms`` stay as they are)."""
    return " ".join(forms.get(w, w) for w in term.split(" "))


def sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in _SENTENCE_END.split(text[:2_000_000])) if _WORD.search(s)]


def paragraph_count(text: str) -> int:
    parts = [p for p in re.split(r"\n\s*\n|\n", text[:2_000_000]) if _WORD.search(p)]
    return max(1, len(parts)) if _WORD.search(text) else 0


def syllables_en(word: str) -> int:
    w = re.sub(r"[^a-z]", "", fold(word))
    if not w:
        return 1
    if len(w) > 2 and w.endswith("e") and not w.endswith(("le", "ee")):
        w = w[:-1]
    return max(1, len(_EN_VOWELS.findall(w)))


def syllables_tr(word: str) -> int:
    return max(1, sum(1 for ch in word.casefold() if ch in _TR_VOWELS))


def supported_language(lang: str | None) -> str | None:
    code = (lang or "").lower().replace("_", "-").split("-")[0]
    return code if code in ("en", "tr") else None


def readability_scores(text: str, lang: str) -> dict[str, Any] | None:
    """Readability numbers for ``text`` in ``lang`` ("en" | "tr"), or None if there is no text."""
    words = _WORD.findall(text[:2_000_000])[:MAX_WORDS]
    sents = sentences(text)
    if not words or not sents:
        return None
    counter = syllables_tr if lang == "tr" else syllables_en
    syll = sum(counter(w) for w in words)
    n_words, n_sents = len(words), len(sents)
    wps = n_words / n_sents
    spw = syll / n_words
    long_share = sum(1 for s in sents if len(_WORD.findall(s)) > LONG_SENTENCE_WORDS) / n_sents
    out: dict[str, Any] = {
        "words": n_words,
        "sentences": n_sents,
        "paragraphs": paragraph_count(text),
        "avg_sentence_words": round(wps, 1),
        "avg_syllables_per_word": round(spw, 2),
        "long_sentence_share": round(long_share, 3),
    }
    if lang == "tr":
        score = 198.825 - 40.175 * spw - 2.610 * wps
        out["formula"] = "atesman"
        out["score"] = round(score, 1)
        out["label"] = _atesman_label(score)
    else:
        score = 206.835 - 1.015 * wps - 84.6 * spw
        out["formula"] = "flesch"
        out["score"] = round(score, 1)
        out["grade"] = round(0.39 * wps + 11.8 * spw - 15.59, 1)
        out["label"] = _flesch_label(score)
    return out


def _flesch_label(s: float) -> str:
    for limit, label in (
        (90, "very-easy"),
        (70, "easy"),
        (50, "fairly-difficult"),
        (30, "difficult"),
    ):
        if s >= limit:
            return label
    return "very-difficult"


def _atesman_label(s: float) -> str:
    for limit, label in ((90, "very-easy"), (70, "easy"), (50, "medium"), (30, "difficult")):
        if s >= limit:
            return label
    return "very-difficult"


def is_very_difficult(r: dict[str, Any]) -> bool:
    """Flesch < 30 or Atesman < 40 (this module's own cut-offs, not a search engine rule)."""
    s = float(r["score"])
    return s < 40 if r["formula"] == "atesman" else s < 30


@lru_cache(maxsize=1)
def _stopword_lists() -> dict[str, frozenset[str]]:
    raw = json.loads(files("seoprobe.core").joinpath("data/stopwords.json").read_text(encoding="utf-8"))
    return {k: frozenset(fold(w) for w in v) for k, v in raw.items() if isinstance(v, list)}


def stopwords(lang: str | None) -> frozenset[str]:
    lists = _stopword_lists()
    code = supported_language(lang)
    if code:
        return lists.get(code, frozenset())
    return lists.get("en", frozenset()) | lists.get("tr", frozenset())


def top_terms(toks: list[str], stop: frozenset[str], *, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    """Most frequent single terms and 2-3 word phrases (edges must not be stopwords)."""
    total = len(toks) or 1
    uni = Counter(t for t in toks if len(t) >= 3 and t not in stop and not t.isdigit())
    result: dict[str, list[dict[str, Any]]] = {
        "terms": [{"term": t, "count": c, "density": round(100 * c / total, 2)} for t, c in uni.most_common(limit)]
    }
    phrases: Counter[str] = Counter()
    for n in (2, 3):
        for i in range(len(toks) - n + 1):
            first, last = toks[i], toks[i + n - 1]
            if first in stop or last in stop or len(first) < 2 or len(last) < 2:
                continue
            if first.isdigit() or last.isdigit():
                continue
            phrases[" ".join(toks[i : i + n])] += 1
    result["phrases"] = [{"term": t, "count": c} for t, c in phrases.most_common(limit * 3) if c >= 2][:limit]
    return result


def count_phrase(haystack: list[str], phrase: list[str]) -> int:
    """Occurrences of the word sequence ``phrase`` in ``haystack``."""
    n = len(phrase)
    if n == 0 or len(haystack) < n:
        return 0
    first = phrase[0]
    count = 0
    for i, w in enumerate(haystack[: len(haystack) - n + 1]):
        if w == first and haystack[i : i + n] == phrase:
            count += 1
    return count


def parse_keywords(raw: object, limit: int = 10) -> list[str]:
    """Keywords from a list or a comma/newline separated string; trimmed, de-duplicated, capped."""
    items: list[str]
    if isinstance(raw, str):
        items = re.split(r"[,\n;]", raw)
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = " ".join(it.split())[:80]
        f = fold(k)
        if k and f not in seen:
            seen.add(f)
            out.append(k)
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------- analysis


def _url_tokens(url: str) -> list[str]:
    return tokens(re.sub(r"[-_/.+%]", " ", url))


def check_keyword(
    keyword: str,
    *,
    body: list[str],
    title: str,
    h1: list[str],
    description: str,
    url: str,
    subheadings: list[str],
) -> KeywordCheck:
    phrase = tokens(keyword)
    n = len(phrase)
    count = count_phrase(body, phrase)
    density = round(100 * count * n / len(body), 2) if body and n else 0.0
    return KeywordCheck(
        keyword=keyword,
        in_title=count_phrase(tokens(title), phrase) > 0,
        in_h1=any(count_phrase(tokens(h), phrase) > 0 for h in h1),
        in_description=count_phrase(tokens(description), phrase) > 0,
        in_url=count_phrase(_url_tokens(url), phrase) > 0,
        in_first_100_words=count_phrase(body[:100], phrase) > 0,
        in_subheadings=any(count_phrase(tokens(h), phrase) > 0 for h in subheadings),
        occurrences=count,
        density_percent=density,
    )


def analyze(
    *,
    text: str,
    lang: str | None,
    title: str,
    h1: list[str],
    description: str,
    outline: list[tuple[int, str]],
    url: str,
    keywords: list[str],
) -> tuple[ReadabilityData, list[Finding], list[NotChecked]]:
    data = ReadabilityData(language=lang)
    findings: list[Finding] = []
    not_checked: list[NotChecked] = []
    body = tokens(text)
    code = supported_language(lang)

    if code is None:
        not_checked.append(NotChecked(check_id="readability", reason_code="language-unsupported", url=url))
    elif len(body) < MIN_WORDS:
        not_checked.append(NotChecked(check_id="readability", reason_code="too-little-text", url=url))
    else:
        r = readability_scores(text, code)
        data.readability = r
        if r:
            if is_very_difficult(r):
                findings.append(make_finding("readability-hard", url=url, evidence=f"{r['formula']} {r['score']}"))
            if r["long_sentence_share"] > LONG_SENTENCE_SHARE:
                findings.append(
                    make_finding(
                        "long-sentences",
                        url=url,
                        evidence=f"{round(r['long_sentence_share'] * 100)}% of sentences > 25 words",
                    )
                )

    stop = stopwords(lang)
    tt = top_terms(body, stop)
    data.terms, data.phrases = tt["terms"], tt["phrases"]
    head_tokens = set(tokens(" ".join([title, *h1, description])))
    data.title_terms = [t for t in data.terms if t["term"] in head_tokens]
    forms = display_forms(text)  # matching uses folded tokens; the report shows real spellings
    for row in (*data.terms, *data.phrases):  # title_terms shares the rows of data.terms
        row["term"] = show(row["term"], forms)

    if not keywords:
        not_checked.append(NotChecked(check_id="keyword-targeting", reason_code="no-keywords", url=url))
        data.suggested_targets = [p["term"] for p in data.phrases[:3]] or [t["term"] for t in data.terms[:3]]
        return data, findings, not_checked

    subheadings = [t for lvl, t in outline if lvl in (2, 3)]
    for kw in keywords:
        if not tokens(kw):
            continue
        kc = check_keyword(
            kw,
            body=body,
            title=title,
            h1=h1,
            description=description,
            url=url,
            subheadings=subheadings,
        )
        data.keywords.append(kc)
        if not kc.in_title:
            findings.append(make_finding("keyword-missing-in-title", url=url, evidence=kw))
        if not kc.in_h1:
            findings.append(make_finding("keyword-missing-in-h1", url=url, evidence=kw))
        if description and not kc.in_description:
            findings.append(make_finding("keyword-missing-in-description", url=url, evidence=kw))
        if kc.density_percent > STUFFING_PERCENT:
            findings.append(make_finding("keyword-stuffing", url=url, evidence=f"{kw}: {kc.density_percent}%"))
    return data, findings, not_checked


@operation("readability", output=ReadabilityData, requires=Requirements(network=True), timeout_s=20)
async def readability(
    ctx: Context,
    target: str | Page,
    *,
    keywords: list[str] | str | None = None,
    lang: str | None = None,
) -> Outcome[ReadabilityData]:
    """
    Flesch / Atesman readability, frequent terms and the target keyword audit.

    ``keywords``: a list or a comma/newline separated string (at most 10 are used). ``lang``: the
    page language; defaults to the page's ``lang`` attribute.
    """
    page = await ctx.page(target)
    p = page.parsed
    if p is None:
        return Outcome(
            data=ReadabilityData(),
            not_checked=[ctx.not_checked("readability", "parse-failed", page.url)],
        )
    text = p.main_text or p.visible_text or ""
    data, findings, nc = analyze(
        text=text,
        lang=lang or p.lang or None,
        title=p.title,
        h1=list(p.h1),
        description=p.meta_description,
        outline=list(p.outline),
        url=page.url,
        keywords=parse_keywords(keywords),
    )
    return Outcome(data=data, findings=findings, not_checked=nc)
