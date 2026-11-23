"""
Technology detection: CMS, framework, CDN, analytics, ... from what the entry page already
returned (headers, HTML, scripts, cookies). No extra requests. Informational: it produces no
findings. A signature that fits plain text (a page *talking about* jQuery) does not count:
script/link signatures only look at URLs, cookie signatures only at cookie names.

Signature database (``techstack.json``, package data): a signature is one regex on one *kind* of
evidence: a response header (``key`` = header name; a key ending in ``-`` matches any header with
that prefix), a ``<meta name>`` content, a script/link URL, a cookie name, or the raw HTML.
Regexes are short and bounded; they only ever run on the first ``MAX_HTML`` characters.
``(?P<v>...)`` captures a version.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Literal

from seoprobe.core.feature import Context, Outcome, operation
from seoprobe.core.models import Requirements
from seoprobe.core.page import Page

from .models import TechItem, TechStack

PatternType = Literal["header", "meta", "script", "html", "cookie"]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class Signature:
    type: PatternType
    key: str | None
    regex: re.Pattern[str]
    confidence: Confidence


@dataclass(frozen=True)
class Technology:
    name: str
    category: str
    website: str | None
    signatures: tuple[Signature, ...]


@lru_cache(maxsize=1)
def load_technologies() -> tuple[Technology, ...]:
    raw = json.loads(files(__package__).joinpath("techstack.json").read_text(encoding="utf-8"))
    techs: list[Technology] = []
    for t in raw["technologies"]:
        sigs = tuple(
            Signature(
                p["type"],
                (p.get("key") or None) and p["key"].lower(),
                re.compile(p["regex"], re.IGNORECASE),
                p.get("confidence", "medium"),
            )
            for p in t["patterns"]
        )
        techs.append(Technology(t["name"], t["category"], t.get("website"), sigs))
    return tuple(techs)


MAX_HTML = 300_000
MAX_URLS = 300
_RANK = {"low": 0, "medium": 1, "high": 2}
_SRC = re.compile(r"""<(?:script|link|img|iframe)\b[^>]{0,400}?\b(?:src|href)\s*=\s*["']([^"']{1,500})""", re.I)
_VALUE_CAP = 300


def _cookie_names(headers: dict[str, str]) -> list[str]:
    raw = headers.get("set-cookie", "")
    names = []
    for line in raw.split("\n"):
        name = line.split("=", 1)[0].strip()
        if name and len(name) < 100:
            names.append(name)
    return names


def _metas(page: Page, html: str) -> list[tuple[str, str]]:
    parsed = page.parsed
    if parsed is not None and getattr(parsed, "meta_tags", None):
        return [(n.lower(), c) for n, _p, c in parsed.meta_tags if n]
    out = []
    for m in re.finditer(r"<meta\b[^>]{0,400}>", html, re.I):
        tag = m.group(0)
        n = re.search(r"""name\s*=\s*["']([^"']{1,80})""", tag, re.I)
        c = re.search(r"""content\s*=\s*["']([^"']{0,200})""", tag, re.I)
        if n and c:
            out.append((n.group(1).lower(), c.group(1)))
    return out


def detect(headers: dict[str, str], html: str, metas: list[tuple[str, str]]) -> TechStack:
    html = html[:MAX_HTML]
    urls = [m.group(1) for m in _SRC.finditer(html)][:MAX_URLS]
    cookies = _cookie_names(headers)
    found: dict[str, list[tuple[str | None, str, Confidence]]] = {}
    techs = {t.name: t for t in load_technologies()}
    for tech in techs.values():
        for sig in tech.signatures:
            cands: list[tuple[str, str]] = []  # (text to match, evidence label)
            if sig.type == "header":
                for hk, hv in headers.items():
                    if sig.key and (hk == sig.key or (sig.key.endswith("-") and hk.startswith(sig.key))):
                        cands.append((hv[:_VALUE_CAP], f"header {hk}"))
            elif sig.type == "meta":
                cands = [(c[:_VALUE_CAP], f"meta {n}") for n, c in metas if n == sig.key]
            elif sig.type == "script":
                cands = [(u, "script/link URL") for u in urls]
            elif sig.type == "cookie":
                cands = [(c, f"cookie {c}") for c in cookies]
            else:
                cands = [(html, "HTML marker")]
            for text, label in cands:
                m = sig.regex.search(text)
                if m:
                    ver = m.groupdict().get("v") if sig.regex.groupindex.get("v") else None
                    found.setdefault(tech.name, []).append((ver, label, sig.confidence))
                    break
    items: list[TechItem] = []
    for name, matches in found.items():
        tech = techs[name]
        best = max((c for _v, _l, c in matches), key=lambda c: _RANK[c])
        labels = list(dict.fromkeys(label for _v, label, _c in matches))
        if len(labels) >= 2 and best == "medium":
            best = "high"
        version = next((v for v, _l, _c in matches if v), None)
        items.append(
            TechItem(
                name=name,
                category=tech.category,
                version=version,
                evidence=", ".join(labels[:3]),
                confidence=best,
                website=tech.website,
            )
        )
    items.sort(key=lambda i: (i.category, -_RANK[i.confidence], i.name))
    by_cat: dict[str, list[str]] = {}
    for i in items:
        by_cat.setdefault(i.category, []).append(i.name)
    return TechStack(items=items, by_category=by_cat)


@operation("techstack", output=TechStack, requires=Requirements(network=True), timeout_s=20.0)
async def techstack(ctx: Context, target: str | Page) -> Outcome[TechStack]:
    """Technologies (CMS, framework, CDN, analytics, ...) seen in the page's response."""
    page = await ctx.page(target)
    html = page.content_html
    return Outcome(data=detect(page.headers, html, _metas(page, html[:MAX_HTML])))
