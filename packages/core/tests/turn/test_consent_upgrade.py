"""`consent_upgrade.covers`: the one place a stored consent is compared with a target."""

from __future__ import annotations

import ast
from pathlib import Path

import daimon.core
from daimon.core.stores.domain import _BackendDropConsent  # pyright: ignore[reportPrivateUsage]
from daimon.core.turn.consent_upgrade import covers
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec
from mux.state.operations import request_digest

SWITCH = "mcp_tool:daimon-mcp:request_agent_backend_switch"


def copy_for(
    model: str = "gpt-6-luna",
    *,
    drops: tuple[str, ...] = ("memory",),
    url: str = "https://a.invalid/mcp",
) -> AgentSpec:
    return AgentSpec(
        name="copy",
        model=ModelRef(provider="openai", id=model),
        system="Source.\n\nUnavailable capabilities: " + ", ".join(drops) + ".",
        tools=(ToolSpec(name="bash", kind="builtin"),),
        mcp_servers=(
            MCPConnection(
                name="daimon-mcp",
                url=url,
                credential_ref="turn",
                tool_policy={"allowed_tools": ["describe_agent"]},
            ),
        ),
        declared_drops=drops,
    )


def consent_for(target: AgentSpec) -> _BackendDropConsent:
    return _BackendDropConsent(
        declared_drops=target.declared_drops,
        source_pin="a" * 64,
        target_digest=request_digest(target.model_dump(mode="json")),
    )


def holds(consent: _BackendDropConsent, target: AgentSpec | None, model: str | None = None) -> bool:
    return covers(
        consent, target, source_system="Source.", backend="openai", model=model, picked=model
    )


def test_covers_when_the_target_is_the_one_confirmed() -> None:
    target = copy_for()
    assert holds(consent_for(target), target), "the confirmed target is covered"
    assert not holds(consent_for(target), None), "no target is never covered"


def test_covers_refuses_another_model_with_the_same_losses() -> None:
    """The model stays pinned: same drops on a different model ask again."""
    then = copy_for()
    other = copy_for("gpt-6-other")
    assert not covers(
        consent_for(then),
        other,
        source_system="Source.",
        backend="openai",
        model="gpt-6-other",
        picked="gpt-6-luna",
    ), "a model change must re-confirm"


def test_covers_accepts_the_switch_tool_dropped_by_a_rollback() -> None:
    """Turning the switch gate off drops the tool from agents confirmed with it offered."""
    then = copy_for()
    today = copy_for(drops=("memory", SWITCH))
    assert holds(consent_for(then), today, "gpt-6-luna"), "a Daimon-decided drop is no new loss"


def test_covers_refuses_a_gained_loss_the_owner_never_accepted() -> None:
    then = copy_for()
    today = copy_for(drops=("builtin_tool:web_fetch", "memory"))
    assert not holds(consent_for(then), today, "gpt-6-luna"), "a new loss asks again"


def test_no_module_compares_a_consent_digest_outside_the_helper() -> None:
    """Both operand orders of `== ...target_digest` live only in `consent_upgrade`."""
    root = Path(next(iter(daimon.core.__path__)))
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name == "consent_upgrade.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            if any(isinstance(o, ast.Attribute) and o.attr == "target_digest" for o in operands):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, f"compare consents through consent_upgrade.covers: {offenders}"
