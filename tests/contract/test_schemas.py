"""
Every operation's output JSON Schema changes only together with its ``schema_version``.

The snapshots live in ``tests/fixtures/schemas/<feature>/<operation>.json``. When an output model
changes on purpose: bump ``schema_version`` in the operation's ``@operation(...)`` (a new field is
minor, a removed or re-meant field is major: "Versioning" in docs/architecture/overview.md), record it in CHANGELOG.md and
regenerate with ``python -m tests.contract.test_schemas``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import seoprobe

SNAPSHOTS = Path(__file__).resolve().parents[1] / "fixtures" / "schemas"
OPERATIONS = {op.name: op for op in seoprobe.operations()}


def _path(feature: str, name: str) -> Path:
    return SNAPSHOTS / feature / f"{name}.json"


def _snapshot(op: Any) -> dict[str, Any]:
    return {"schema_version": op.schema_version, "schema": op.output_schema}


@pytest.mark.parametrize("name", sorted(OPERATIONS))
def test_output_schema_matches_its_snapshot(name: str) -> None:
    op = OPERATIONS[name]
    path = _path(op.feature, name)
    assert path.exists(), f"new operation {name}: python -m tests.contract.test_schemas"
    saved = json.loads(path.read_text())
    if saved["schema"] != op.output_schema and saved["schema_version"] == op.schema_version:
        pytest.fail(f"the output schema of {name} changed: bump schema_version, add a CHANGELOG entry, regenerate")
    assert saved == _snapshot(op), f"intended? python -m tests.contract.test_schemas ({name})"


def test_no_snapshot_without_an_operation() -> None:
    expected = {_path(op.feature, name) for name, op in OPERATIONS.items()}
    assert set(SNAPSHOTS.rglob("*.json")) <= expected, "stale snapshots: python -m tests.contract.test_schemas"


if __name__ == "__main__":  # pragma: no cover
    for stale in SNAPSHOTS.rglob("*.json"):
        stale.unlink()
    for name, op in OPERATIONS.items():
        path = _path(op.feature, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_snapshot(op), indent=1, sort_keys=True) + "\n")
