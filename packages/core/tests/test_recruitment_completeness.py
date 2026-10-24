"""Independent reader families refuse reserved channels before content/row access."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest
from daimon.core.config import RecruitmentSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.privacy import TurnPrivacy


def policy():
    return RecruitmentSettings(tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}})


def channel():
    value = MagicMock(spec=discord.Thread)
    value.id, value.parent_id, value.guild.id = 20, 2, 101
    return value


def interaction():
    value = MagicMock(spec=discord.Interaction)
    value.channel, value.guild_id = channel(), 101
    value.client = SimpleNamespace(
        runtime=SimpleNamespace(settings=SimpleNamespace(recruitment=policy()))
    )
    value.response.is_done.return_value = False
    value.response.send_message = AsyncMock()
    value.followup.send = AsyncMock()
    return value


@pytest.mark.parametrize("parent", ["2", None])
async def test_universal_driver_and_headless_refuse_direct_discord_callers(parent):
    from daimon.core.headless_runner import run_turn as headless
    from daimon.core.turn.driver import run_turn

    privacy = TurnPrivacy(
        "discord",
        policy(),
        derive_tenant_uuid(platform="discord", workspace_id="101"),
        "20",
        parent,
    )
    provider = MagicMock(side_effect=AssertionError("provider"))
    with pytest.raises(AdmissionDenied):
        await run_turn(
            privacy=privacy,
            anthropic=provider,
            session_id="private",
            user_message="private",
            lifecycle=MagicMock(),
            cancel=MagicMock(),
            billing=MagicMock(),
        )
    with pytest.raises(AdmissionDenied):
        await headless(
            privacy=privacy,
            anthropic=provider,
            agent_id="a",
            environment_id="e",
            trigger_message="private",
            tool_safety=MagicMock(),
        )
    provider.assert_not_called()


async def test_mcp_routine_destination_refuses_before_permissions_or_creation(monkeypatch):
    from contextlib import asynccontextmanager

    from daimon.adapters.mcp.tools import routines
    from fastmcp.exceptions import ToolError

    @asynccontextmanager
    async def rest(token):
        yield SimpleNamespace(fetch_channel=AsyncMock(return_value=channel()))

    monkeypatch.setattr(routines, "rest_client", rest)
    monkeypatch.setattr(routines, "_require_bot_token", lambda _: "fake")
    monkeypatch.setattr(routines, "_require_guild_id", lambda _: "101")
    monkeypatch.setattr(routines, "_require_discord_identity", lambda _: "7")
    monkeypatch.setattr(routines, "_resolve_member", AsyncMock(return_value=(None, MagicMock())))
    with pytest.raises(ToolError, match="Source unavailable"):
        await routines._resolve_discord_destination(
            SimpleNamespace(settings=SimpleNamespace(recruitment=policy())),
            MagicMock(),
            kind="thread",
            destination_id="20",
        )


def test_turn_driver_requires_explicit_source_authority():
    import inspect

    from daimon.core.headless_runner import run_turn as headless
    from daimon.core.turn.driver import run_turn

    assert inspect.signature(run_turn).parameters["privacy"].default is inspect.Parameter.empty
    assert inspect.signature(headless).parameters["privacy"].default is inspect.Parameter.empty


async def test_routine_creation_refuses_reserved_source_before_agent_or_row(monkeypatch):
    from contextlib import asynccontextmanager

    from daimon.adapters.mcp.tools import routines
    from fastmcp.exceptions import ToolError

    @asynccontextmanager
    async def session():
        yield MagicMock()

    origin = SimpleNamespace(thread_id="20", parent_channel_id="2")
    monkeypatch.setattr(routines, "_load_policy_for_save", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(routines, "origin_budget_channel", AsyncMock(return_value="2"))
    monkeypatch.setattr(routines, "get_verified_origin", AsyncMock(return_value=origin))
    agent = AsyncMock(side_effect=AssertionError("agent/provider lookup"))
    monkeypatch.setattr(routines, "find_agent_by_daimon_tag", agent)
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            recruitment=policy(), mcp=SimpleNamespace(require_routine_destination=False)
        ),
        session_factory=session,
    )
    auth = SimpleNamespace(
        tenant_id=uuid4(),
        platform="discord",
        external_id="101",
        platform_user_id="7",
        agent_id=None,
        chat_agent_id=None,
        client=False,
        hold=None,
    )
    with pytest.raises(ToolError, match="Source unavailable"):
        await routines._create_routine_impl(
            runtime,
            auth,
            agent_name="a",
            cron_expr="* * * * *",
            timezone="UTC",
            trigger_message="synthetic candidate",
        )
    agent.assert_not_awaited()


def test_driver_source_policy_keeps_default_off_and_tenant_isolation():
    TurnPrivacy("discord", RecruitmentSettings()).check()
    TurnPrivacy(
        "discord", policy(), derive_tenant_uuid(platform="discord", workspace_id="202"), "2", None
    ).check()
    TurnPrivacy(
        "discord", policy(), derive_tenant_uuid(platform="discord", workspace_id="101"), "30", "3"
    ).check()
    with pytest.raises(AdmissionDenied):
        TurnPrivacy("discord", policy(), None, "20", None).check()
    with pytest.raises(AdmissionDenied):
        TurnPrivacy("discord").check()


def test_prepared_privacy_prefers_admitted_tenant_to_repeated_caller_id():
    admitted_tenant = derive_tenant_uuid(platform="discord", workspace_id="101")
    admission = SimpleNamespace(
        grant=SimpleNamespace(tenant_id=admitted_tenant),
        origin_thread_id="20",
        origin_channel_id="2",
    )
    privacy = TurnPrivacy.from_admission(
        policy(), admission, platform="discord", tenant_id=uuid4(), fallback_channel_id="3"
    )
    assert privacy.tenant_id == admitted_tenant
    assert privacy.channel_id == "20" and privacy.parent_id == "2"
    with pytest.raises(AdmissionDenied):
        privacy.check()
