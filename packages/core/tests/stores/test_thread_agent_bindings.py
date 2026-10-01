"""Setup routing remains shared while defaults and caller sessions stay independent."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from daimon.core._models import ThreadAgentBinding
from daimon.core.continuity.handoff import HandoffRefusedInSetupThread
from daimon.core.errors import DaimonError
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.stores.accounts import delete_account
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant, resolve
from daimon.core.stores.thread_agent_bindings import (
    create_binding,
    get_binding,
    list_active_bindings,
    list_active_setup_bindings_for_tenant,
    update_channel_lifecycle,
    update_lifecycle,
    upsert_responder_binding,
)
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


async def test_thread_wins_without_changing_environment_or_reachability(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    caller = await make_account(db_session, tenant=tenant)
    binding = await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="setup",
        responder_ma_agent_id="agent_daimon",
        responder_name="daimon",
        configuration_target_ma_agent_id="agent_specialist",
        configuration_target_name="specialist",
        creator_account_id=caller.id,
    )
    default = DeploymentDefault(agent_name="specialist", environment_name="science")
    context = ScopeContext(
        tenant_id=tenant.id,
        platform="discord",
        channel_id="channel",
        thread_id="setup",
        account_id=caller.id,
    )
    configured = await resolve(db_session, context=context, default=default)
    assert configured.responder_ma_agent_id == "agent_daimon", "thread selects concrete Daimon"
    assert configured.agent_name_tier == "thread", "thread should outrank the parent"
    assert configured.configuration_target_ma_agent_id == "agent_specialist", (
        "target stays distinct"
    )
    assert configured.environment_name == "science", "setup must retain the environment cascade"
    assert configured.environment_name_tier == "deployment", "environment has no thread tier"
    parent = await resolve(
        db_session, context=context.model_copy(update={"thread_id": None}), default=default
    )
    assert parent.agent_name == "specialist", "opening setup must not change the parent responder"
    assert not await is_agent_reachable_in_tenant(
        db_session, tenant_id=tenant.id, agent_name="daimon", default=default
    ), "thread bindings must not affect mutation reachability"
    await delete_account(db_session, account_id=caller.id)
    retained = await get_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="setup",
    )
    assert retained is not None and retained.id == binding.id, "creator erasure retains shared work"
    assert retained.creator_account_id is None, "erasure removes attribution"


async def test_lifecycle_retains_identity_and_omits_closed_conversations(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    for index in range(12):
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="slack",
            parent_channel_id="channel",
            thread_id=str(index),
            responder_ma_agent_id="agent_daimon",
            responder_name="daimon",
            configuration_target_ma_agent_id="deleted_target",
            configuration_target_name="specialist",
        )
    assert (
        len(
            await list_active_bindings(
                db_session, tenant_id=tenant.id, platform="slack", parent_channel_id="channel"
            )
        )
        == 10
    ), "recent list must stay bounded"
    assert not await list_active_bindings(
        db_session, tenant_id=tenant.id, platform="slack", parent_channel_id="other"
    ), "list cannot cross parent channels"
    await update_channel_lifecycle(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="channel",
        archived=True,
    )
    assert not await list_active_bindings(
        db_session, tenant_id=tenant.id, platform="slack", parent_channel_id="channel"
    ), "channel archive covers more than the visible ten"
    context = ScopeContext(
        tenant_id=tenant.id, platform="slack", channel_id="channel", thread_id="0"
    )
    archived = await resolve(db_session, context=context, default=DeploymentDefault())
    assert archived.configuration_target_ma_agent_id == "deleted_target", (
        "archiving retains exact target"
    )
    await update_channel_lifecycle(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="channel",
        archived=False,
    )
    await update_lifecycle(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="channel",
        thread_id="0",
        deleted=True,
    )
    await update_lifecycle(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="channel",
        thread_id="0",
        deleted=False,
    )
    with pytest.raises(DaimonError, match="deleted"):
        await resolve(db_session, context=context, default=DeploymentDefault(agent_name="other"))
    assert (
        len(
            await list_active_bindings(
                db_session, tenant_id=tenant.id, platform="slack", parent_channel_id="channel"
            )
        )
        == 10
    ), "reopened remaining conversations return"


async def test_binding_location_is_unique_and_tenant_scoped(db_session: AsyncSession) -> None:
    first = await make_tenant(db_session)
    second = await make_tenant(db_session)
    for tenant in (first, second):
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="setup",
            responder_ma_agent_id="agent_daimon",
            responder_name="daimon",
        )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await create_binding(
                db_session,
                tenant_id=first.id,
                platform="discord",
                parent_channel_id="channel",
                thread_id="setup",
                responder_ma_agent_id="agent_other",
                responder_name="other",
            )


async def test_a_handoff_binding_is_accepted_alongside_setup_bindings(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    binding = await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
        responder_ma_agent_id="agent_stats",
        responder_name="stats-bot",
        kind="handoff",
    )

    assert binding.kind == "handoff", (
        "an ordinary thread may record that its task was handed to another agent"
    )
    fetched = await get_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
    )
    assert fetched is not None and fetched.responder_ma_agent_id == "agent_stats", (
        "the handoff binding is what later turns read to find the responder"
    )


async def test_an_unknown_binding_kind_is_refused_by_the_database(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    db_session.add(
        ThreadAgentBinding(
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="bogus-thread",
            kind="bogus",
            responder_ma_agent_id="agent_stats",
            responder_name="stats-bot",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_listing_setups_never_surfaces_a_handoff_binding(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="setup-thread",
        responder_ma_agent_id="agent_daimon",
        responder_name="daimon",
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="handoff-thread",
        responder_ma_agent_id="agent_stats",
        responder_name="stats-bot",
        kind="handoff",
    )

    listed = await list_active_bindings(
        db_session, tenant_id=tenant.id, platform="discord", parent_channel_id="channel"
    )

    assert [row.thread_id for row in listed] == ["setup-thread"], (
        "a handed-over ordinary thread is not a setup conversation and must not be listed as one"
    )


async def test_upsert_responder_binding_creates_a_handoff_row_in_an_unbound_thread(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    row = await upsert_responder_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
        responder_ma_agent_id="agent_stats",
        responder_name="stats-bot",
        created_by_account_id=None,
        now=_NOW,
    )

    assert row.kind == "handoff", "a task handed over in an ordinary thread binds as a handoff"
    assert row.responder_ma_agent_id == "agent_stats", "the destination answers from now on"
    assert row.configuration_target_ma_agent_id is None, (
        "a handoff thread configures nothing, so it carries no target"
    )


async def test_upsert_responder_binding_moves_a_task_on_and_clears_the_stale_target(
    db_session: AsyncSession,
) -> None:
    """A task may be handed on again; the row follows it rather than stacking."""
    tenant = await make_tenant(db_session)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
        responder_ma_agent_id="agent_stats",
        responder_name="stats-bot",
        configuration_target_ma_agent_id="agent_stale",
        configuration_target_name="stale",
        kind="handoff",
    )

    row = await upsert_responder_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
        responder_ma_agent_id="agent_research",
        responder_name="research-bot",
        created_by_account_id=None,
        now=_NOW,
    )

    assert row.responder_name == "research-bot", "the newest destination answers"
    assert row.configuration_target_ma_agent_id is None, (
        "the previous thread's configuration target must not survive the move"
    )
    listed = await get_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
    )
    assert listed is not None and listed.id == row.id, "one location still holds exactly one row"


async def test_upsert_responder_binding_refuses_over_a_setup_conversation(
    db_session: AsyncSession,
) -> None:
    """Setup conversations answer as Daimon; overwriting the responder would break
    the admission path that asserts exactly that."""
    tenant = await make_tenant(db_session)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="setup-thread",
        responder_ma_agent_id="agent_daimon",
        responder_name="daimon",
    )

    with pytest.raises(HandoffRefusedInSetupThread):
        await upsert_responder_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="setup-thread",
            responder_ma_agent_id="agent_stats",
            responder_name="stats-bot",
            created_by_account_id=None,
            now=_NOW,
        )

    unchanged = await get_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="setup-thread",
    )
    assert unchanged is not None and unchanged.responder_ma_agent_id == "agent_daimon", (
        "a refused handoff leaves the setup conversation exactly as it was"
    )


async def test_upsert_responder_binding_is_scoped_to_one_tenant_and_location(
    db_session: AsyncSession,
) -> None:
    first = await make_tenant(db_session)
    second = await make_tenant(db_session)
    await create_binding(
        db_session,
        tenant_id=second.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
        responder_ma_agent_id="agent_other",
        responder_name="other-bot",
        kind="handoff",
    )

    await upsert_responder_binding(
        db_session,
        tenant_id=first.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
        responder_ma_agent_id="agent_stats",
        responder_name="stats-bot",
        created_by_account_id=None,
        now=_NOW,
    )

    neighbour = await get_binding(
        db_session,
        tenant_id=second.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ordinary-thread",
    )
    assert neighbour is not None and neighbour.responder_ma_agent_id == "agent_other", (
        "another workspace's identically-named thread is untouched"
    )


async def test_tenant_wide_listing_omits_handoffs_and_closed_conversations(
    db_session: AsyncSession,
) -> None:
    """Only live setup conversations count, across every channel in the install."""
    tenant = await make_tenant(db_session)
    for thread_id, kind in (
        ("live-a", "setup"),
        ("live-b", "setup"),
        ("handed-over", "handoff"),
        ("archived", "setup"),
        ("locked", "setup"),
        ("deleted", "setup"),
    ):
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=f"channel-{thread_id}",
            thread_id=thread_id,
            responder_ma_agent_id="agent_daimon",
            responder_name="daimon",
            kind=kind,
        )
    for thread_id, field in (
        ("archived", "archived"),
        ("locked", "locked"),
        ("deleted", "deleted"),
    ):
        await update_lifecycle(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=f"channel-{thread_id}",
            thread_id=thread_id,
            **{field: True},
        )

    rows, truncated = await list_active_setup_bindings_for_tenant(
        db_session, tenant_id=tenant.id, platform="discord"
    )

    assert sorted(row.thread_id for row in rows) == ["live-a", "live-b"], (
        "handed-over threads and archived/locked/deleted conversations must all be omitted"
    )
    assert not truncated, "two rows under the default limit is not a truncated listing"


async def test_tenant_wide_listing_reports_truncation_past_the_limit(
    db_session: AsyncSession,
) -> None:
    """The caller learns more conversations exist without a second query."""
    tenant = await make_tenant(db_session)
    for index in range(4):
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel",
            thread_id=f"setup-{index}",
            responder_ma_agent_id="agent_daimon",
            responder_name="daimon",
        )

    rows, truncated = await list_active_setup_bindings_for_tenant(
        db_session, tenant_id=tenant.id, platform="discord", limit=3
    )

    assert len(rows) == 3, "the listing must stop at the requested limit, not the probe row"
    assert truncated, "a fourth live conversation must be reported as truncated"

    all_rows, all_truncated = await list_active_setup_bindings_for_tenant(
        db_session, tenant_id=tenant.id, platform="discord", limit=4
    )
    assert len(all_rows) == 4 and not all_truncated, (
        "a limit that covers every live conversation must not report truncation"
    )


async def test_tenant_wide_listing_isolates_one_tenant_and_one_platform(
    db_session: AsyncSession,
) -> None:
    """A tenant-wide read still stops at the install boundary."""
    tenant = await make_tenant(db_session, workspace_id="guild-1")
    other = await make_tenant(db_session, workspace_id="guild-2")
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="ours",
        responder_ma_agent_id="agent_daimon",
        responder_name="daimon",
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="slack",
        parent_channel_id="channel",
        thread_id="other-platform",
        responder_ma_agent_id="agent_daimon",
        responder_name="daimon",
    )
    await create_binding(
        db_session,
        tenant_id=other.id,
        platform="discord",
        parent_channel_id="channel",
        thread_id="other-tenant",
        responder_ma_agent_id="agent_daimon",
        responder_name="daimon",
    )

    rows, _truncated = await list_active_setup_bindings_for_tenant(
        db_session, tenant_id=tenant.id, platform="discord"
    )

    assert [row.thread_id for row in rows] == ["ours"], (
        "another tenant's and another platform's conversations must not leak into the listing"
    )
