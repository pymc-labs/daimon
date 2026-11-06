"""Rule contract: every routine destination check decides with the shared core rule.

A routine posts on its creator's behalf, so the chat tool that saves it, the
web app that saves it and the delivery that posts it must agree on where the
creator may post. The rule lives in core (`routine_delivery.discord_creator_may_post`,
`routine_delivery.slack_creator_may_post`, `routine_destinations`); each surface
supplies the platform facts it read. A surface with its own copy of the rule
drifts from the others without a failing test, so this walks each surface's
code and fails when it stops reaching the rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_ADAPTERS = _ROOT / "packages" / "adapters"

#: The core rule functions a destination check must reach.
_RULES = frozenset(
    {
        "discord_creator_may_post",
        "slack_creator_may_post",
        "slack_may_deliver",
        "discord_deliverable_channels",
    }
)
#: Calls that read the facts the rule decides on.
_FACT_READS = frozenset({"permissions_for", "conversations_info", "conversations_members"})

#: (module, function): each surface's destination check.
_SURFACES = (
    ("mcp/daimon/adapters/mcp/tools/routines.py", "_resolve_discord_destination"),
    ("mcp/daimon/adapters/mcp/tools/routines.py", "_resolve_slack_destination"),
    ("discord/daimon/adapters/discord/routine_delivery.py", "creator_may_post"),
    ("slack/daimon/adapters/slack/routine_delivery.py", "_destination_refusal"),
    ("web/daimon/adapters/web/channel_visibility.py", "_discord"),
    ("web/daimon/adapters/web/channel_visibility.py", "_slack"),
)


def _call_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _call_graph(source: str) -> dict[str, frozenset[str]]:
    """Each module-level function's called names."""
    graph: dict[str, frozenset[str]] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            graph[node.name] = frozenset(
                name
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and (name := _call_name(call)) is not None
            )
    return graph


def _reaches_rule(function: str, graph: dict[str, frozenset[str]]) -> bool:
    seen: set[str] = set()
    pending = [function]
    while pending:
        name = pending.pop()
        if name in seen or name not in graph:
            continue
        seen.add(name)
        if graph[name] & _RULES:
            return True
        pending.extend(graph[name])
    return False


def _called_from(roots: set[str], graph: dict[str, frozenset[str]]) -> set[str]:
    seen: set[str] = set()
    pending = [callee for root in roots for callee in graph[root]]
    while pending:
        name = pending.pop()
        if name in seen or name not in graph:
            continue
        seen.add(name)
        pending.extend(graph[name])
    return seen


@pytest.mark.parametrize(("module", "function"), _SURFACES)
def test_each_surface_decides_a_destination_with_the_core_rule(module: str, function: str) -> None:
    graph = _call_graph((_ADAPTERS / module).read_text(encoding="utf-8"))
    assert function in graph, f"{module} no longer defines {function}: update _SURFACES"
    assert _reaches_rule(function, graph), (
        f"{module}:{function} decides where a routine may post without the core rule "
        f"({', '.join(sorted(_RULES))}); supply the facts to it instead of a local copy"
    )


def test_every_routine_module_fact_read_reaches_the_rule() -> None:
    """A new check in a routine module that reads posting facts must use the rule too."""
    modules = sorted(_ADAPTERS.glob("*/daimon/adapters/*/**/routine*.py"))
    assert modules, "the walk found no routine module"
    offenders = []
    for path in modules:
        graph = _call_graph(path.read_text(encoding="utf-8"))
        deciders = {name for name in graph if _reaches_rule(name, graph)}
        # A helper that only reads facts for a decider is part of that decider.
        helpers = _called_from(deciders, graph)
        offenders.extend(
            f"{path.relative_to(_ADAPTERS)}:{name}"
            for name, calls in graph.items()
            if calls & _FACT_READS and name not in deciders | helpers
        )
    assert offenders == [], f"routine destination facts read without the core rule: {offenders}"


def test_the_contract_flags_a_local_copy_of_the_rule() -> None:
    source = (
        "async def _resolve_slack_destination(client, channel_id):\n"
        "    info = await client.conversations_info(channel=channel_id)\n"
        "    return bool(info['channel'].get('is_member'))\n"
        "def _decide(perms):\n"
        "    return discord_creator_may_post(administrator=perms.administrator)\n"
        "async def _resolve_discord_destination(channel, member):\n"
        "    return _decide(channel.permissions_for(member))\n"
    )
    graph = _call_graph(source)
    assert not _reaches_rule("_resolve_slack_destination", graph)
    assert _reaches_rule("_resolve_discord_destination", graph)
