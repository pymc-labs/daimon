"""Imported timers: erased with the person they belong to, never with someone else's."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from daimon.core.channel_backend import set_channel_backend
from daimon.core.continuity.watch_imports import ImportedTimer
from daimon.core.privacy import collect_purge_preview
from daimon.core.purge import purge_account, purge_principal
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.held_timers import (
    cancel_missing_timers,
    import_held_timer,
    list_unrunnable_timers,
    settle_held_timers,
)
from daimon.core.stores.task_continuations import record_continuation
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ChannelRef
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def timer(source_id: str, user: str) -> ImportedTimer:
    return ImportedTimer(
        source_id=source_id,
        owner_discord_id=user,
        channel_id="900",
        thread_id=None,
        place_type="direct-message",
        fire_at=datetime.now(UTC) + timedelta(hours=2),
        note="remember",
    )


async def hold(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantRow,
    user: str,
    *,
    agent_id: str = "agent-1",
    place: str = "direct-message",
) -> None:
    async with factory.begin() as session:
        definition = timer(f"t-{user}", user).model_copy(update={"place_type": place})
        result = await import_held_timer(
            session, definition, tenant_id=tenant.id, agent_id=agent_id, now=datetime.now(UTC)
        )
    assert result.outcome == "held", result


async def count_rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int((await session.execute(text("select count(*) from held_timers"))).scalar_one())


async def test_erasing_an_account_removes_its_held_timers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, workspace_id="700000102")
    account = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="999", tenant=tenant, account=account
    )
    await make_platform_principal(
        db_session, platform="discord", external_id="1000", tenant=tenant, account=other
    )
    await db_session.commit()
    await hold(db_session_factory, tenant, "999")
    await hold(db_session_factory, tenant, "1000")

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    assert preview.held_timers.count == 1, "the preview names the held timer"

    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert report.db.held_timers == 1
    assert await count_rows(db_session_factory) == 1, "the other person's timer stays"


async def test_erasing_a_principal_removes_only_its_held_timers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, workspace_id="700000102")
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="999", tenant=tenant
    )
    await db_session.commit()
    await hold(db_session_factory, tenant, "999")
    await hold(db_session_factory, tenant, "1000")

    report = await purge_principal(
        sm=db_session_factory, principal_id=principal.id, kind="platform"
    )

    assert report.db.held_timers == 1
    assert await count_rows(db_session_factory) == 1


async def outcomes(factory: async_sessionmaker[AsyncSession]) -> dict[str, str | None]:
    async with factory() as session:
        rows = await session.execute(text("select source_id, outcome from held_timers"))
        return {source_id: outcome for source_id, outcome in rows}


async def test_go_live_and_full_import_touch_only_their_own_agents_timers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, workspace_id="700000102")
    await db_session.commit()
    await hold(db_session_factory, tenant, "1", agent_id="agent-1")
    await hold(db_session_factory, tenant, "2", agent_id="agent-2")
    now = datetime.now(UTC)

    async with db_session_factory.begin() as session:
        cancelled = await cancel_missing_timers(
            session, tenant_id=tenant.id, agent_id="agent-1", keep=set(), now=now
        )
    assert cancelled == 1
    assert await outcomes(db_session_factory) == {"t-1": "cancelled", "t-2": None}

    counts = await settle_held_timers(
        db_session_factory, tenant_id=tenant.id, agent_id="agent-2", agent_name="b", now=now
    )
    assert counts == {"scheduled": 1}
    assert await outcomes(db_session_factory) == {"t-1": "cancelled", "t-2": "scheduled"}


async def test_dm_and_channel_timers_stay_held_for_an_agent_off_managed_agents(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, workspace_id="700000102")
    await db_session.commit()
    await hold(db_session_factory, tenant, "dm", place="direct-message")
    await hold(db_session_factory, tenant, "room", place="channel")
    await hold(db_session_factory, tenant, "thread", place="thread")

    async with db_session_factory() as session:
        named = await list_unrunnable_timers(
            session,
            tenant_id=tenant.id,
            agent_id="agent-1",
            agent_runs=False,
            channel_backends=True,
        )
    counts = await settle_held_timers(
        db_session_factory,
        tenant_id=tenant.id,
        agent_id="agent-1",
        agent_name="a",
        now=datetime.now(UTC),
        skip=named,
    )

    assert named == ["t-dm", "t-room"]
    assert counts == {"scheduled": 1}, "only the thread timer is scheduled"
    assert await outcomes(db_session_factory) == {
        "t-dm": None,
        "t-room": None,
        "t-thread": "scheduled",
    }


async def test_a_channels_own_backend_wins_over_the_agents_for_its_timers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, workspace_id="700000102")
    await db_session.commit()
    await hold(db_session_factory, tenant, "room", place="channel")  # channel "900"
    channel = ChannelRef(tenant_id=str(tenant.id), platform="discord", channel_id="900")

    async def unrunnable(*, agent_runs: bool, channel_backends: bool = True) -> list[str]:
        async with db_session_factory() as session:
            return await list_unrunnable_timers(
                session,
                tenant_id=tenant.id,
                agent_id="agent-1",
                agent_runs=agent_runs,
                channel_backends=channel_backends,
            )

    assert await unrunnable(agent_runs=False) == ["t-room"], "no channel setting: the agent's"
    async with db_session_factory.begin() as session:
        await set_channel_backend(session, channel, BackendConfig())  # Managed Agents
    assert await unrunnable(agent_runs=False) == [], "the channel runs it though the agent won't"
    assert await unrunnable(agent_runs=False, channel_backends=False) == ["t-room"]
    async with db_session_factory.begin() as session:
        await set_channel_backend(session, channel, BackendConfig(backend="openai", model="gpt-5"))
    assert await unrunnable(agent_runs=True) == ["t-room"], "the channel's other backend wins"


async def test_the_migration_settles_due_dm_timers_and_no_others(
    db_session: AsyncSession,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "migration_0211",
        Path(__file__).parents[1] / "alembic/versions/0211_callback_watch_imports.py",
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    tenant = await make_tenant(db_session, workspace_id="700000102")
    account = await make_account(db_session, tenant=tenant)
    now = datetime.now(UTC)
    for thread, due in (
        ("dm:old", now - timedelta(days=30)),
        ("dm:later", now + timedelta(days=1)),
        ("4242", now - timedelta(days=30)),
    ):
        await record_continuation(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id=thread,
            thread_id=thread,
            requester_account_id=account.id,
            requester_external_user_id="999",
            target_ma_agent_id="agent-1",
            target_name="a",
            reason="timer",
            idempotency_key=uuid.uuid4(),
            available_at=due,
        )

    await db_session.execute(text(migration.SETTLE_DUE_DM_TIMERS))

    rows = await db_session.execute(text("select thread_id, status from task_continuations"))
    assert {thread: status for thread, status in rows} == {
        "dm:old": "skipped",
        "dm:later": "pending",
        "4242": "pending",
    }
