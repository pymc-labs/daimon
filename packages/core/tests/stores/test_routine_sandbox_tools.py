"""An admin's per-routine opt-in to built-in sandbox tools, and its change log."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from daimon.core.authz import Subject
from daimon.core.errors import StoreError
from daimon.core.stores.domain import RoutineRow, SandboxPin
from daimon.core.stores.routines import (
    clear_sandbox_drift,
    create_routine,
    get_routine,
    pause_routine,
    record_result,
    sandbox_tools_history,
    set_sandbox_tools,
    update_routine,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

ADMIN = Subject(is_admin=True, platform_user_id="admin-1")
PIN = SandboxPin(
    agent_id="agent_a",
    agent_version=3,
    environment_id="env_1",
    environment_updated_at=datetime(2026, 10, 1, tzinfo=UTC),
)


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
    return (await make_tenant(db_session)).id


async def _routine(
    session: AsyncSession, tenant_id: uuid.UUID, owner: str = "owner-1"
) -> RoutineRow:
    return await create_routine(
        session,
        tenant_id=tenant_id,
        created_by_user_id=owner,
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="nightly",
    )


async def test_a_new_routine_has_sandbox_tools_off(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)
    assert row.sandbox_tools_enabled is False
    assert row.sandbox_tools_set_by is None
    assert row.sandbox_tools_set_at is None


async def test_admin_opt_in_is_recorded_with_who_when_and_routine(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)

    result = await set_sandbox_tools(
        db_session,
        row.id,
        tenant_id=tenant_id,
        enabled=True,
        pin=PIN,
        set_by="account-admin",
        actor=ADMIN,
        via="mcp",
    )

    assert result is not None
    updated, changed = result
    assert changed is True
    assert updated.sandbox_tools_enabled is True
    assert updated.sandbox_tools_set_by == "account-admin"
    assert updated.sandbox_tools_set_at is not None
    [entry] = await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)
    assert (entry.routine_id, entry.before_enabled, entry.after_enabled) == (row.id, False, True)
    assert (entry.set_by, entry.via) == ("account-admin", "mcp")
    assert entry.set_at == updated.sandbox_tools_set_at


@pytest.mark.parametrize(
    "actor",
    [
        Subject(is_admin=False, platform_user_id="owner-1"),
        Subject(is_admin=True, platform_user_id="owner-1", via_agent_key=True),
    ],
    ids=["owner_not_admin", "admin_agent_key"],
)
async def test_only_a_human_admin_may_opt_in(
    db_session: AsyncSession, tenant_id: uuid.UUID, actor: Subject
) -> None:
    """Owning the routine is not enough, and an admin's agent key is not a person."""
    row = await _routine(db_session, tenant_id, owner="owner-1")

    with pytest.raises(PermissionError):
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=tenant_id,
            enabled=True,
            pin=PIN,
            set_by="owner-account",
            actor=actor,
            via="mcp",
        )

    after = await get_routine(db_session, row.id, tenant_id=tenant_id)
    assert after is not None and after.sandbox_tools_enabled is False
    assert await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id) == []


async def test_setting_the_same_value_again_writes_nothing(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)
    for _ in range(2):
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=tenant_id,
            enabled=True,
            pin=PIN,
            set_by="a",
            actor=ADMIN,
            via="cli",
        )
    result = await set_sandbox_tools(
        db_session,
        row.id,
        tenant_id=tenant_id,
        enabled=True,
        pin=PIN,
        set_by="b",
        actor=ADMIN,
        via="cli",
    )

    assert result is not None and result[1] is False
    assert result[0].sandbox_tools_set_by == "a"
    assert len(await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)) == 1


async def test_opt_in_changes_only_that_routine(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    chosen = await _routine(db_session, tenant_id)
    other = await _routine(db_session, tenant_id)

    await set_sandbox_tools(
        db_session,
        chosen.id,
        tenant_id=tenant_id,
        enabled=True,
        pin=PIN,
        set_by="a",
        actor=ADMIN,
        via="mcp",
    )

    after_other = await get_routine(db_session, other.id, tenant_id=tenant_id)
    assert after_other is not None and after_other.sandbox_tools_enabled is False


async def test_another_tenant_cannot_reach_the_routine(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)
    stranger = (await make_tenant(db_session)).id

    assert (
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=stranger,
            enabled=True,
            pin=PIN,
            set_by="a",
            actor=ADMIN,
            via="mcp",
        )
        is None
    )


async def _opted_in(session: AsyncSession, tenant_id: uuid.UUID) -> RoutineRow:
    row = await _routine(session, tenant_id)
    result = await set_sandbox_tools(
        session,
        row.id,
        tenant_id=tenant_id,
        enabled=True,
        pin=PIN,
        set_by="a",
        actor=ADMIN,
        via="mcp",
    )
    assert result is not None
    return result[0]


@pytest.mark.parametrize(
    "edit",
    [
        {"trigger_message": "something else"},
        {"agent_id": "agent_b", "agent_name": "other"},
        {"destination_kind": "channel", "destination_id": "C123", "channel_id": "C123"},
    ],
    ids=["prompt", "agent", "destination"],
)
async def test_an_owner_edit_to_what_was_approved_clears_the_opt_in(
    db_session: AsyncSession, tenant_id: uuid.UUID, edit: dict[str, str]
) -> None:
    """The owner cannot carry an admin's opt-in over to a new agent, prompt or place."""
    row = await _opted_in(db_session, tenant_id)

    updated = await update_routine(
        db_session, row.id, tenant_id=tenant_id, edited_by="owner-account", **edit
    )

    assert updated is not None
    assert updated.sandbox_tools_enabled is False
    assert updated.sandbox_tools_set_by == "owner-account"
    history = await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)
    assert [(h.via, h.before_enabled, h.after_enabled, h.set_by) for h in history] == [
        ("mcp", False, True, "a"),
        ("edit", True, False, "owner-account"),
    ]


async def test_schedule_edits_pause_and_unchanged_values_keep_the_opt_in(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _opted_in(db_session, tenant_id)

    await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=tenant_id,
        cron_expr="0 10 * * *",
    )
    await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=tenant_id,
        trigger_message="nightly",
    )
    await pause_routine(db_session, row.id, tenant_id=tenant_id)

    after = await get_routine(db_session, row.id, tenant_id=tenant_id)
    assert after is not None and after.sandbox_tools_enabled is True
    assert len(await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)) == 1


async def _dm_ready(session: AsyncSession, tenant_id: uuid.UUID) -> RoutineRow:
    """A channel-less routine created with known provenance, so it may turn into a DM one."""
    return await create_routine(
        session,
        tenant_id=tenant_id,
        created_by_user_id="1001",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="nightly",
        source_provenance_known=True,
    )


async def test_a_dm_routine_cannot_be_opted_in(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _dm_ready(db_session, tenant_id)
    dm = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=tenant_id,
        destination_kind="dm",
    )
    assert dm is not None and dm.destination_kind == "dm"

    with pytest.raises(StoreError, match="DM routines can't use sandbox tools"):
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=tenant_id,
            enabled=True,
            pin=PIN,
            set_by="a",
            actor=ADMIN,
            via="cli",
        )

    after = await get_routine(db_session, row.id, tenant_id=tenant_id)
    assert after is not None and after.sandbox_tools_enabled is False
    assert await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id) == []


async def test_converting_to_dm_clears_the_opt_in(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _dm_ready(db_session, tenant_id)
    await set_sandbox_tools(
        db_session,
        row.id,
        tenant_id=tenant_id,
        enabled=True,
        pin=PIN,
        set_by="a",
        actor=ADMIN,
        via="mcp",
    )

    dm = await update_routine(
        db_session, row.id, tenant_id=tenant_id, destination_kind="dm", edited_by="1001"
    )

    assert dm is not None and dm.destination_kind == "dm"
    assert dm.sandbox_tools_enabled is False
    history = await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)
    assert history[-1].via == "edit" and history[-1].after_enabled is False


@pytest.mark.parametrize("enabled", [True, False], ids=["turned_on", "turned_off"])
async def test_a_flip_takes_effect_from_the_next_run_and_drops_the_in_flight_result(
    db_session: AsyncSession, tenant_id: uuid.UUID, enabled: bool
) -> None:
    """A run fires with the flag on its claimed row and keeps it to the end.

    Flipping the flag meanwhile bumps the routine's revision, so the run's
    fenced result write (`expected_row`) matches nothing: neither the result
    nor a delivery from the run that started under the old setting is kept.
    """
    claimed = await _routine(db_session, tenant_id)
    if not enabled:
        claimed = await _opted_in(db_session, tenant_id)

    await set_sandbox_tools(
        db_session,
        claimed.id,
        tenant_id=tenant_id,
        enabled=enabled,
        pin=PIN if enabled else None,
        set_by="a",
        actor=ADMIN,
        via="cli",
    )
    await record_result(
        db_session, claimed.id, tail="ran under the old setting", error=None, expected_row=claimed
    )

    after = await get_routine(db_session, claimed.id, tenant_id=tenant_id)
    assert after is not None
    assert after.sandbox_tools_enabled is enabled
    assert after.last_result_tail is None
    assert after.delivery_payload is None


async def test_purging_an_admin_erases_them_as_setter_but_keeps_the_log(
    db_session: AsyncSession, db_session_factory: Any
) -> None:
    """Privacy erasure: the change log keeps when, which routine and how; not who."""
    from daimon.core.purge import purge_account
    from daimon.testing.factories import make_account, make_platform_principal

    tenant = await make_tenant(db_session, workspace_id="g_purge")
    admin = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="2002", tenant=tenant, account=admin
    )
    by_mcp = await _routine(db_session, tenant.id)
    by_panel_edit = await _routine(db_session, tenant.id)
    kept = await _routine(db_session, tenant.id)
    for row, setter in ((by_mcp, str(admin.id)), (by_panel_edit, "x"), (kept, "other-admin")):
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=tenant.id,
            enabled=True,
            pin=PIN,
            set_by=setter,
            actor=ADMIN,
            via="mcp",
        )
    # The admin's panel edit is logged under their Discord user id.
    await update_routine(
        db_session, by_panel_edit.id, tenant_id=tenant.id, trigger_message="new", edited_by="2002"
    )
    await db_session.commit()

    await purge_account(sm=db_session_factory, account_id=admin.id)

    async with db_session_factory() as session:
        setters = {
            row.id: [
                (h.set_by, h.via)
                for h in await sandbox_tools_history(session, row.id, tenant_id=tenant.id)
            ]
            for row in (by_mcp, by_panel_edit, kept)
        }
        current = {
            row.id: (await get_routine(session, row.id, tenant_id=tenant.id)).sandbox_tools_set_by
            for row in (by_mcp, kept)
        }
        assert await get_routine(session, by_panel_edit.id, tenant_id=tenant.id) is None
        from daimon.core.stores.security_audit import list_events

        transfers = await list_events(session, tenant_id=tenant.id, tool_name="routines")
        assert len(transfers) == 1 and transfers[0].platform_user_id is None
    assert setters == {
        by_mcp.id: [("erased", "mcp")],
        by_panel_edit.id: [("x", "mcp"), ("erased", "edit")],
        kept.id: [("other-admin", "mcp")],
    }
    assert current == {by_mcp.id: "erased", kept.id: "other-admin"}


async def test_an_opt_in_records_the_agent_version_and_environment_it_covers(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _opted_in(db_session, tenant_id)

    assert (
        row.sandbox_agent_id,
        row.sandbox_agent_version,
        row.sandbox_environment_id,
        row.sandbox_environment_updated_at,
    ) == (PIN.agent_id, PIN.agent_version, PIN.environment_id, PIN.environment_updated_at)
    [entry] = await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)
    assert (entry.agent_id, entry.agent_version, entry.environment_id) == (
        PIN.agent_id,
        PIN.agent_version,
        PIN.environment_id,
    )
    assert entry.environment_updated_at == PIN.environment_updated_at


async def test_an_opt_in_without_a_readable_agent_and_environment_is_refused(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _routine(db_session, tenant_id)

    with pytest.raises(StoreError, match="Can't read the routine's agent version"):
        await set_sandbox_tools(
            db_session,
            row.id,
            tenant_id=tenant_id,
            enabled=True,
            set_by="a",
            actor=ADMIN,
            via="cli",
        )

    after = await get_routine(db_session, row.id, tenant_id=tenant_id)
    assert after is not None and after.sandbox_tools_enabled is False


async def test_re_approving_a_new_agent_version_records_it_and_off_forgets_it(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _opted_in(db_session, tenant_id)
    newer = replace(PIN, agent_version=4)

    result = await set_sandbox_tools(
        db_session,
        row.id,
        tenant_id=tenant_id,
        enabled=True,
        pin=newer,
        set_by="a",
        actor=ADMIN,
        via="cli",
    )
    assert result is not None and result[1] is True
    assert result[0].sandbox_agent_version == 4
    off = await set_sandbox_tools(
        db_session, row.id, tenant_id=tenant_id, enabled=False, set_by="a", actor=ADMIN, via="cli"
    )
    assert off is not None
    assert (off[0].sandbox_agent_id, off[0].sandbox_agent_version) == (None, None)


async def test_a_fire_that_found_drift_clears_only_the_opt_in_it_ran_under(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _opted_in(db_session, tenant_id)

    # A stale pin (an admin re-approved meanwhile) clears nothing.
    assert not await clear_sandbox_drift(
        db_session, row.id, tenant_id=tenant_id, pin=replace(PIN, agent_version=2)
    )
    assert await clear_sandbox_drift(db_session, row.id, tenant_id=tenant_id, pin=PIN)

    after = await get_routine(db_session, row.id, tenant_id=tenant_id)
    assert after is not None
    assert after.sandbox_tools_enabled is False
    assert after.sandbox_tools_set_by == "agent_changed"
    assert after.sandbox_agent_version is None
    # The run that noticed keeps its revision, so its result is still recorded.
    assert after.updated_at == row.updated_at
    history = await sandbox_tools_history(db_session, row.id, tenant_id=tenant_id)
    assert (history[-1].via, history[-1].set_by, history[-1].after_enabled) == (
        "edit",
        "agent_changed",
        False,
    )


async def test_an_edit_that_clears_the_opt_in_forgets_its_pin(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    row = await _opted_in(db_session, tenant_id)

    updated = await update_routine(
        db_session,
        row.id,
        edited_by=row.created_by_user_id,
        tenant_id=tenant_id,
        trigger_message="new",
    )

    assert updated is not None
    assert (updated.sandbox_agent_id, updated.sandbox_environment_id) == (None, None)
