"""The routine service: the checks every surface (chat tool, panels, web) shares."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from daimon.core.access_policy import OPEN_ACCESS_POLICY
from daimon.core.authz import Place, build_subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.routine_service import (
    NOT_FOUND,
    CallView,
    NewRoutine,
    Placement,
    RoutineCaller,
    RoutineChange,
    RoutineEnv,
    RoutineRefused,
    create_routine,
    delete_routine,
    destination_channel_of,
    get_routine,
    list_routines,
    run_now,
    set_enabled,
    update_routine,
)
from daimon.core.rule_views import RuleViewer
from daimon.core.stores.domain import RoutineDestinationKind
from daimon.core.stores.routines import claim_due_fireable
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from mux.testing.drivers.anthropic.ma_models import ma_model_config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def committing_sessionmaker(
    db_engine: AsyncEngine,
    db_clean: None,
) -> async_sessionmaker[AsyncSession]:
    """Sessions on their own connection, so a refused change rolls back only itself."""
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


def _client(tenant_id: uuid.UUID, *names: str) -> AsyncAnthropic:
    agents = [
        ma_agent(
            id=f"ag_{name}",
            name=name,
            model=ma_model_config("claude-sonnet-4-6", speed="standard"),
            metadata={MA_METADATA_KEY_TENANT: str(tenant_id), MA_METADATA_KEY_NAME: name},
        ).model_dump(mode="json")
        for name in names
    ]
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda _req, _m: list_response(agents))
    return build_fake_anthropic(router.dispatch)


def _caller(
    tenant_id: uuid.UUID,
    user: str | None = "u_owner",
    *,
    admin: bool = False,
    talking_to: str | None = "daimon",
    hold: TurnHold | None = None,
) -> RoutineCaller:
    return RoutineCaller(
        tenant_id=tenant_id,
        subject=build_subject(is_admin=admin, platform_user_id=user),
        platform="slack",
        is_admin=admin,
        hold=hold,
        own_agent_ids=frozenset(
            {derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=f"ag_{talking_to}")}
            if talking_to
            else set()
        ),
    )


async def _view() -> CallView:
    return CallView(viewer=RuleViewer(policy=OPEN_ACCESS_POLICY))


def _env(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, **overrides: object
) -> RoutineEnv:
    base = RoutineEnv(
        sessionmaker=sessionmaker,
        anthropic=_client(tenant_id, "daimon", "other"),
        load_view=_view,
    )
    return RoutineEnv(**{**base.__dict__, **overrides})  # pyright: ignore[reportArgumentType]


def _new(**overrides: object) -> NewRoutine:
    fields: dict[str, object] = {
        "agent_name": "daimon",
        "cron_expr": "0 9 * * *",
        "timezone": "UTC",
        "trigger_message": "report",
    }
    return NewRoutine(**{**fields, **overrides})  # pyright: ignore[reportArgumentType]


async def _tenant(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session, platform="slack")
    return tenant.id


def test_destination_channel_by_platform() -> None:
    assert destination_channel_of("slack", "thread", "C0123ABC:1700000000.000100") == "C0123ABC"
    assert destination_channel_of("teams", "thread", "19:abc;messageid=9") == "19:abc"
    assert destination_channel_of("discord", "thread", "555") is None
    assert destination_channel_of("discord", "channel", "555") == "555"
    assert destination_channel_of("slack", None, None) is None


async def test_create_stamps_owner_and_next_fire(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    row = await create_routine(_env(db_session_factory, tenant_id), _caller(tenant_id), _new())
    assert row.created_by_user_id == "u_owner"
    assert row.agent_id == "ag_daimon"
    assert row.next_fire_at is not None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"timezone": "Mars/Phobos"}, "unknown timezone"),
        ({"cron_expr": "not cron"}, "invalid cron"),
        ({"agent_name": "ghost"}, "no agent named"),
        ({"destination_kind": "channel"}, "together"),
        ({"destination_kind": "thread", "destination_id": "C0123ABC"}, "invalid destination_id"),
    ],
)
async def test_create_refuses_bad_input(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, object],
    message: str,
) -> None:
    tenant_id = await _tenant(db_session)
    with pytest.raises(RoutineRefused, match=message):
        await create_routine(
            _env(db_session_factory, tenant_id), _caller(tenant_id), _new(**overrides)
        )


async def test_create_needs_a_platform_user(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    with pytest.raises(RoutineRefused, match="platform user identity"):
        await create_routine(
            _env(db_session_factory, tenant_id), _caller(tenant_id, user=None), _new()
        )


async def test_held_credential_saves_no_routine(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    hold = TurnHold(platform="slack", channel_id="C0123ABC", client=True)
    with pytest.raises(RoutineRefused, match="held to the channel"):
        await create_routine(
            _env(db_session_factory, tenant_id), _caller(tenant_id, hold=hold), _new()
        )


async def test_member_may_not_schedule_another_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    env = _env(db_session_factory, tenant_id)
    with pytest.raises(RoutineRefused, match="only an admin can schedule it"):
        await create_routine(env, _caller(tenant_id), _new(agent_name="other"))
    row = await create_routine(
        env, _caller(tenant_id, "u_admin", admin=True), _new(agent_name="other")
    )
    assert row.agent_name == "other", "an admin may schedule any agent"


async def test_channel_agent_may_be_scheduled_into_its_channel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)

    async def resolve(kind: RoutineDestinationKind, destination_id: str) -> Placement:
        return Placement(channel_id=destination_id.partition(":")[0])

    async def answers_with(channel_id: str) -> str | None:
        return "other"

    env = _env(
        db_session_factory,
        tenant_id,
        resolve_destination=resolve,
        channel_agent_name=answers_with,
    )
    row = await create_routine(
        env,
        _caller(tenant_id),
        _new(agent_name="other", destination_kind="channel", destination_id="C0123ABC"),
    )
    assert row.channel_id == "C0123ABC"
    assert (row.destination_kind, row.destination_id) == ("channel", "C0123ABC")


async def test_destination_needs_a_resolver(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    with pytest.raises(RoutineRefused, match="can't be set here"):
        await create_routine(
            _env(db_session_factory, tenant_id),
            _caller(tenant_id),
            _new(destination_kind="channel", destination_id="C0123ABC"),
        )


async def test_thread_spend_counts_against_its_channel(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)

    async def resolve(kind: RoutineDestinationKind, destination_id: str) -> Placement:
        return Placement(channel_id=destination_id, parent_channel_id="C0123ABC")

    row = await create_routine(
        _env(db_session_factory, tenant_id, resolve_destination=resolve),
        _caller(tenant_id),
        _new(destination_kind="thread", destination_id="C0123ABC:1700000000.000100"),
    )
    assert row.channel_id == "C0123ABC"


async def test_required_destination_and_origin_budget(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    with pytest.raises(RoutineRefused, match="No destination"):
        await create_routine(
            _env(db_session_factory, tenant_id, require_destination=True),
            _caller(tenant_id),
            _new(),
        )

    async def origin_budget() -> str | None:
        return "C0999ZZZ"

    row = await create_routine(
        _env(db_session_factory, tenant_id, origin_budget_channel=origin_budget),
        _caller(tenant_id),
        _new(),
    )
    assert row.channel_id == "C0999ZZZ"


async def test_only_the_owner_or_an_admin_changes_a_routine(
    db_session: AsyncSession, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await db_session.commit()
    env = _env(committing_sessionmaker, tenant_id)
    row = await create_routine(env, _caller(tenant_id), _new())
    other = _caller(tenant_id, "u_other")
    admin = _caller(tenant_id, "u_admin", admin=True)

    for call in (
        get_routine(env, other, row.id),
        update_routine(env, other, row.id, RoutineChange(trigger_message="x")),
        set_enabled(env, other, row.id, enabled=False),
        delete_routine(env, other, row.id),
    ):
        with pytest.raises(RoutineRefused) as refused:
            await call
        assert str(refused.value) == NOT_FOUND, "a non-owner learns nothing about the routine"

    assert await list_routines(env, other) == []
    assert [r.id for r in await list_routines(env, admin)] == [row.id]
    changed = await update_routine(env, admin, row.id, RoutineChange(trigger_message="new"))
    assert changed.trigger_message == "new"


async def test_pause_resume_and_delete(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    env = _env(db_session_factory, tenant_id)
    owner = _caller(tenant_id)
    row = await create_routine(env, owner, _new())
    paused = await set_enabled(env, owner, row.id, enabled=False)
    assert (paused.enabled, paused.next_fire_at) == (False, None)
    resumed = await set_enabled(env, owner, row.id, enabled=True)
    assert resumed.enabled is True
    assert resumed.next_fire_at is not None
    await delete_routine(env, owner, row.id)
    with pytest.raises(RoutineRefused, match=NOT_FOUND):
        await get_routine(env, owner, row.id)


async def test_run_now_makes_the_routine_due_and_the_schedule_carries_on(
    db_session: AsyncSession, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await db_session.commit()
    now = datetime(2030, 5, 6, 10, 30, tzinfo=UTC)
    seen: list[str] = []

    async def on_change(_session: AsyncSession, happened: str, _row: object) -> None:
        seen.append(happened)

    env = _env(committing_sessionmaker, tenant_id, now=lambda: now, on_change=on_change)
    owner = _caller(tenant_id)
    row = await create_routine(env, owner, _new())
    assert row.next_fire_at is not None and row.next_fire_at > now

    due = await run_now(env, owner, row.id)

    assert due.next_fire_at == now, "the scheduler picks routines by their next-run time"
    assert seen == ["created", "run_now"]
    async with committing_sessionmaker() as session, session.begin():
        [claimed] = await claim_due_fireable(session, now=now + timedelta(seconds=30))
        assert claimed.id == row.id
    async with committing_sessionmaker() as session:
        after = await get_routine(env, owner, row.id)
    assert after.next_fire_at == datetime(2030, 5, 7, 9, 0, tzinfo=UTC), (
        "after the run the routine's own schedule is next"
    )


async def test_only_the_owner_or_an_admin_runs_a_routine_now(
    db_session: AsyncSession, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    await db_session.commit()
    env = _env(committing_sessionmaker, tenant_id)
    row = await create_routine(env, _caller(tenant_id), _new())

    with pytest.raises(RoutineRefused) as refused:
        await run_now(env, _caller(tenant_id, "u_other"), row.id)
    assert str(refused.value) == NOT_FOUND
    with pytest.raises(RoutineRefused) as ownerless:
        await run_now(env, _caller(tenant_id, None), row.id)
    assert str(ownerless.value) == NOT_FOUND

    assert (await run_now(env, _caller(tenant_id, "u_admin", admin=True), row.id)).id == row.id


async def test_a_paused_routine_is_not_run_now(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)
    env = _env(db_session_factory, tenant_id)
    owner = _caller(tenant_id)
    row = await create_routine(env, owner, _new())
    await set_enabled(env, owner, row.id, enabled=False)

    with pytest.raises(RoutineRefused, match="paused"):
        await run_now(env, owner, row.id)
    assert (await get_routine(env, owner, row.id)).next_fire_at is None


async def test_update_recomputes_next_fire_and_clears_destination(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _tenant(db_session)

    async def resolve(kind: RoutineDestinationKind, destination_id: str) -> Placement:
        return Placement(channel_id=destination_id)

    env = _env(db_session_factory, tenant_id, resolve_destination=resolve)
    owner = _caller(tenant_id)
    row = await create_routine(
        env, owner, _new(destination_kind="channel", destination_id="C0123ABC")
    )
    changed = await update_routine(
        env, owner, row.id, RoutineChange(cron_expr="30 4 * * *", clear_destination=True)
    )
    assert changed.next_fire_at != row.next_fire_at
    assert changed.destination_kind is None
    assert changed.channel_id == "C0123ABC", "spend keeps counting against the same channel"
    with pytest.raises(RoutineRefused, match="cannot be combined"):
        await update_routine(
            env,
            owner,
            row.id,
            RoutineChange(
                clear_destination=True, destination_kind="channel", destination_id="C0456DEF"
            ),
        )


def test_placement_budget_channel() -> None:
    assert Placement(channel_id="T1", parent_channel_id="C0123ABC").budget_channel_id == "C0123ABC"
    assert Placement(channel_id="C0123ABC").budget_channel_id == "C0123ABC"
    assert Place(channel_id="C0123ABC").channel_id == "C0123ABC"
