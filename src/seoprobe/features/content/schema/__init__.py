"""Structured data: JSON-LD checks (``checks``) and copy-paste JSON-LD suggestions (``suggest``)."""

from __future__ import annotations

from .checks import DEPRECATED_TYPES, SCHEMA_RULE_IDS, run_schema_checks, schema
from .suggest import NEVER_SUGGEST, product_gaps, suggest_for_page, suggest_schema, validate_json_ld

__all__ = [
    "DEPRECATED_TYPES",
    "NEVER_SUGGEST",
    "SCHEMA_RULE_IDS",
    "product_gaps",
    "run_schema_checks",
    "schema",
    "suggest_for_page",
    "suggest_schema",
    "validate_json_ld",
]
