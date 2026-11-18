"""Every model-switch surface asks through one rule, `needs_switch_card`.

Surfaces are found from the code (the AST): any module that previews a switch is listed
with the gated helper it goes through, or allowed with its reason.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[2]
ROOTS: Final = (REPO / "packages/core/daimon", *sorted(REPO.glob("packages/adapters/*/daimon")))

#: Calls that preview a switch or show its card.
ENTRY_POINTS: Final = frozenset(
    {
        "preview_or_refusal",
        "preview_thread_or_refusal",
        "preview_thread_model",
        "preview_model_switch",
        "pick_or_refusal",
        "pick_thread_or_card",
        "backend_switch_callbacks",
        "preview",
    }
)

#: Each switch surface and the gated helper it must call.
SURFACES: Final = {
    "adapters/discord/daimon/adapters/discord/commands/model.py": "pick_thread_or_card",
    "adapters/discord/daimon/adapters/discord/agent_setup/details_view.py": "pick_or_refusal",
    "adapters/slack/daimon/adapters/slack/agent_setup/actions.py": "pick_or_refusal",
    "adapters/teams/daimon/adapters/teams/setup_panel.py": "pick_or_refusal",
    "adapters/web/daimon/adapters/web/agents.py": "preview_doc",
    "adapters/web/daimon/adapters/web/chat_thread_model.py": "thread_asks_first",
    "adapters/mcp/daimon/adapters/mcp/tools/agents.py": "needs_switch_card",
    "adapters/mcp/daimon/adapters/mcp/tools/thread_model.py": "pick_thread_or_card",
    "core/daimon/core/thread_model_flow.py": "pick_thread_or_card",
    "core/daimon/core/thread_model_reask.py": "pick_thread_or_card",
}

#: Modules that preview a switch but are not a surface that decides on the card.
ALLOWED: Final = {
    "adapters/cli/daimon/adapters/cli/commands/agents.py": (
        "Operator CLI: prints the whole preview, confirms every change like its other"
        " commands, and takes losses only through --accept-drops"
    ),
    "adapters/discord/daimon/adapters/discord/tool_confirmation.py": (
        "Shows the hook card only when request_agent_backend_switch asks first"
    ),
    "adapters/slack/daimon/adapters/slack/tool_confirmation.py": (
        "Shows the hook card only when request_agent_backend_switch asks first"
    ),
    "adapters/teams/daimon/adapters/teams/tool_confirmation.py": (
        "Shows the hook card only when request_agent_backend_switch asks first"
    ),
    "core/daimon/core/agent_changes.py": (
        "Write path: checks consent a surface gathered; creating an agent takes its losses"
    ),
    "core/daimon/core/channel_backend.py": "The consent store: previews and checks digests",
    "core/daimon/core/confirmation.py": "Hook card text, from switch_card_lines",
    "core/daimon/core/model_switch.py": "Home of pick_or_refusal",
    "core/daimon/core/thread_model_switch.py": "Home of pick_thread_or_card",
    "core/daimon/core/switch_cards.py": "Posts a card a surface already chose to show",
}

#: Each gated helper and the module it lives in; each reaches `needs_switch_card`.
GATES: Final = {
    "asks_first": ("core/daimon/core/model_switch.py", "needs_switch_card"),
    "pick_or_refusal": ("core/daimon/core/model_switch.py", "asks_first"),
    "thread_asks_first": ("core/daimon/core/thread_model_switch.py", "asks_first"),
    "pick_thread_or_card": ("core/daimon/core/thread_model_switch.py", "thread_asks_first"),
    "preview_doc": ("adapters/web/daimon/adapters/web/model_preview.py", "asks_first"),
}

#: Modules that draw the card's two buttons; they take the shared labels.
BUTTONS: Final = (
    "core/daimon/core/switch_cards.py",
    "adapters/discord/daimon/adapters/discord/commands/model.py",
    "adapters/discord/daimon/adapters/discord/agent_setup/details_view.py",
    "adapters/slack/daimon/adapters/slack/agent_setup/panel_views.py",
    "adapters/slack/daimon/adapters/slack/thread_model.py",
    "adapters/teams/daimon/adapters/teams/setup_card.py",
    "adapters/teams/daimon/adapters/teams/thread_model.py",
)


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
    """A new caller of a switch preview must say how it decides on the card."""
    found = _discovered()
    listed = SURFACES.keys() | ALLOWED.keys()
    assert not found - listed, f"unlisted switch previews: {sorted(found - listed)}"
    assert not listed - found, f"listed but no longer previewing: {sorted(listed - found)}"


def test_every_surface_goes_through_its_gate() -> None:
    """Each surface calls the helper that applies needs_switch_card."""
    missing = {rel: gate for rel, gate in SURFACES.items() if gate not in _names(_tree(rel))}
    assert not missing, f"surfaces skipping their gate: {missing}"


def test_every_gate_reaches_the_one_rule() -> None:
    """Each gated helper calls the rule, directly or through another gate."""
    for gate, (rel, via) in GATES.items():
        defs = [
            node
            for node in ast.walk(_tree(rel))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == gate
        ]
        assert defs, f"{gate} not defined in {rel}"
        assert via in _names(defs[0]), f"{gate} no longer goes through {via}"
        assert via == "needs_switch_card" or via in GATES, f"{via} is not a gate"


def test_every_card_takes_the_shared_button_labels() -> None:
    """Cancel and Switch read the same on every platform."""
    for rel in BUTTONS:
        names = _names(_tree(rel))
        assert {"CANCEL_LABEL", "SWITCH_LABEL"} <= names, f"{rel} words its own buttons"
    web = (REPO / "web/src/modules/chat/ThreadModel.tsx").read_text()
    assert web.index(">Cancel</Button>") < web.index(">Switch</Button>"), "web card buttons"
