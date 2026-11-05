"""An admin sets any agent's helpers, seeded ones included, for their own workspace only."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core import agent_admin
from daimon.core.agent_editing import AgentActor, AgentEditError
from daimon.core.authz import Subject, Surface
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.stores import agent_helpers
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _roster(tenant_id: uuid.UUID) -> list[BetaManagedAgentsAgent]:
    seeded = {MA_METADATA_KEY_MANAGED: "true"}
    return [
        ma_agent(id="ag_daimon", name="daimon", tenant_id=tenant_id, metadata=seeded),
        ma_agent(id="ag_argon", name="argon", tenant_id=tenant_id),
        ma_agent(
            id="ag_old",
            name="old",
            tenant_id=tenant_id,
            archived_at=datetime(2030, 1, 1, tzinfo=UTC),
        ),
    ]


async def _actor(session: AsyncSession, *, admin: bool = True) -> AgentActor:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    await session.commit()
    return AgentActor(
        tenant_id=tenant.id,
        account_id=account.id,
        subject=Subject(is_admin=admin, platform_user_id=str(account.id)),
        surface=Surface.WEB,
    )


async def _list(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, agent_id: str
) -> list[str]:
    async with factory() as session:
        return await agent_helpers.list_helper_ids(session, tenant_id=tenant_id, agent_id=agent_id)


async def test_an_admin_sets_a_seeded_agents_helpers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    audited: list[str] = []

    async def record(_session: AsyncSession, name: str) -> None:
        audited.append(name)

    saved = await agent_admin.set_helpers(
        db_session_factory,
        actor=actor,
        ma_agent_id="ag_daimon",
        helper_ids=["ag_argon", "ag_argon"],
        roster=_roster(actor.tenant_id),
        audited=record,
    )

    assert saved == ["ag_argon"], "a repeated helper is kept once"
    assert await _list(db_session_factory, actor.tenant_id, "ag_daimon") == ["ag_argon"]
    assert audited == ["daimon"], "the change is audited with the agent's name"


async def test_saving_the_same_list_again_records_nothing(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    audited: list[str] = []

    async def record(_session: AsyncSession, name: str) -> None:
        audited.append(name)

    for _ in range(2):
        await agent_admin.set_helpers(
            db_session_factory,
            actor=actor,
            ma_agent_id="ag_daimon",
            helper_ids=["ag_argon"],
            roster=_roster(actor.tenant_id),
            audited=record,
        )

    assert audited == ["daimon"], "only the first save changed anything"


async def test_a_non_admin_is_refused(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session, admin=False)

    with pytest.raises(AgentEditError, match="needs an admin") as err:
        await agent_admin.set_helpers(
            db_session_factory,
            actor=actor,
            ma_agent_id="ag_daimon",
            helper_ids=["ag_argon"],
            roster=_roster(actor.tenant_id),
        )

    assert err.value.kind == "forbidden"
    assert await _list(db_session_factory, actor.tenant_id, "ag_daimon") == []


@pytest.mark.parametrize(
    ("agent", "helpers", "kind", "words"),
    [
        ("ag_daimon", ["ag_daimon"], "invalid", "own helper"),
        ("ag_daimon", ["ag_missing"], "invalid", "isn't available"),
        ("ag_daimon", ["ag_old"], "invalid", "isn't available"),
        ("ag_missing", ["ag_argon"], "not_found", "No such agent"),
        ("ag_old", ["ag_argon"], "not_found", "No such agent"),
    ],
)
async def test_an_unusable_agent_or_helper_is_refused_and_nothing_changes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    agent: str,
    helpers: list[str],
    kind: str,
    words: str,
) -> None:
    actor = await _actor(db_session)

    with pytest.raises(AgentEditError, match=words) as err:
        await agent_admin.set_helpers(
            db_session_factory,
            actor=actor,
            ma_agent_id=agent,
            helper_ids=helpers,
            roster=_roster(actor.tenant_id),
        )

    assert err.value.kind == kind
    assert await _list(db_session_factory, actor.tenant_id, agent) == []


async def test_a_helper_since_archived_can_still_be_taken_off(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    actor = await _actor(db_session)
    async with db_session_factory.begin() as session:
        await agent_helpers.set_helpers(
            session,
            tenant_id=actor.tenant_id,
            agent_id="ag_daimon",
            helper_ids=["ag_old", "ag_argon"],
        )

    saved = await agent_admin.set_helpers(
        db_session_factory,
        actor=actor,
        ma_agent_id="ag_daimon",
        helper_ids=["ag_old"],
        roster=_roster(actor.tenant_id),
    )

    assert saved == ["ag_old"], "a helper already on the list is not re-checked"


async def test_one_workspaces_edit_leaves_the_other_workspaces_list_alone(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    a = await _actor(db_session)
    b = await _actor(db_session)

    await agent_admin.set_helpers(
        db_session_factory,
        actor=a,
        ma_agent_id="ag_daimon",
        helper_ids=["ag_argon"],
        roster=_roster(a.tenant_id),
    )

    assert await _list(db_session_factory, a.tenant_id, "ag_daimon") == ["ag_argon"]
    assert await _list(db_session_factory, b.tenant_id, "ag_daimon") == [], (
        "the same agent id in another workspace keeps its own, empty, list"
    )
    with pytest.raises(AgentEditError, match="No such agent"):
        await agent_admin.set_helpers(
            db_session_factory,
            actor=a,
            ma_agent_id="ag_daimon",
            helper_ids=["ag_argon"],
            roster=_roster(b.tenant_id)[:0],
        )
