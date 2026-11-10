"""Provider turns nobody watches: what may run, and the origin the MCP gate reads."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from daimon.core.stores.turn_origins import get_active_origin
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.unattended import (
    require_native_unattended_capabilities,
    unattended_provider_origin,
)
from daimon.core.turn.unattended_capabilities import UnattendedEntryPoint, capability_policy
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import ModelRef
from mux.contracts.resources import AgentSpec, MCPConnection, ToolSpec
from mux.profiles import PROFILES

if TYPE_CHECKING:
    from daimon.core.turn.admission import Admission
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PROVIDER_PROFILES = (
    "openai.persistent_workspace",
    "openai.conversation_only",
    "gemini.inline_reuse",
)
ENTRY_POINTS: tuple[UnattendedEntryPoint, ...] = ("routine", "wake", "watch", "long_job")


def _spec(*tools: ToolSpec, options: dict[str, str] | None = None) -> AgentSpec:
    return AgentSpec(
        name="worker",
        model=ModelRef(provider="openai", id="gpt-6-astra", options=options or {}),
        tools=tools,
        mcp_servers=(MCPConnection(name="daimon", url="https://example.invalid/mcp"),),
    )


def _check(spec: AgentSpec, profile: str, entry: UnattendedEntryPoint, *, gated: bool) -> None:
    require_native_unattended_capabilities(
        spec, profile_id=profile, gated=gated, sandbox=capability_policy(entry).allow_builtins
    )


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("profile", PROVIDER_PROFILES)
def test_gated_mcp_runs_unattended_and_hosted_shell_needs_the_sandbox_opt_in(
    profile: str, entry: UnattendedEntryPoint
) -> None:
    hosted_shell = PROFILES[profile].implicit_execution
    opted_in = capability_policy(entry).allow_builtins
    spec = _spec(ToolSpec(name="web_search", kind="builtin", permission="auto"))
    if hosted_shell and not opted_in:
        with pytest.raises(AdmissionDenied, match="Automatic sandbox"):
            _check(spec, profile, entry, gated=True)
    else:
        _check(spec, profile, entry, gated=True)
    with pytest.raises(AdmissionDenied):
        _check(spec, profile, entry, gated=False)
    bash = _spec(ToolSpec(name="bash", kind="builtin", permission="auto"))
    if opted_in:
        _check(bash, profile, entry, gated=True)
    else:
        with pytest.raises(AdmissionDenied, match="Automatic sandbox"):
            _check(bash, profile, entry, gated=True)


def test_watches_never_get_the_sandbox() -> None:
    assert not capability_policy("watch").allow_builtins, "a watch wake must not run a shell"
    assert not capability_policy("wake").allow_builtins
    assert capability_policy("routine", routine_sandbox_tools=True).allow_builtins


def test_custom_auto_tools_stay_refused_with_the_gate_on() -> None:
    spec = _spec(ToolSpec(name="deploy", kind="custom", permission="auto"))
    with pytest.raises(AdmissionDenied, match="Automatic sandbox"):
        _check(spec, "gemini.inline_reuse", "long_job", gated=True)


def test_model_options_pass_only_when_the_profile_declares_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = "openai.conversation_only"
    spec = _spec(options={"effort": "high"})
    with pytest.raises(AdmissionDenied, match="cannot inspect"):
        _check(spec, profile, "routine", gated=True)
    declared = PROFILES[profile].model_copy(update={"model_controls": ("effort",)})
    monkeypatch.setitem(cast("dict[str, object]", PROFILES), profile, declared)
    _check(spec, profile, "routine", gated=True)
    with pytest.raises(AdmissionDenied, match="cannot inspect"):
        _check(_spec(options={"effort": "high", "other": "x"}), profile, "routine", gated=True)


async def test_unattended_provider_origin_is_unattended_and_removed_after(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    admission = cast(
        "Admission",
        SimpleNamespace(
            account_id=account.id,
            origin_channel_id="parent",
            budget_channel_id=None,
            agent=SimpleNamespace(id="agent", name="Agent"),
            config=SimpleNamespace(agent_name=None),
            hold=None,
            shared_channel=False,
        ),
    )
    async with unattended_provider_origin(
        db_session_factory, admission, tenant_id=tenant.id, platform="discord", thread_id="t"
    ) as origin:
        assert not origin.attended, "the MCP gate must read this turn as unattended"
        assert origin.responder_ma_agent_id == "agent"
        assert origin.parent_channel_id == "parent"
    async with db_session_factory() as session:
        gone = await get_active_origin(
            session,
            origin_id=origin.id,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="discord",
            now=origin.created_at,
        )
    assert gone is None, "the origin must not outlive the turn"
    assert uuid.UUID(str(origin.id))
