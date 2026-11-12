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
PUBLISH = "mcp_tool:daimon-mcp:publish_report"
CARDED = ("describe_agent", "publish_report")
GEMINI = "gemini-3.8-flash"


def copy_for(
    model: str = "gpt-6-luna",
    *,
    provider: str = "openai",
    drops: tuple[str, ...] = ("memory",),
    url: str = "https://a.invalid/mcp",
    allowed: tuple[str, ...] = ("describe_agent",),
    relays: tuple[str, ...] = (),
    relay_ref: str = "turn",
) -> AgentSpec:
    return AgentSpec(
        name="copy",
        model=ModelRef(provider=provider, id=model),
        system="Source.\n\nUnavailable capabilities: " + ", ".join(drops) + ".",
        tools=(ToolSpec(name="bash", kind="builtin"),),
        mcp_servers=(
            MCPConnection(
                name="daimon-mcp",
                url=url,
                credential_ref="turn",
                tool_policy={"allowed_tools": list(allowed)},
            ),
            *(
                MCPConnection(name=name, url=f"{url}/connectors/{name}", credential_ref=relay_ref)
                for name in relays
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


def holds(
    consent: _BackendDropConsent,
    target: AgentSpec | None,
    model: str | None = None,
    backend: str = "openai",
) -> bool:
    return covers(
        consent, target, source_system="Source.", backend=backend, model=model, picked=model
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


def _regained(consent: _BackendDropConsent, today: AgentSpec) -> bool:
    """Only the regained-tool rule can pass: the picked model is another one."""
    return covers(consent, today, source_system="Source.", backend="openai", model="x", picked="y")


def test_covers_a_gated_tool_the_call_gate_offers_again() -> None:
    """With the gate on, a tool the consent named lost runs behind a card: no re-confirm."""
    then = copy_for(drops=(PUBLISH, "memory"))
    today = copy_for(allowed=CARDED)
    assert _regained(consent_for(then), today), "a carded tool regained is consented"


def test_a_regained_tool_still_pins_the_rest_of_the_target() -> None:
    """Regaining a carded tool never lets a model or MCP URL change through."""
    consent = consent_for(copy_for(drops=(PUBLISH, "memory")))
    moved = copy_for(allowed=CARDED, url="https://b.invalid/mcp")
    other = copy_for("gpt-6-other", allowed=CARDED)
    assert not _regained(consent, moved), "an MCP URL change asks again"
    assert not _regained(consent, other), "a model change asks again"


def test_only_a_carded_tool_may_be_regained() -> None:
    """A regained loss Daimon neither cards nor covers with its own tool, or a new loss, asks again."""
    fetch = consent_for(copy_for(drops=("builtin_tool:web_fetch", "memory")))
    assert not _regained(fetch, copy_for()), "a regained web tool asks again"
    hosted = copy_for(allowed=("describe_agent", "web_fetch"))
    assert _regained(fetch, hosted), "Daimon's own web_fetch covers the provider's"
    moved = copy_for(allowed=("describe_agent", "web_fetch"), url="https://b.invalid/mcp")
    assert not _regained(fetch, moved), "the rest of the target is still pinned"
    lost = consent_for(copy_for(drops=(PUBLISH, "memory")))
    assert not _regained(lost, copy_for(allowed=CARDED, drops=("memory", "skills"))), "new loss"


def test_the_gate_turned_off_is_a_daimon_decided_drop() -> None:
    """A rollback drops gated tools again; agents confirmed with them on are not asked."""
    then = copy_for(allowed=CARDED)
    today = copy_for(drops=(PUBLISH, "memory"))
    assert holds(consent_for(then), today, "gpt-6-luna"), "the gate's drop is no new loss"


LINEAR = "mcp_server:linear"


def test_covers_a_connector_the_relay_mounts_again() -> None:
    """A connector the consent named lost runs through the relay on each person's grant."""
    consent = consent_for(copy_for(drops=(LINEAR, "memory")))
    assert _regained(consent, copy_for(relays=("linear",))), "a relayed connector is consented"
    both = consent_for(copy_for(drops=(LINEAR, PUBLISH, "memory")))
    assert _regained(both, copy_for(allowed=CARDED, relays=("linear",))), "tool and connector"


def test_a_relayed_connector_still_pins_the_rest_of_the_target() -> None:
    """Only a relay server on Daimon's own credential and URL counts as a regain."""
    consent = consent_for(copy_for(drops=(LINEAR, "memory")))
    moved = copy_for(relays=("linear",), url="https://b.invalid/mcp")
    foreign = copy_for(relays=("linear",), relay_ref="other")
    assert not _regained(consent, moved), "an MCP URL change asks again"
    assert not _regained(consent, foreign), "a relay on another credential asks again"


def test_the_relay_turned_off_is_a_daimon_decided_drop() -> None:
    """With the relay gate off a connector is lost again; its owner is not asked."""
    then = copy_for(relays=("linear",))
    today = copy_for(drops=(LINEAR, "memory"))
    assert holds(consent_for(then), today, "gpt-6-luna"), "the relay's drop is no new loss"
    assert holds(consent_for(today), then, "gpt-6-luna"), "the relay's mount is no new gain"


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


def test_covers_a_template_that_attaches_the_skills_it_lost_whole() -> None:
    """Fewer losses never ask again: the skills attach, one over the size limit is named."""
    then = copy_for(drops=("memory", "skills"))
    assert holds(consent_for(then), copy_for(), "gpt-6-luna"), "every skill attaches"
    today = copy_for(drops=("memory", "skill:huge"))
    assert holds(consent_for(then), today, "gpt-6-luna"), "one skill is less than all of them"


def test_a_skill_loss_the_owner_never_accepted_asks_again() -> None:
    today = copy_for(drops=("memory", "skill:huge"))
    assert not holds(consent_for(copy_for()), today, "gpt-6-luna"), "a new loss asks again"


def test_a_switched_gemini_agent_keeps_its_consent_when_skills_attach() -> None:
    """A Gemini agent confirmed with its skills lost whole is not asked again."""
    gemini = {"model": GEMINI, "provider": "gemini"}
    then = copy_for(**gemini, drops=("memory", "skills"))
    today = copy_for(**gemini, drops=("memory", "skill:huge"))
    assert holds(consent_for(then), today, GEMINI, "gemini"), "one skill is less than all"
    assert holds(consent_for(then), copy_for(**gemini), GEMINI, "gemini"), "every skill attaches"
