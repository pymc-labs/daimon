"""`compute_approval`: skip drops the launch and extend cards; the rest keep asking."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.confirmation import (
    compute_card_skipped,
    confirmed_tools_for_tenant,
    watch_card_skipped,
)
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import FeatureActor, change_feature, change_refusal
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.tool_safety import (
    APPROVAL_SKIPPABLE_TOOLS,
    COMPUTE_APPROVAL_TOOLS,
    CONFIRMED_DAIMON_TOOLS,
    DAIMON_SERVER_NAME,
    WATCH_APPROVAL_TOOLS,
    WATCH_CONFIRMED_TOOLS,
    ResolvedAgentToolRule,
    ToolCall,
    ToolSafetyPolicy,
    ToolVerdict,
    decide_tool_call,
    has_confirmation_gate,
    session_tools_for_policy,
)
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.unattended_capabilities import protected_tools
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AGENT = "agent_compute"
STILL_ASK = WATCH_CONFIRMED_TOOLS - COMPUTE_APPROVAL_TOOLS


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


def _deps(sessions: async_sessionmaker[AsyncSession]) -> TurnDeps:
    return cast(
        TurnDeps,
        SimpleNamespace(
            backend_selection_enabled=False,
            microsoft_send=False,
            tool_safety=ToolSafetyPolicy(),
            public_url=None,
            sessionmaker=sessions,
        ),
    )


async def _admin(_: object) -> bool:
    return True


async def _not_admin(_: object) -> bool:
    return False


async def _set(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    tier: str,
    value: str,
    key: str = "compute_approval",
) -> None:
    actor = FeatureActor(tenant_id, None, "web", None, _admin)
    place = FeaturePlace(tenant_id, "discord", None, agent_id=AGENT)
    await change_feature(
        sessions, _settings(), actor, key=key, tier=tier,  # type: ignore[arg-type]
        value=value, place=place,
    )  # fmt: skip


def test_the_setting_defaults_to_ask_at_the_workspace_and_agent_tiers() -> None:
    spec = FEATURES["compute_approval"]
    assert spec.default == "ask", "the card stays unless an admin skips it"
    assert spec.tiers == ("workspace", "agent"), "no channel, thread or deployment value"
    assert list(spec.values)[-1] == "ask", "an unreadable setting applies the strict value"


async def test_ask_keeps_every_card(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    got = await confirmed_tools_for_tenant(_deps(db_session_factory), tenant.id, AGENT)
    assert got >= WATCH_CONFIRMED_TOOLS, "ask is unchanged: launch and extend ask"


@pytest.mark.parametrize("tier", ["workspace", "agent"])
async def test_skip_drops_only_launch_and_extend(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tier: str
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _set(db_session_factory, tenant.id, tier, "skip")
    got = await confirmed_tools_for_tenant(_deps(db_session_factory), tenant.id, AGENT)
    assert not got & COMPUTE_APPROVAL_TOOLS, "launch and extend run without a card"
    assert got >= STILL_ASK, "watches, key rotation and long jobs keep asking"
    assert not await watch_card_skipped(db_session_factory, tenant.id, AGENT), "two switches"


async def test_skip_for_one_agent_leaves_another_asking(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _set(db_session_factory, tenant.id, "agent", "skip")
    other = await confirmed_tools_for_tenant(_deps(db_session_factory), tenant.id, "agent_other")
    assert other >= WATCH_CONFIRMED_TOOLS, "the agent tier is per agent"
    assert await compute_card_skipped(db_session_factory, tenant.id, AGENT)
    assert not await compute_card_skipped(db_session_factory, tenant.id, None), "no agent: ask"


async def test_only_a_server_admin_may_set_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = FeatureActor(tenant.id, None, "web", None, _not_admin, owns_agent=_admin)
    place = FeaturePlace(tenant.id, "discord", None, agent_id=AGENT)
    refused = await change_refusal(
        db_session_factory, _settings(), actor, key="compute_approval", tier="agent",
        place=place, value="skip",
    )  # fmt: skip
    assert refused is not None, "an agent owner who is not a server admin cannot skip the card"


async def test_an_unreadable_setting_keeps_the_card() -> None:
    class Broken:
        def __call__(self) -> object:
            raise RuntimeError("down")

    assert not await compute_card_skipped(Broken(), uuid.uuid4(), AGENT)  # pyright: ignore[reportArgumentType]


_ON = ToolSafetyPolicy(enabled=True)
_DAIMON = frozenset({DAIMON_SERVER_NAME})


def _asks(tool: str, confirmed: frozenset[str]) -> tuple[str, bool]:
    """With tool safety on: the decider's outcome, and whether the session holds a card."""
    call = ToolCall(
        tool_use_id="tu", server_name=DAIMON_SERVER_NAME, tool_name=tool,
        input={CONFIRMED_DAIMON_TOOLS[tool] or "": "x"},
    )  # fmt: skip
    outcome = decide_tool_call(
        _ON, call, attended=True, trusted_servers=_DAIMON, confirmed_tools=confirmed
    ).outcome
    toolset = [{"type": "mcp_toolset", "mcp_server_name": DAIMON_SERVER_NAME}]
    gated = session_tools_for_policy(
        _ON, toolset, trusted_servers=_DAIMON, confirmed_tools=confirmed
    )
    return outcome, has_confirmation_gate(gated or toolset, tool_name=tool)


@pytest.mark.parametrize(
    ("key", "skipped"),
    [("compute_approval", COMPUTE_APPROVAL_TOOLS), ("watch_approval", WATCH_APPROVAL_TOOLS)],
)
async def test_skip_holds_with_tool_safety_on(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    key: str,
    skipped: frozenset[str],
) -> None:
    """Staging, 2026-11-25: with tool safety on, skip still showed the card, because the
    policy's own list asked on every confirmed tool. The turn's set now decides."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _set(db_session_factory, tenant.id, "agent", "skip", key=key)
    confirmed = await confirmed_tools_for_tenant(_deps(db_session_factory), tenant.id, AGENT)
    for tool in sorted(skipped):
        assert _asks(tool, confirmed) == ("allow", False), f"{tool} runs with no card"
    for tool in sorted(WATCH_CONFIRMED_TOOLS - skipped):
        assert _asks(tool, confirmed) == ("ask", True), f"{tool} keeps its card"


async def test_only_the_skippable_tools_ever_leave_a_turns_set(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The one list (`APPROVAL_SKIPPABLE_TOOLS`): a turn's set always holds the rest of the
    watch tools, which is how the policy knows the set was built from the settings."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    deps = _deps(db_session_factory)
    every = await confirmed_tools_for_tenant(deps, tenant.id, AGENT)
    for key in ("compute_approval", "watch_approval"):
        await _set(db_session_factory, tenant.id, "agent", "skip", key=key)
    both = await confirmed_tools_for_tenant(deps, tenant.id, AGENT)
    assert every - both == APPROVAL_SKIPPABLE_TOOLS
    assert both >= WATCH_CONFIRMED_TOOLS - APPROVAL_SKIPPABLE_TOOLS


def _verdict(
    policy: ToolSafetyPolicy,
    tool: str,
    *,
    owner: frozenset[str] | None,
    rule: ResolvedAgentToolRule | None = None,
) -> ToolVerdict:
    """The unattended verdict, as an agent-chat turn's worker decides it. `owner=None` calls
    the decider as a caller with no owner tools does."""
    call = ToolCall(
        tool_use_id="tu", server_name=DAIMON_SERVER_NAME, tool_name=tool,
        input={CONFIRMED_DAIMON_TOOLS[tool] or "": "x"},
    )  # fmt: skip
    extra = {} if owner is None else {"owner_tools": owner}
    return decide_tool_call(
        policy,
        call,
        attended=False,
        trusted_servers=_DAIMON,
        confirmed_tools=protected_tools(policy),
        agent_rule=rule,
        **extra,
    )


_DENIED = tuple(f"daimon-mcp/{t}" for t in COMPUTE_APPROVAL_TOOLS)
#: Each case: the operator's deny list, the agent's rule, and the verdict for launch and
#: extend from an owner the agent-chat rule counts as present.
_OWNER_CASES = {
    "operator_deny": (_DENIED, None, ("deny", "write", "denied_by_operator")),
    "agent_deny": ((), "deny", ("deny", "write", "agent_rule_deny")),
    "agent_ask": ((), "ask", ("deny", "write", "agent_rule_ask")),
    "no_rule": ((), None, ("allow", "write", "agent_chat_owner")),
}


@pytest.mark.parametrize("enabled", [True, False], ids=["policy_on", "policy_off"])
@pytest.mark.parametrize("case", sorted(_OWNER_CASES))
def test_the_owners_start_stays_a_write_that_every_deny_and_ask_decides(
    enabled: bool, case: str
) -> None:
    """With the policy on or off, launch and extend stay confirmed writes through every
    check; the owner exception only turns what is left into an allow, last."""
    denied, rule, expected = _OWNER_CASES[case]
    policy = ToolSafetyPolicy(enabled=enabled, agent_rules=True, denied=denied)
    held = None if rule is None else ResolvedAgentToolRule(rule=rule)  # type: ignore[arg-type]
    for tool in sorted(COMPUTE_APPROVAL_TOOLS):
        got = _verdict(policy, tool, owner=COMPUTE_APPROVAL_TOOLS, rule=held)
        assert (got.outcome, got.effect, got.reason) == expected, tool


@pytest.mark.parametrize("enabled", [True, False], ids=["policy_on", "policy_off"])
@pytest.mark.parametrize("case", sorted(_OWNER_CASES))
def test_with_no_owner_tools_every_verdict_is_as_before(enabled: bool, case: str) -> None:
    """Every confirmed tool, every case: an empty exception decides as a caller without it."""
    denied, rule, _ = _OWNER_CASES[case]
    policy = ToolSafetyPolicy(enabled=enabled, agent_rules=True, denied=denied)
    held = None if rule is None else ResolvedAgentToolRule(rule=rule)  # type: ignore[arg-type]
    for tool in sorted(STILL_ASK | COMPUTE_APPROVAL_TOOLS):
        assert _verdict(policy, tool, owner=frozenset(), rule=held) == _verdict(
            policy, tool, owner=None, rule=held
        ), tool
        assert _verdict(policy, tool, owner=None, rule=held).outcome == "deny", tool


@pytest.mark.parametrize("enabled", [True, False], ids=["policy_on", "policy_off"])
async def test_the_worker_reads_the_agents_rule_for_the_owners_start_with_the_policy_off(
    enabled: bool,
) -> None:
    """The unattended decider reads the agent's rule for launch and extend even with the
    policy off, so a stored deny refuses them there; other calls read it as before."""
    from daimon.core.turn.approvals import unattended_decider

    policy = ToolSafetyPolicy(enabled=enabled, agent_rules=True)
    asked: list[str] = []

    async def deny(call: ToolCall) -> ResolvedAgentToolRule:
        asked.append(call.tool_name)
        return ResolvedAgentToolRule(rule="deny")

    decide = unattended_decider(
        policy,
        trusted_servers=_DAIMON,
        confirmed_tools=protected_tools(policy),
        rules_for=deny,
        daimon_rules=True,
        owner_tools=COMPUTE_APPROVAL_TOOLS,
    )
    for tool in sorted(COMPUTE_APPROVAL_TOOLS):
        call = ToolCall(
            tool_use_id="tu", server_name=DAIMON_SERVER_NAME, tool_name=tool,
            input={CONFIRMED_DAIMON_TOOLS[tool] or "": "x"},
        )  # fmt: skip
        assert not (await decide(call)).allow, tool
    assert sorted(asked) == sorted(COMPUTE_APPROVAL_TOOLS)
    other = ToolCall(tool_use_id="tu", server_name=DAIMON_SERVER_NAME, tool_name="list_routines")
    asked.clear()
    await decide(other)
    assert asked == (["list_routines"] if enabled else []), "other calls read rules as before"
