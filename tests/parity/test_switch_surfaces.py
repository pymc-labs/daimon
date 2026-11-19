"""Every model-switch surface applies a pick at once, through one shared write.

Surfaces are found from the code (the AST): any module that previews or picks a switch
is listed with the helper it applies through, or allowed with its reason. Each helper
writes in the same call; none asks first.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
ROOTS: Final = (REPO / "packages/core/daimon", *sorted(REPO.glob("packages/adapters/*/daimon")))

#: Calls that preview, pick or write a switch.
ENTRY_POINTS: Final = frozenset(
    {
        "preview_or_refusal",
        "preview_model_switch",
        "pick_or_refusal",
        "pick_thread",
        "thread_revision",
        "change_model",
        "switch_provider",
        "set_backend",
        "preview",
    }
)

#: Each switch surface and the shared helper it applies a pick through.
SURFACES: Final = {
    "adapters/discord/daimon/adapters/discord/commands/model.py": "pick_thread",
    "adapters/discord/daimon/adapters/discord/agent_setup/details_view.py": "pick_or_refusal",
    "adapters/slack/daimon/adapters/slack/agent_setup/actions.py": "pick_or_refusal",
    "adapters/teams/daimon/adapters/teams/setup_panel.py": "pick_or_refusal",
    "adapters/web/daimon/adapters/web/agents.py": "switch_provider",
    "adapters/web/daimon/adapters/web/chat_thread_model.py": "pick_thread",
    "adapters/mcp/daimon/adapters/mcp/tools/agents.py": "change_model",
    "adapters/mcp/daimon/adapters/mcp/tools/thread_model.py": "pick_thread",
    "core/daimon/core/thread_model_flow.py": "pick_thread",
}

#: Modules that preview or write a switch but are not a surface a person picks on.
ALLOWED: Final = {
    "adapters/cli/daimon/adapters/cli/commands/agents.py": (
        "Operator CLI: prints the preview and confirms every change like its other commands"
    ),
    "core/daimon/core/agent_changes.py": "Home of change_model and switch_provider",
    "core/daimon/core/backend_refusals.py": "The refusal card's Switch back to Claude button",
    "core/daimon/core/channel_backend.py": "The provider store: previews and writes",
    "core/daimon/core/model_switch.py": "Home of pick_or_refusal",
    "core/daimon/core/thread_model_switch.py": "Home of pick_thread",
}

#: Each shared helper, its module, and the write it makes in the same call.
WRITES: Final = {
    "pick_or_refusal": ("core/daimon/core/model_switch.py", "change_model"),
    "change_model": ("core/daimon/core/agent_changes.py", "set_backend"),
    "switch_provider": ("core/daimon/core/agent_changes.py", "write_provider_switch"),
    "pick_thread": ("core/daimon/core/thread_model_switch.py", "_keep"),
    "_keep": ("core/daimon/core/thread_model_switch.py", "set_thread_model"),
}


@cache
def _tree(rel: str) -> ast.Module:
    return ast.parse((REPO / "packages" / rel).read_text())


def _names(node: ast.AST) -> set[str]:
    """Every name a node refers to: bare names, attributes and imports."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
        elif isinstance(sub, ast.ImportFrom):
            found.update(alias.name for alias in sub.names)
    return found


def _calls_entry_point(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in ENTRY_POINTS:
                return True
    return False


def _discovered() -> set[str]:
    packages = REPO / "packages"
    return {
        str(path.relative_to(packages))
        for root in ROOTS
        for path in root.rglob("*.py")
        if _calls_entry_point(_tree(str(path.relative_to(packages))))
    }


def test_every_module_that_previews_a_switch_is_listed() -> None:
    """A new caller of a switch preview or write must say how it applies the pick."""
    found = _discovered()
    listed = SURFACES.keys() | ALLOWED.keys()
    assert not found - listed, f"unlisted switch callers: {sorted(found - listed)}"
    assert not listed - found, f"listed but no longer switching: {sorted(listed - found)}"


def test_every_surface_applies_through_its_shared_helper() -> None:
    """Each surface calls the shared helper, never a copy of its rule."""
    missing = {rel: via for rel, via in SURFACES.items() if via not in _names(_tree(rel))}
    assert not missing, f"surfaces skipping their helper: {missing}"


def test_every_helper_writes_in_the_same_call() -> None:
    """A pick is written by the call that makes it: no helper stops to ask first."""
    for helper, (rel, write) in WRITES.items():
        defs = [
            node
            for node in ast.walk(_tree(rel))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == helper
        ]
        assert defs, f"{helper} not defined in {rel}"
        assert write in _names(defs[0]), f"{helper} no longer writes through {write}"
