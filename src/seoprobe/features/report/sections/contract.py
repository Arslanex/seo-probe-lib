"""
The contract every report section follows: it is *measured* (with data) or *not measured* (with
a reason), never an empty table; and the shared text handling (every string that came from a page
or from an external source goes through ``clean_text``; consumers must still render it as text).
The module blocks arrive as JSON in ``Audit.extras[kind]`` and are re-validated by ``_load``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from seoprobe.core.parse.text import clean_text
from seoprobe.core.rules.catalog import has_text, message, reason_text, t
from seoprobe.features.audit import Audit

from .. import models as s


def _c(text: object, limit: int = 300) -> str:
    return clean_text(str(text))[:limit]


def _code(error: str | None) -> str | None:
    if not error or not error.strip():
        return None
    return error.split(":")[0].strip().split()[0]


def not_measured(lang: str, reason: str) -> dict[str, Any]:
    return {
        "status": "not_measured",
        "not_measured_reason": reason,
        "not_measured_text": reason_text(lang, reason),
    }


M = TypeVar("M", bound=BaseModel)


def _label(lang: str, prefix: str, code: str) -> str:
    """Translated label for a stable code; the (cleaned) code itself when there is no entry."""
    key = f"{prefix}.{code}"
    return t(lang, key) if has_text(key) else _c(code, 120)


def _codes(lang: str, prefix: str, details: str) -> str:
    return ", ".join(_label(lang, prefix, x.strip()) for x in details.split(",") if x.strip())


def _load(rec: Audit, kind: str, model: type[M]) -> M | str:
    """The validated block, or the reason code why there is none."""
    raw = rec.extras.get(kind)
    if raw is None:
        return "suite-no-data"
    try:
        return model.model_validate(raw)
    except ValidationError:
        return "suite-bad-data"


def _why(rec: Audit, *check_ids: str) -> str:
    """Reason a module produced nothing: its own NotChecked code when it left one."""
    for n in rec.not_checked:
        if n.check_id in check_ids:
            return n.reason_code
    return "suite-no-data"


def _nm(lang: str, reason: str) -> dict[str, Any]:
    return not_measured(lang, reason)


def _vis_error(lang: str, err: object) -> str:
    code = _code(str(err)) or "unknown"
    key = f"vis.err.{code}"
    if has_text(key):
        return t(lang, key)
    return t(lang, "vis.err.unknown", code=_c(code, 40))


def _day(value: object) -> str:
    return _c(value, 40)[:10]


def _yn(lang: str, v: bool) -> str:
    return t(lang, "idx.yes" if v else "idx.no")


def check_rows(category_checks: Iterable[Any], lang: str) -> list[s.CheckRow]:
    rows: list[s.CheckRow] = []
    for ck in list(category_checks)[:120]:
        key = f"finding.{ck.check_id}"
        text = message(key, lang) if has_text(key) else ck.check_id
        rows.append(
            s.CheckRow(
                check_id=ck.check_id,
                message=_c(text),
                severity=ck.severity,
                weight=ck.weight,
                pass_ratio=max(0.0, min(1.0, ck.pass_ratio)),
                pages_affected=ck.pages_affected,
                pages_assessed=max(1, ck.pages_assessed),
                status=ck.status,
            )
        )
    return rows
