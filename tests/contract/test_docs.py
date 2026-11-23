"""The documentation lists exactly what the code defines."""

from __future__ import annotations

import re
from pathlib import Path

from seoprobe.core.errors import RETRYABLE, ErrorCode

DOCS = Path(__file__).resolve().parents[2] / "docs"


def test_errors_md_lists_every_error_code_with_its_retry_flag() -> None:
    rows = re.findall(r"^\| `([a-z-]+)` \| (yes|no) \|", (DOCS / "errors.md").read_text(), flags=re.M)
    documented = {code: flag == "yes" for code, flag in rows}
    assert documented == {c.value: c in RETRYABLE for c in ErrorCode}


def render_operations_md() -> str:
    """docs/operations.md, generated from the operation registry."""
    import seoprobe
    from seoprobe.features import FEATURES

    order = ["core"] + [f.name for f in FEATURES]
    ops = sorted(seoprobe.operations(), key=lambda o: (order.index(o.feature), o.name))
    out = [
        "# Operations",
        "",
        "Generated from `seoprobe.operations()`; `tests/contract/test_docs.py` fails when it is out of date",
        "(regenerate with `python -m tests.contract.test_docs`). Every operation is called as",
        "`await seoprobe.<name>(...)` (dotted names are namespaces: `seoprobe.gsc.summary`) and returns",
        "`Result[<output>]`; `session=` and `timeout_s=` are accepted by all of them.",
        "",
    ]
    feature = None
    for op in ops:
        if op.feature != feature:
            feature = op.feature
            out += [f"## {feature}", "", "| Operation | Output | Requires | What it does |", "|---|---|---|---|"]
        req = op.requires
        needs = [
            x
            for x in ("network" if req.network else "", "browser" if req.browser else "", "oauth" if req.oauth else "")
            if x
        ]
        needs += [f"`[{e}]`" for e in req.extras] + [f"`{c}`" for c in req.credentials]
        title = op.output_schema.get("title", "")
        out.append(f"| `{op.name}` | `{title}` | {', '.join(needs) or '—'} | {op.description} |")
        if op is ops[-1] or ops[ops.index(op) + 1].feature != feature:
            out.append("")
    return "\n".join(out)


def test_operations_md_matches_the_registry() -> None:
    assert (DOCS / "operations.md").read_text() == render_operations_md(), (
        "regenerate: python -m tests.contract.test_docs"
    )


if __name__ == "__main__":  # pragma: no cover
    (DOCS / "operations.md").write_text(render_operations_md())
