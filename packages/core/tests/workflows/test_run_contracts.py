"""Contracts found by AST: `start_run` is the only way a run is created, and preflight
runs before a run is created or a step spends.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[3]
RUNS = PACKAGES / "core/daimon/core/workflows/runs.py"


def _functions(tree: ast.Module) -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _called(node: ast.AST, name: str) -> list[int]:
    return [
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (
            (isinstance(call.func, ast.Name) and call.func.id == name)
            or (isinstance(call.func, ast.Attribute) and call.func.attr == name)
        )
    ]


def test_start_run_is_the_only_run_creator() -> None:
    creators: set[str] = set()
    for path in PACKAGES.glob("**/daimon/**/*.py"):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _called(fn, "create_run"):
                creators.add(f"{path.name}:{fn.name}:create_run")
            if _called(fn, "WorkflowRun"):
                creators.add(f"{path.name}:{fn.name}:WorkflowRun")
    assert creators == {
        "runs.py:start_run:create_run",
        "workflow_runs.py:create_run:WorkflowRun",
    }, f"runs are created only through start_run: {sorted(creators)}"


def test_preflight_runs_before_a_run_is_created_or_a_step_spends() -> None:
    fns = _functions(ast.parse(RUNS.read_text()))
    for name, spend in (("start_run", "create_run"), ("execute", "executor")):
        checks, spends = _called(fns[name], "check"), _called(fns[name], spend)
        assert checks and spends and max(checks) < min(spends), f"{name} checks before {spend}"


def test_every_trigger_path_starts_runs_through_start_run_and_ingress_starts_none() -> None:
    callers: set[str] = set()
    for path in PACKAGES.glob("**/daimon/**/*.py"):
        for fn in ast.walk(ast.parse(path.read_text())):
            named = isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name
            if named and named != "start_run" and _called(fn, "start_run"):
                callers.add(f"{path.name}:{named}")
    assert callers == {
        "service.py:start",  # manual and test runs, from the web, MCP and chat cards
        "service.py:rerun",
        "triggers.py:_start_one",  # schedule slots, webhook deliveries, finished runs
        "triggers.py:start_called",  # call triggers
        "webhooks.py:replay",
        "workflows.py:run",  # the test helper in daimon.testing
    }, f"a new path that starts runs joins this list: {sorted(callers)}"


def test_every_manual_start_is_a_listed_path() -> None:
    callers: set[str] = set()
    for path in PACKAGES.glob("**/daimon/**/*.py"):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                func = isinstance(call, ast.Call) and call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "start"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "service"
                ):
                    callers.add(f"{path.name}:{fn.name}")
    assert callers == {
        "chat_cards.py:start_on_card",  # Run and Test run pressed on a chat card
        "workflows.py:_run_workflow_impl",  # the MCP run tool
        "workflows.py:start",  # the web route
        "workflows.py:workflows_router",  # the route's enclosing router
    }, f"a new path that starts manual runs joins this list: {sorted(callers)}"
