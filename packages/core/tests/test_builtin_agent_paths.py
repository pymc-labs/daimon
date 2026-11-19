"""Every identity path asks `is_builtin_agent`, never compares a hard-coded agent name."""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGES = Path(__file__).resolve().parents[2]


def _identity_calls() -> list[tuple[str, ast.expr]]:
    calls: list[tuple[str, ast.expr]] = []
    for path in _PACKAGES.glob("**/daimon/**/*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "resolve_agent_identity":
                continue
            calls.extend(
                (f"{path.relative_to(_PACKAGES)}:{node.lineno}", keyword.value)
                for keyword in node.keywords
                if keyword.arg == "is_builtin"
            )
    return calls


def test_resolve_agent_identity_when_called_then_builtin_never_compares_a_name() -> None:
    calls = _identity_calls()
    assert len(calls) >= 10, f"the AST scan should find the identity paths, found {len(calls)}"
    literal = [
        where
        for where, value in calls
        if any(
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            for node in ast.walk(value)
        )
    ]
    assert not literal, f"use agent_identity.is_builtin_agent instead of a name check: {literal}"
