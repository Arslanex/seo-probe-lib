"""
The dependency rules of docs/architecture/overview.md (ADR 0002), checked on the source code itself.

* core imports no feature;
* a feature imports another feature only through its package root, only names in its
  ``__all__``, and only features it declares in ``FEATURE.depends_on``;
* the declared dependency graph has no cycle.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from seoprobe.features import FEATURES

SRC = Path(importlib.import_module("seoprobe").__file__ or "").parent
DECLARED = {f.name: set(f.depends_on) for f in FEATURES}


def _imports(path: Path) -> list[tuple[str, list[str], int]]:
    """(module, imported names, line) of every absolute import in ``path``."""
    out: list[tuple[str, list[str], int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module, [a.name for a in node.names], node.lineno))
        elif isinstance(node, ast.Import):
            out += [(a.name, [], node.lineno) for a in node.names]
    return out


def test_core_imports_no_feature() -> None:
    bad = [
        f"{p.relative_to(SRC)}:{line} {mod}"
        for p in (SRC / "core").rglob("*.py")
        for mod, _, line in _imports(p)
        if mod == "seoprobe.features" or mod.startswith("seoprobe.features.")
    ]
    assert not bad, bad


def test_features_use_each_other_only_through_declared_public_names() -> None:
    problems: list[str] = []
    for feature in FEATURES:
        root = SRC / "features" / feature.name
        for p in root.rglob("*.py"):
            for mod, names, line in _imports(p):
                if not mod.startswith("seoprobe.features."):
                    continue
                parts = mod.split(".")
                other = parts[2]
                where = f"{p.relative_to(SRC)}:{line}"
                if other == feature.name:
                    problems.append(f"{where}: use a relative import inside the own package ({mod})")
                    continue
                if len(parts) > 3:
                    problems.append(f"{where}: internal module of another feature ({mod})")
                    continue
                if other not in DECLARED[feature.name]:
                    problems.append(f"{where}: {feature.name} does not declare a dependency on {other}")
                public = set(getattr(importlib.import_module(mod), "__all__", []))
                hidden = [n for n in names if n not in public]
                if hidden:
                    problems.append(f"{where}: {other} does not export {hidden}")
    assert not problems, "\n".join(problems)


def test_declared_dependencies_exist_and_have_no_cycle() -> None:
    names = set(DECLARED)
    for feature, deps in DECLARED.items():
        assert deps <= names, (feature, deps - names)
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 1:
            raise AssertionError(f"dependency cycle: {' -> '.join([*path, n])}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in DECLARED[n]:
            visit(d, [*path, n])
        state[n] = 2

    for n in DECLARED:
        visit(n, [])


def test_every_feature_exports_its_operations_and_models() -> None:
    for feature in FEATURES:
        module = importlib.import_module(feature.package)
        public = set(getattr(module, "__all__", []))
        for op in feature.operations:
            assert getattr(module, op.__name__.split(".")[-1], None) is not None or op.name.split(".")[0] in public, (
                feature.name,
                op.name,
            )
        models = SRC / "features" / feature.name / "models.py"
        if models.exists():
            tree = ast.parse(models.read_text(encoding="utf-8"))
            classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef) and not n.name.startswith("_")]
            missing = [c for c in classes if c not in public]
            assert not missing, (feature.name, missing)
