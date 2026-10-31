"""Tests for daimon.core.subagents: the switch, the roster rule, the budget and the framing.

The write gate is exercised through `mux_compat.agent_spec`/`agent_patch`, the one
translation every agent create and update passes through.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.config import SubagentsSettings, load_subagents_settings
from daimon.core.context_prompt import FRAGMENT_MARKER
from daimon.core.mux_compat import agent_patch, agent_spec
from daimon.core.sessions import create_isolated_session, create_session
from daimon.core.subagents import (
    SubagentsError,
    check_multiagent_value,
    coordinator_system,
    require_session_allowed,
    session_budget,
    subagents_override,
    turn_budget,
)
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from mux.testing.drivers.anthropic.ma_models import (
    ma_agent,
    ma_environment,
    ma_session,
    ma_session_agent,
)

SELF_ROSTER = {"type": "coordinator", "agents": [{"type": "self"}]}


@pytest.fixture
def on() -> Iterator[None]:
    with subagents_override(SubagentsSettings(enabled=True)):
        yield


@pytest.fixture
def off() -> Iterator[None]:
    with subagents_override(SubagentsSettings(enabled=False)):
        yield


def test_helpers_are_off_unless_the_deployment_turns_them_on() -> None:
    assert load_subagents_settings(_env_file=None).enabled is False
    assert load_subagents_settings(_env_file=None).turn_budget_usd == Decimal("10")


def test_the_env_turns_them_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_SUBAGENTS__ENABLED", "true")
    monkeypatch.setenv("DAIMON_SUBAGENTS__TURN_BUDGET_USD", "2.5")
    settings = load_subagents_settings(_env_file=None)
    assert (settings.enabled, settings.turn_budget_usd) == (True, Decimal("2.5"))


@pytest.mark.usefixtures("off")
def test_a_roster_is_refused_while_off_and_clearing_always_passes() -> None:
    with pytest.raises(SubagentsError, match="turned off"):
        check_multiagent_value(SELF_ROSTER)
    check_multiagent_value(None)
    check_multiagent_value({"clear": True})


@pytest.mark.usefixtures("on")
@pytest.mark.parametrize(
    "value",
    [
        {"type": "coordinator", "agents": ["agent_01other"]},
        {"type": "coordinator", "agents": [{"type": "agent", "id": "agent_01other"}]},
        {"type": "coordinator", "agents": [{"type": "self"}, {"type": "self"}]},
        {"type": "coordinator", "agents": []},
        {"type": "multiagent_20261001"},
        {"type": "coordinator", "agents": [{"type": "self"}], "advisor": {"model": "x"}},
        "coordinator",
    ],
)
def test_only_copies_of_the_agent_itself_are_admitted(value: object) -> None:
    """A foreign roster entry brings its own tools and MCP servers into the lead's vault."""
    with pytest.raises(SubagentsError, match="copies of this agent"):
        check_multiagent_value(value)


@pytest.mark.usefixtures("on")
def test_the_self_roster_passes_when_on() -> None:
    check_multiagent_value(SELF_ROSTER)


@pytest.mark.usefixtures("off")
def test_every_agent_write_passes_the_gate() -> None:
    payload = {"name": "coder", "model": "claude-sonnet-5-5", "multiagent": SELF_ROSTER}
    with pytest.raises(SubagentsError):
        agent_spec(payload)
    with pytest.raises(SubagentsError):
        agent_patch({"multiagent": SELF_ROSTER})
    # Clearing a roster, and every agent without one, is untouched.
    cleared = agent_patch({"multiagent": None}).extensions or {}
    assert cleared["anthropic.multiagent"].value == {"clear": True}
    plain = agent_spec({"name": "coder", "model": "claude-sonnet-5-5"}).extensions or ()
    assert "anthropic.multiagent" not in [e.namespace for e in plain]


@pytest.mark.usefixtures("on")
def test_the_write_carries_the_self_roster_when_on() -> None:
    spec = agent_spec({"name": "coder", "model": "claude-sonnet-5-5", "multiagent": SELF_ROSTER})
    roster = [e for e in spec.extensions or () if e.namespace == "anthropic.multiagent"]
    assert [e.value for e in roster] == [SELF_ROSTER]


def test_a_coordinator_session_needs_a_chat_turn_and_the_switch() -> None:
    with (
        subagents_override(SubagentsSettings(enabled=False)),
        pytest.raises(SubagentsError, match="turned off"),
    ):
        require_session_allowed(SELF_ROSTER, chat=True)
    with subagents_override(SubagentsSettings(enabled=True)):
        with pytest.raises(SubagentsError, match="only work in a chat"):
            require_session_allowed(SELF_ROSTER, chat=False)
        require_session_allowed(SELF_ROSTER, chat=True)
    # An agent without helpers is never affected, on or off, chat or not.
    with subagents_override(SubagentsSettings(enabled=False)):
        require_session_allowed(None, chat=False)


def test_budgets_are_whole_cents_rounded_up() -> None:
    with subagents_override(SubagentsSettings(enabled=True, turn_budget_usd=Decimal("2.501"))):
        assert session_budget() == {
            "type": "limit",
            "max_list_cost": {"amount": "251", "currency": "USD"},
        }
        # A later turn: what the session has spent, plus one turn's worth.
        assert turn_budget(1234)["max_list_cost"] == {"amount": "1485", "currency": "USD"}
        assert turn_budget(-5)["max_list_cost"] == {"amount": "251", "currency": "USD"}


def test_the_framing_goes_before_the_tenant_context_line() -> None:
    with subagents_override(SubagentsSettings(enabled=True, max_concurrent=3)):
        system = "You code.\n" + FRAGMENT_MARKER + '{"fragments": []}'
        framed = coordinator_system(None, system, SELF_ROSTER)
        assert framed is not None
        prompt, marker, fragments = framed.partition(FRAGMENT_MARKER)
        assert prompt.startswith("You code.")
        assert "at most 3 helpers at once" in prompt
        assert "git worktree add" in prompt
        assert (marker, fragments) == (FRAGMENT_MARKER, '{"fragments": []}')
        # A platform variant is framed, not the stored prompt.
        assert coordinator_system("Variant.", system, SELF_ROSTER) is not None
        assert (coordinator_system("Variant.", system, SELF_ROSTER) or "").startswith("Variant.")
    # No helpers: whatever was going to be sent, unchanged.
    assert coordinator_system(None, "You code.", None) is None
    assert coordinator_system("Variant.", "You code.", None) == "Variant."


# --- the session gate, through create_session itself ---------------------------


def _coordinator_agent() -> BetaManagedAgentsAgent:
    plain = ma_agent(id="ag_coord", name="coder", model="claude-sonnet-5-5")
    # Managed Agents echoes a `self` entry as the agent's own id and version.
    return BetaManagedAgentsAgent.model_validate(
        {
            **plain.model_dump(mode="json"),
            "multiagent": {
                "type": "coordinator",
                "agents": [{"type": "agent", "id": "ag_coord", "version": 1}],
            },
        }
    )


def _recording_client(bodies: list[dict[str, Any]]) -> AsyncAnthropic:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path.endswith("/v1/sessions")
        body = json.loads(request.content)
        bodies.append(body)
        session = ma_session(
            id="sesn_coord", agent=ma_session_agent(model="claude-sonnet-5-5"), status="idle"
        )
        return httpx.Response(200, json=session.model_dump(mode="json"))

    return build_fake_anthropic(handler)


async def test_no_caller_but_a_chat_turn_starts_a_session_with_helpers() -> None:
    bodies: list[dict[str, Any]] = []
    client = _recording_client(bodies)
    agent, env = _coordinator_agent(), ma_environment(id="env_1", name="e")
    with subagents_override(SubagentsSettings(enabled=True)):
        for unattended in (
            create_session(client, agent=agent, environment=env),
            create_isolated_session(
                client, agent=agent, environment=env, account_id=None, tenant_id=None, resources=[]
            ),
        ):
            with pytest.raises(SubagentsError, match="only work in a chat"):
                await unattended
    with (
        subagents_override(SubagentsSettings(enabled=False)),
        pytest.raises(SubagentsError, match="turned off"),
    ):
        await create_session(client, agent=agent, environment=env, subagents=True)
    assert bodies == [], "refused before anything reached the provider"


async def test_a_chat_coordinator_session_is_created_with_its_spending_limit() -> None:
    bodies: list[dict[str, Any]] = []
    client = _recording_client(bodies)
    settings = SubagentsSettings(enabled=True, turn_budget_usd=Decimal("3"))
    with subagents_override(settings):
        await create_session(
            client,
            agent=_coordinator_agent(),
            environment=ma_environment(id="env_1", name="e"),
            subagents=True,
        )
        # An agent without helpers gets no budget, on or off.
        await create_session(
            client,
            agent=ma_agent(id="ag_plain", name="plain", model="claude-sonnet-5-5"),
            environment=ma_environment(id="env_1", name="e"),
        )
    assert bodies[0]["budget"] == {
        "type": "limit",
        "max_list_cost": {"amount": "300", "currency": "USD"},
    }
    assert "budget" not in bodies[1]
