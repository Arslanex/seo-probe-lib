"""
Every ``ErrorCode`` is produced by at least one test (ADR 0004), and no error detail is lost.

A test counts when it names the code as ``ErrorCode.<NAME>``: the kebab-case values ("timeout",
"not-html") are also HTTP client error strings, finding ids and reason codes, so matching them
would hide a code that nothing returns.
"""

from __future__ import annotations

import ast
from pathlib import Path

from seoprobe.core.errors import SAFE_DETAIL_KEYS, ErrorCode

TESTS = Path(__file__).resolve().parents[1]


def test_every_detail_an_operation_fails_with_is_kept() -> None:
    """
    ``OperationError`` drops unknown detail keys silently (they could leak); a key a caller
    passes on purpose must therefore be one of ``SAFE_DETAIL_KEYS``.
    """
    used: dict[str, str] = {}
    for path in (TESTS.parent / "src" / "seoprobe").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "fail":
                for kw in node.keywords:
                    if kw.arg is not None:
                        used.setdefault(kw.arg, f"{path.name}:{node.lineno}")
    dropped = {k: where for k, where in used.items() if k not in SAFE_DETAIL_KEYS}
    assert not dropped, f"detail keys that never reach ErrorInfo.details: {dropped}"


def test_every_error_code_is_named_in_a_behaviour_test() -> None:
    text = "".join(p.read_text(encoding="utf-8") for p in TESTS.rglob("*.py") if p.parent.name != "contract")
    missing = sorted(c.name for c in ErrorCode if f"ErrorCode.{c.name}" not in text)
    assert not missing, f"no test produces: {', '.join(missing)}"
