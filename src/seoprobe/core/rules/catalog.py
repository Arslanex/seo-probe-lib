"""
Rule definitions and localized texts.

Every package that owns rules or texts ships a ``catalog.json``::

    {"rules": {"<id>": {<CheckDef fields>}}, "messages": {"en": {key: text}, "tr": {key: text}}}

Catalogs are read lazily on first use. ``core``'s own catalog is always installed; feature
catalogs are installed by ``seoprobe.features`` from its explicit ``FEATURES`` list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal

from seoprobe.core.models import Category, Finding, Layer, Severity

RULE_VERSION = "0.1.0"
LANGS = ("en", "tr")
DEFAULT_LANG = "en"


@dataclass(frozen=True)
class CheckDef:
    id: str  # kebab-case, stable
    category: Category
    severity: Severity  # initial severity; may be lowered by context
    layer: Layer = Layer.A
    scope: Literal["page", "site"] = "page"
    heuristic: bool = False
    effort_default: Literal["S", "M", "L"] = "S"
    impact_default: Literal["low", "medium", "high"] = "medium"
    message_key: str = ""
    recommendation_key: str = ""
    failure_check_key: str = ""
    leading_indicator_key: str = ""
    depends_on: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()  # "headers" | "rendered" | "lab" | "psi" | "robots" | "sitemap"
    modes: tuple[str, ...] = ("page", "site")
    references: tuple[str, ...] = ()
    since: str = RULE_VERSION
    scored: bool = True  # False: reported, never part of the score (rules the source project did not have)

    def __post_init__(self) -> None:
        if not self.message_key:
            object.__setattr__(self, "message_key", f"finding.{self.id}")
        if not self.recommendation_key:
            object.__setattr__(self, "recommendation_key", f"rec.{self.id}")


class Catalog:
    """The ``catalog.json`` of one package, read on first access."""

    def __init__(self, package: str, path: str = "catalog.json", *, feature: str = "") -> None:
        self.package = package
        self.path = path
        self.feature = feature or package.rsplit(".", 1)[-1]
        self._rules: dict[str, CheckDef] | None = None
        self._messages: dict[str, dict[str, str]] | None = None

    def _load(self) -> None:
        resource = files(self.package).joinpath(self.path)
        data: dict[str, Any] = json.loads(resource.read_text(encoding="utf-8")) if resource.is_file() else {}
        rules: dict[str, CheckDef] = {}
        for rid, row in data.get("rules", {}).items():
            row = dict(row)
            for key in ("depends_on", "requires", "modes", "references"):
                row[key] = tuple(row.get(key, ()))
            row["category"] = Category(row["category"])
            row["severity"] = Severity(row["severity"])
            row["layer"] = Layer(row.get("layer", "A"))
            rules[rid] = CheckDef(id=rid, **row)
        self._rules = rules
        self._messages = {lang: dict(data.get("messages", {}).get(lang, {})) for lang in LANGS}

    @property
    def rules(self) -> dict[str, CheckDef]:
        if self._rules is None:
            self._load()
        assert self._rules is not None
        return self._rules

    @property
    def messages(self) -> dict[str, dict[str, str]]:
        if self._messages is None:
            self._load()
        assert self._messages is not None
        return self._messages


CORE_CATALOG = Catalog("seoprobe.core", "data/catalog.json", feature="core")
_installed: list[Catalog] = [CORE_CATALOG]
_merged: tuple[dict[str, CheckDef], dict[str, dict[str, str]], dict[str, str]] | None = None


def install(catalogs: list[Catalog]) -> None:
    """Add feature catalogs (idempotent). Called once from ``seoprobe.features``."""
    global _merged
    known = {(c.package, c.path) for c in _installed}
    for c in catalogs:
        if (c.package, c.path) not in known:
            _installed.append(c)
            known.add((c.package, c.path))
    _merged = None


def installed() -> list[Catalog]:
    return list(_installed)


def _view() -> tuple[dict[str, CheckDef], dict[str, dict[str, str]], dict[str, str]]:
    global _merged
    if _merged is None:
        rules: dict[str, CheckDef] = {}
        owner: dict[str, str] = {}
        messages: dict[str, dict[str, str]] = {lang: {} for lang in LANGS}
        for c in _installed:
            for rid, d in c.rules.items():
                if rid in rules and rules[rid] != d:
                    raise ValueError(f"duplicate rule id with different definition: {rid}")
                rules[rid] = d
                owner[rid] = c.feature
            for lang in LANGS:
                messages[lang].update(c.messages[lang])
        _merged = (rules, messages, owner)
    return _merged


def rules() -> dict[str, CheckDef]:
    return _view()[0]


def rule_feature(check_id: str) -> str:
    return _view()[2].get(check_id, "")


def get(check_id: str) -> CheckDef:
    return _view()[0][check_id]


def norm_lang(lang: str | None) -> str:
    return lang if lang in LANGS else DEFAULT_LANG


def message(key: str, lang: str = "en") -> str:
    """Rule text: the language, else English, else the key itself."""
    msgs = _view()[1]
    return msgs.get(lang, {}).get(key) or msgs["en"].get(key) or key


def t(lang: str, key: str, **params: object) -> str:
    """Translate a text key; unknown keys return the key (never raises, never leaks)."""
    msgs = _view()[1]
    text = msgs.get(norm_lang(lang), {}).get(key)
    if text is None:
        text = msgs["en"].get(key, key)
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def has_text(key: str) -> bool:
    return key in _view()[1]["en"]


def reason_text(lang: str, code: str | None) -> str:
    """Sentence for a reason code; non-code free text is returned unchanged."""
    if not code:
        return t(lang, "reason.unknown")
    key = f"reason.{code}"
    if has_text(key):
        return t(lang, key)
    return code


def make_finding(
    check_id: str,
    *,
    url: str | None = None,
    evidence: object = None,
    count: int | None = None,
    severity: Severity | None = None,
    severity_reason: str | None = None,
    confidence: Literal["high", "medium", "low"] = "high",
    lang: str = "en",
) -> Finding:
    """Build a Finding from the catalog (evidence is trimmed to 200 chars)."""
    d = get(check_id)
    sev = severity or d.severity
    ev = None if evidence is None else str(evidence).strip()[:200]
    rec = message(d.recommendation_key, lang) if d.recommendation_key else None
    return Finding(
        id=d.id,
        rule_version=d.since,
        category=d.category,
        layer=d.layer,
        severity=sev,
        severity_original=d.severity if severity and severity != d.severity else None,
        severity_reason=severity_reason if severity and severity != d.severity else None,
        scope=d.scope,
        url=url,
        message_key=d.message_key,
        message=message(d.message_key, lang),
        evidence=ev,
        count=count,
        heuristic=d.heuristic,
        confidence=confidence,
        recommendation_key=d.recommendation_key,
        recommendation=None if rec == d.recommendation_key else rec,
        effort=d.effort_default,
        impact=d.impact_default,
    )


def localize(finding: Finding, lang: str) -> Finding:
    """The same finding with its texts in ``lang``."""
    rec = message(finding.recommendation_key, lang) if finding.recommendation_key else None
    return finding.model_copy(
        update={
            "message": message(finding.message_key, lang),
            "recommendation": None if rec == finding.recommendation_key else rec,
        }
    )
