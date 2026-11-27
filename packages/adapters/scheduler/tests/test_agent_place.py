"""A routine fire and an agent step reach their place through one admission."""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

from daimon.adapters.scheduler.workflows import lane_turn
from daimon.core.fire import run
from daimon.core.workflows import agent_step

#: The checks `fire.admit.admit_place` owns: access, hold and origin.
OWNED = {"authorize", "routine_hold", "routine_origin", "lapsed_role"}


def test_only_admit_place_runs_the_access_hold_and_origin_checks() -> None:
    for module in (run, lane_turn, agent_step):
        tree = ast.parse(Path(_file(module)).read_text())
        called = {
            node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        assert not called & OWNED, f"{module.__name__} checks {called & OWNED} itself"
        assert "admit_place" in called or module is agent_step, module.__name__


def _file(module: ModuleType) -> str:
    assert module.__file__ is not None
    return module.__file__
