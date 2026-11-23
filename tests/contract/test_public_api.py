"""
The public API (``seoprobe.__all__`` and every operation's signature) changes only on purpose.

When a change is intended, regenerate the snapshot with ``python -m tests.contract.test_public_api``
and record it in CHANGELOG.md (removals and signature changes need a deprecation period, see
"Versioning" in docs/architecture/overview.md).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import seoprobe

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "public_api.json"


def _describe(obj: Any) -> str:
    if isinstance(obj, type):
        return "class"
    if inspect.ismodule(obj):
        names = sorted(getattr(obj, "__all__", []))
        return "namespace: " + ", ".join(f"{n}{_signature(getattr(obj, n))}" for n in names)
    return "function" + _signature(obj)


def _signature(obj: Any) -> str:
    if isinstance(obj, type):  # model fields render differently across Python versions
        return ""
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return ""


def public_api() -> dict[str, str]:
    return {name: _describe(getattr(seoprobe, name)) for name in sorted(seoprobe.__all__) if name != "__version__"}


def test_public_api_matches_the_snapshot() -> None:
    assert public_api() == json.loads(SNAPSHOT.read_text()), "intended? python -m tests.contract.test_public_api"


if __name__ == "__main__":  # pragma: no cover
    SNAPSHOT.write_text(json.dumps(public_api(), indent=1, sort_keys=True) + "\n")
