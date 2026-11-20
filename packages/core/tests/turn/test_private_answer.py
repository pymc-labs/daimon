"""The Private answer from a contained room (`audience_hold`, BUILD-PLAN C4, E3).

The offer shows in mode `on` only, to an internal asker, in a contained room that is
not narrow, unless the press would certainly be refused (DMs off, or an agent that never
DMs). A press is checked again. The private turn is a DM with no source: the
agent's budget channel is its spend channel, never a source whose seal refuses it, on
the first turn and on follow-ups. Ids are fictional. Mode `on` comes from the table in
`test_admission_audience`, because the registry has no `on` value yet.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from daimon.core import direct_messages
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import AgentRef
from daimon.core.direct_messages import admit_cold_dm, reply_to_dm_result, start_cold_dm
from daimon.core.private_answer import offer_private_answer, private_answer_refusal
from daimon.core.stores.direct_messages import (
    COLD_DM_ORIGIN,
    PRIVATE_ANSWER_ORIGIN,
    get_conversation,
    set_dm_enabled,
)
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.turn.admission import Admission, reauthorize
from daimon.core.turn.errors import DmSourceSealedError
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from .test_admission import _admittable_router, _deps, _seed_admittable_tenant
from .test_admission_audience import (  # noqa: F401  # _mode_table is this module's autouse fixture too
    _MODES,
    CONTAINED,
    NARROW,
    OPEN,
    ROOM,
    THREAD,
    USER,
    _facts,
    _mode,
    _mode_table,
)

INTERNAL = "9001"
OTHER = "9002"
DM_ROUTE = "7501"


@pytest.fixture(autouse=True)
def _mode_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """`read_mode` reads through this module's own import of the mode reader."""

    async def mode(_session: AsyncSession, place: object, **_kw: object) -> str:
        return _MODES.get(place.tenant_id, "off")  # type: ignore[attr-defined]

    monkeypatch.setattr("daimon.core.private_answer.audience_mode", mode)


def _policy(**fields: object) -> TenantAccessPolicy:
    return TenantAccessPolicy(internal_role_ids=(INTERNAL,), **fields)  # type: ignore[arg-type]


async def _offer_room(
    db_session: AsyncSession,
    facts: object,
    *,
    policy: TenantAccessPolicy | None = None,
    dms: bool = True,
) -> TenantRow:
    """A server with the room's and thread's facts, and DMs on unless `dms` is False."""
    tenant = await _seed_admittable_tenant(db_session, policy=policy or _policy())
    await _facts(db_session, tenant, {ROOM: facts, THREAD: facts})  # type: ignore[dict-item]
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=dms)
    await db_session.commit()
    return tenant


async def _offer(
    factory: async_sessionmaker[AsyncSession], tenant: TenantRow, mode: str, roles: tuple[str, ...]
) -> bool:
    async with factory() as session:
        return await offer_private_answer(
            session,
            tenant_id=tenant.id,
            user_id=USER,
            channel_id=ROOM,
            thread_id=THREAD,
            mode=mode,  # type: ignore[arg-type]
            agent="daimon",
            agent_ref=AgentRef.of("daimon"),
            event_role_ids=roles,
        )


@pytest.mark.parametrize(
    ("facts", "roles", "shown"),
    [
        (CONTAINED, (INTERNAL,), True),
        (CONTAINED, (OTHER,), False),  # an outsider gets no button
        (OPEN, (INTERNAL,), False),
        (NARROW, (INTERNAL,), False),  # narrow rooms are closed to private answers
    ],
    ids=["contained-internal", "contained-outsider", "open", "narrow"],
)
async def test_the_offer_shows_in_mode_on_to_an_internal_asker_in_a_contained_room(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    facts: object,
    roles: tuple[str, ...],
    shown: bool,
) -> None:
    """Spec check 5: the button is for an internal asker in a contained room only."""
    tenant = await _offer_room(db_session, facts)

    assert await _offer(db_session_factory, tenant, "on", roles) is shown


@pytest.mark.parametrize("mode", ["on", "shadow"])
@pytest.mark.parametrize(
    ("dms", "policy", "reason"),
    [
        (False, None, "dms_off"),
        (  # a channel's own agent never DMs (decision 1)
            True,
            _policy(
                agent_rules={"daimon": AgentRule(runs_in=(ROOM,))},
                channel_rules={ROOM: ChannelRule(readers="own", writers="own")},
            ),
            "agent_no_dms",
        ),
    ],
    ids=["dms-off", "own-agent"],
)
async def test_an_offer_the_press_would_refuse_is_skipped(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    mode: str,
    dms: bool,
    policy: TenantAccessPolicy | None,
    reason: str,
) -> None:
    """The press's static checks run at offer time; a skip logs its reason, no text."""
    tenant = await _offer_room(db_session, CONTAINED, policy=policy, dms=dms)

    with capture_logs() as logs:
        assert await _offer(db_session_factory, tenant, mode, (INTERNAL,)) is False

    skipped = [log for log in logs if log["event"] == "audience.private_offer_skipped"]
    assert skipped == [
        {
            "event": "audience.private_offer_skipped",
            "log_level": "info",
            "tenant_id": str(tenant.id),
            "channel_id": ROOM,
            "agent": "daimon",
            "reason": reason,
        }
    ]
    assert not [log for log in logs if log.get("kind") == "private_offer"]


async def test_shadow_logs_the_offer_and_off_reads_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await _offer_room(db_session, CONTAINED)
    with capture_logs() as logs:
        assert await _offer(db_session_factory, tenant, "shadow", (INTERNAL,)) is False
    offers = [log for log in logs if log.get("kind") == "private_offer"]
    assert [(r["event"], r["channel_id"], r["agent"], r["reason"]) for r in offers] == [
        ("audience.shadow", ROOM, "daimon", "room_contained")
    ]

    read = AsyncMock(side_effect=AssertionError("mode off reads no fact"))
    monkeypatch.setattr("daimon.core.private_answer.channel_has_outsiders", read)
    with capture_logs() as logs:
        assert await _offer(db_session_factory, tenant, "off", (INTERNAL,)) is False
    assert not [log for log in logs if log.get("kind") == "private_offer"]


async def test_a_press_is_checked_again_and_passes_once_the_role_is_added(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Spec check 4: a non-internal presser is refused; with the internal role it passes."""
    tenant = await _seed_admittable_tenant(db_session, policy=_policy())
    await _facts(db_session, tenant, {ROOM: CONTAINED, THREAD: CONTAINED})

    async def refusal(mode: str, roles: tuple[str, ...]) -> str | None:
        async with db_session_factory() as session:
            return await private_answer_refusal(
                session,
                tenant_id=tenant.id,
                user_id=USER,
                channel_id=ROOM,
                thread_id=THREAD,
                mode=mode,  # type: ignore[arg-type]
                event_role_ids=roles,
            )

    assert await refusal("shadow", (INTERNAL,)) == "unavailable"
    assert await refusal("on", (OTHER,)) == "not_internal"
    assert await refusal("on", (INTERNAL,)) == "dms_off"
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await db_session.commit()
    assert await refusal("on", (OTHER, INTERNAL)) is None
    await _facts(db_session, tenant, {ROOM: CONTAINED})  # a thread with no row is narrow
    assert await refusal("on", (INTERNAL,)) == "narrow"


async def _private_world(
    db_session: AsyncSession, factory: async_sessionmaker[AsyncSession], tmp_path: Path
):
    """A server in mode `on` with DMs on and the room contained."""
    tenant = await _seed_admittable_tenant(db_session, policy=_policy())
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: CONTAINED})
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await db_session.commit()
    deps = _deps(
        native_member_admin=False,
        sessionmaker=factory,
        defaults_root=tmp_path,
        router=_admittable_router(tenant),
    )
    return tenant, deps


async def _admit_private(deps: object, tenant: TenantRow, *, private: bool) -> Admission:
    return await admit_cold_dm(
        deps,  # type: ignore[arg-type]
        tenant_id=tenant.id,
        external_user_id=USER,
        channel_id=DM_ROUTE,
        role=Role.USER,
        role_ids=(INTERNAL,),
        verified_membership=True,
        agent_name="daimon",
        requester_org=RequesterOrg.HOST,
        dm_no_source=private,
    )


async def test_a_private_answer_runs_where_the_agents_budget_channel_is_contained(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """BUILD-PLAN E3 proof: the agent's budget channel is a contained room, and the
    Private answer still runs, as a DM turn with no hold and read-only shared memory.

    The budget channel is set on the admission: an agent whose rule gives it one (a
    channel's own agent) is refused every DM by policy, so only the precedence is real.
    """
    tenant, deps = await _private_world(db_session, db_session_factory, tmp_path)

    private = await _admit_private(deps, tenant, private=True)

    assert private.grant is not None and private.grant.dm_no_source and private.grant.is_dm
    assert private.hold is None and private.grant.hold is None, "no room to post into"
    assert private.memory_read_only, "shared memory is read-only in a private turn"
    await reauthorize(deps, replace(private, budget_channel_id=ROOM))

    plain = await _admit_private(deps, tenant, private=False)
    with pytest.raises(DmSourceSealedError):
        # A DM with no recorded source keeps the fallback.
        await reauthorize(deps, replace(plain, budget_channel_id=ROOM))


async def test_the_private_conversation_replaces_the_dm_and_its_follow_ups_keep_no_source(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A press during an existing DM conversation starts a fresh scope with no history,
    and every later turn there is admitted with no source."""
    tenant, deps = await _private_world(db_session, db_session_factory, tmp_path)
    admission = await _admit_private(deps, tenant, private=True)
    start = dict(
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key=DM_ROUTE,
        external_user_id=USER,
    )
    existing = await start_cold_dm(deps, admission, **start)  # type: ignore[arg-type]
    assert existing is not None and existing.source_url == COLD_DM_ORIGIN

    private = await start_cold_dm(
        deps,
        admission,
        **start,  # type: ignore[arg-type]
        expected_scope_id=existing.scope_id,
        private_answer=True,
    )

    assert private is not None and private.scope_id != existing.scope_id
    assert private.source_url == PRIVATE_ANSWER_ORIGIN and private.source_channel_id is None
    assert private.history == [] and private.context == ""
    assert private.private_answer and private.is_cold and not existing.private_answer
    async with db_session_factory() as session:
        current = await get_conversation(
            session, platform="discord", route_key=DM_ROUTE, external_user_id=USER
        )
    assert current is not None and current.scope_id == private.scope_id

    class Stop(Exception):
        pass

    admit = AsyncMock(side_effect=Stop)
    monkeypatch.setattr(direct_messages, "admit", admit)
    for message_id in ("101", "102"):  # the first turn and a follow-up
        with pytest.raises(Stop):
            await reply_to_dm_result(
                deps,  # type: ignore[arg-type]
                platform="discord",
                route_key=DM_ROUTE,
                external_user_id=USER,
                message_id=message_id,
                expected_scope_id=private.scope_id,
                text="the question",
                role=Role.USER,
                verified_membership=True,
            )
    assert [call.kwargs["dm_no_source"] for call in admit.await_args_list] == [True, True]
    assert all(call.kwargs["cold_dm"] for call in admit.await_args_list)
