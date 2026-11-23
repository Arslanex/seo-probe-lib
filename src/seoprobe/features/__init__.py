"""The feature registry: the explicit list of installed features and the questions asked of it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from seoprobe.core.feature import Feature, Operation, missing_requirements
from seoprobe.core.models import Preflight, Requirements
from seoprobe.core.page import fetch, normalize
from seoprobe.core.rules import catalog
from seoprobe.core.session import Session
from seoprobe.features import (
    audit,
    content,
    crawl,
    monitoring,
    offsite,
    page,
    performance,
    render,
    report,
    search,
    site,
)

FEATURES: list[Feature] = [
    page.FEATURE,
    content.FEATURE,
    site.FEATURE,
    crawl.FEATURE,
    render.FEATURE,
    performance.FEATURE,
    search.FEATURE,
    offsite.FEATURE,
    monitoring.FEATURE,
    audit.FEATURE,
    report.FEATURE,
]
CORE_OPERATIONS: list[Operation[Any]] = [fetch, normalize]

catalog.install([f.catalog for f in FEATURES])


class OperationInfo(BaseModel):
    name: str
    feature: str
    description: str
    requires: Requirements
    output_schema: dict[str, Any]
    schema_version: str


class AccessCheck(BaseModel):
    provider: str
    ok: bool
    code: str  # "ok" | "missing" | "rejected" | "unreachable" | "rate-limited" | ...


def _all_operations() -> list[Operation[Any]]:
    return CORE_OPERATIONS + [op for f in FEATURES for op in f.operations]


def get_operation(name: str) -> Operation[Any]:
    for op in _all_operations():
        if op.name == name:
            return op
    raise KeyError(f"unknown operation: {name}")


def operations() -> list[OperationInfo]:
    """Every public operation with its feature, requirements and output JSON Schema."""
    out = []
    for op in _all_operations():
        schema = (
            op.output.model_json_schema() if isinstance(op.output, type) and issubclass(op.output, BaseModel) else {}
        )
        out.append(
            OperationInfo(
                name=op.name,
                feature=op.feature,
                description=(op.__doc__ or "").strip().splitlines()[0] if op.__doc__ else "",
                requires=op.requires,
                output_schema=schema,
                schema_version=f"{op.name}/{op.schema_version}",
            )
        )
    return out


def requirements(name: str) -> Requirements:
    return get_operation(name).requires


async def preflight(name: str, *, session: Session) -> Preflight:
    """Whether ``name`` can run with this session's configuration (no network access)."""
    missing = missing_requirements(get_operation(name).requires, session)
    return Preflight(operation=name, ready=not missing, missing=missing)


async def check_access(provider: str, *, session: Session) -> AccessCheck:
    """Try the configured key or account of ``provider`` with one harmless request."""
    for f in FEATURES:
        check = f.access_checks.get(provider)
        if check is not None:
            ok, code = await check(session)
            return AccessCheck(provider=provider, ok=ok, code=code)
    raise KeyError(f"unknown provider: {provider}")


class RuleInfo(BaseModel):
    id: str
    feature: str
    category: str
    severity: str
    scope: str
    layer: str
    heuristic: bool
    requires: list[str]
    modes: list[str]


def rules() -> list[RuleInfo]:
    """The rule catalogue of every installed feature."""
    return [
        RuleInfo(
            id=d.id,
            feature=catalog.rule_feature(d.id),
            category=d.category.value,
            severity=d.severity.value,
            scope=d.scope,
            layer=d.layer.value,
            heuristic=d.heuristic,
            requires=list(d.requires),
            modes=list(d.modes),
        )
        for d in sorted(catalog.rules().values(), key=lambda d: d.id)
    ]
