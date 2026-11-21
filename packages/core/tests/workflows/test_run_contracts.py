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


#: Session openers a workflow step reaches, and the keywords each must pass so the
#: session carries its run and step stamps and its usage settles to the run.
_STAMPED = {
    "run_turn": {"step", "usage_record_factory"},
    "record_turn_usage": {"workflow_run_id"},
    "source_metadata": {"MA_METADATA_KEY_WORKFLOW_RUN", "MA_METADATA_KEY_WORKFLOW_STEP"},
}
#: Deliberate gaps, each with its reason.
_UNSTAMPED = {
    "owner_dm.py:run_dm_routine": "an owner DM step continues the owner's DM session, which "
    "bills through the DM's own usage path and is not a workflow session",
}


def test_every_workflow_session_is_stamped_and_settles_through_usage_recording() -> None:
    found: dict[str, set[str]] = {}
    roots = ("core/daimon/core/workflows", "adapters/scheduler/daimon/adapters/scheduler/workflows")
    for path in (p for root in roots for p in sorted((PACKAGES / root).glob("*.py"))):
        for call in ast.walk(ast.parse(path.read_text())):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "partial" and call.args and isinstance(call.args[0], ast.Name):
                name = call.args[0].id
            passed = {k.arg for k in call.keywords if k.arg}
            for kw in call.keywords:
                if kw.arg == "source_metadata" and isinstance(kw.value, ast.Dict):
                    name = "source_metadata"
                    passed = {k.id for k in kw.value.keys if isinstance(k, ast.Name)}
            if name in _STAMPED or name == "run_dm_routine":
                found[f"{path.name}:{name}"] = passed
    assert set(found) == {
        "lane_turn.py:run_turn",
        "lane_turn.py:record_turn_usage",
        "code_worker.py:source_metadata",
        *_UNSTAMPED,
    }, f"a new session path: stamp it or list it with its reason: {sorted(found)}"
    for where, passed in found.items():
        if where not in _UNSTAMPED:
            missing = _STAMPED[where.split(":")[1]] - passed
            assert not missing, f"{where} passes no {sorted(missing)}"
