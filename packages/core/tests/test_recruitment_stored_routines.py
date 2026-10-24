"""Saved routine content follows current settings, including after a restart."""

from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import httpx
import pytest
from daimon.core.config import RecruitmentSettings
from daimon.core.errors import UserFacingError
from daimon.core.stores.routines import create_routine, get_routine, record_result
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import build_stub_anthropic
from rich.console import Console


@pytest.mark.parametrize(
    "placement",
    [
        "source_parent",
        "source_thread",
        "destination_parent",
        "destination_thread",
        "legacy",
        "unknown_parent",
        "unknown_parent_is_thread",
    ],
)
async def test_stored_content_readers_follow_settings_restart(
    db_session_factory, placement, monkeypatch
):
    from daimon.adapters.cli.commands.routines import run_sandbox_tools
    from daimon.adapters.discord.routines_panel.panel import RoutinesPanelView
    from daimon.adapters.discord.routines_panel.read import load_guild_routines
    from daimon.adapters.discord.routines_panel.state import RoutinesPanelState
    from daimon.adapters.mcp.auth.resolver import AuthIdentity
    from daimon.adapters.mcp.tools import routines as mcp
    from daimon.core.authz import Subject
    from daimon.core.stores.domain import Role

    source_parent = "2" if placement.startswith("source") else "3"
    source_thread = "20" if placement == "source_thread" else None
    kind = (
        "thread"
        if placement in {"destination_thread", "unknown_parent", "unknown_parent_is_thread"}
        else "channel"
    )
    destination = "20" if kind == "thread" else "2" if placement == "destination_parent" else "3"
    budget = (
        None if placement == "unknown_parent" else "2" if placement == "destination_thread" else "3"
    )
    if placement == "unknown_parent_is_thread":
        budget = destination
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="101")
        row = await create_routine(
            session,
            tenant_id=tenant.id,
            created_by_user_id="7",
            agent_id="agent",
            agent_name="daimon",
            cron_expr="0 9 * * *",
            timezone_="UTC",
            trigger_message="synthetic candidate trigger",
            source_channel_id=source_parent,
            source_thread_id=source_thread,
            source_provenance_known=placement != "legacy",
            destination_kind=kind,
            destination_id=destination,
            channel_id=budget,
        )
        await record_result(session, row.id, tail="synthetic candidate result", error=None)
    auth = AuthIdentity(
        account_id=uuid4(),
        tenant_id=tenant.id,
        role=Role.USER,
        platform="discord",
        external_id="101",
        platform_user_id="7",
        is_admin=False,
    )
    client = build_stub_anthropic(
        lambda req: httpx.Response(200, json={"data": [], "has_more": False})
    )

    def runtime(settings):
        return SimpleNamespace(
            session_factory=db_session_factory,
            sessionmaker=db_session_factory,
            client=client,
            anthropic=client,
            settings=SimpleNamespace(recruitment=settings),
        )

    before = runtime(RecruitmentSettings())
    assert (
        await mcp._get_routine_impl(before, auth, routine_id=row.id)
    ).trigger_message == row.trigger_message
    async with db_session_factory() as session:
        previous_entries, _, _ = await load_guild_routines(
            session,
            client,
            tenant_id=tenant.id,
            viewer_user_id="7",
            recruitment=before.settings.recruitment,
        )
    assert len(previous_entries) == 1
    # Construct a new runtime; no origin is in R and no Discord metadata is fetched.
    after = runtime(
        RecruitmentSettings(tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}})
    )
    assert await mcp._list_routines_impl(after, auth) == []
    admin = replace(auth, is_admin=True, platform_user_id="8")
    assert await mcp._list_routines_impl(after, admin) == []
    with pytest.raises(ToolError, match="^routine not found$"):
        await mcp._get_routine_impl(after, admin, routine_id=row.id)
    with pytest.raises(ToolError, match="^routine not found$"):
        await mcp._get_routine_impl(after, auth, routine_id=row.id)
    with pytest.raises(ToolError, match="^routine not found$"):
        await mcp._update_routine_impl(after, auth, routine_id=row.id, enabled=False)
    with pytest.raises(ToolError, match="^routine not found$"):
        await mcp._delete_routine_impl(after, auth, routine_id=row.id)
    # Simulate an already-authorized administrator; the content gate still applies.
    monkeypatch.setattr(mcp, "_sandbox_admin", AsyncMock(return_value=Subject(is_admin=True)))
    with pytest.raises(ToolError, match="^routine not found$"):
        await mcp._set_routine_sandbox_tools_impl(after, auth, routine_id=row.id, enabled=False)
    async with db_session_factory() as session:
        entries, count, _ = await load_guild_routines(
            session,
            client,
            tenant_id=tenant.id,
            viewer_user_id="7",
            recruitment=after.settings.recruitment,
        )
        assert entries == [] and count == 0
        assert (await get_routine(session, row.id, tenant_id=tenant.id)).enabled
    # A saved output control must re-read through the same policy, rather than
    # serialize the old entry kept by its view.
    view = RoutinesPanelView(
        RoutinesPanelState.initial(rows=previous_entries, over_cap_count=0, agent_name_map={}),
        runtime=after,
        allowed_user_id=7,
    )
    button = next(
        item
        for item in view.walk_children()
        if getattr(item, "label", None) == "📜 View last output"
    )
    tap = MagicMock(spec=discord.Interaction)
    tap.user.id = 7
    tap.response.send_message = AsyncMock()
    await button.callback(tap)
    tap.response.send_message.assert_awaited_once_with("Routine not found.", ephemeral=True)
    output = StringIO()
    with pytest.raises(UserFacingError, match="No routine in this tenant"):
        await run_sandbox_tools(
            rt=after,
            console=Console(file=output),
            tenant_id=tenant.id,
            routine_ids=[row.id],
            enabled=False,
            dry_run=True,
        )
    assert "candidate" not in output.getvalue()
    await client.close()


@pytest.mark.parametrize(
    "placement", ["ordinary", "known_source_free", "other_tenant", "feature_off"]
)
def test_saved_routine_policy_preserves_ordinary_readers(placement):
    from daimon.core.ma_identity import derive_tenant_uuid
    from daimon.core.routines import recruitment_withholds_routine

    settings = RecruitmentSettings(
        tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}}
    )
    row = SimpleNamespace(
        tenant_id=derive_tenant_uuid(
            platform="discord", workspace_id="102" if placement == "other_tenant" else "101"
        ),
        source_provenance_known=True,
        source_channel_id=None
        if placement == "known_source_free"
        else "2"
        if placement == "other_tenant"
        else "3",
        source_thread_id=None,
        destination_kind=None,
        destination_id=None,
        channel_id=None,
    )
    assert not recruitment_withholds_routine(
        RecruitmentSettings() if placement == "feature_off" else settings, row
    )
