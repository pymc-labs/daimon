"""Discord rooms in `audience_hold` modes: the stored facts hold a turn in mode `on` only."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.audience_facts import RoomFacts
from daimon.core.channel_audience import audience_mode as _real_audience_mode
from daimon.core.features.snapshot import CACHE, FeaturePlace
from daimon.core.stores.audience_facts import StoredRoom, publish_facts
from daimon.core.stores.direct_messages import set_dm_enabled
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.shared_channels import is_channel_shared
from daimon.core.turn.admission import (
    Admission,
    AdmissionDenied,
    StandingRequest,
    admit,
    reauthorize,
)
from daimon.core.turn.errors import DmSourceSealedError
from daimon.core.turn_hold import RequesterOrg, TurnHold
from daimon.testing.builders import admit_chat
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_admission import _NOW, _admittable_router, _deps, _seed_admittable_tenant

ROOM = "7001"
THREAD = "7002"
USER = "8001"
RELAY_APP = "https://daimon.example.com"
CONTAINED = RoomFacts(contained=True, narrow=False)
OPEN = RoomFacts(contained=False, narrow=False)
NARROW = RoomFacts(contained=False, narrow=True)


_MODES: dict[uuid.UUID, str] = {}


@pytest.fixture(autouse=True)
def _mode_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry has no `on` value yet, so the mode comes from a table the tests set."""
    _MODES.clear()

    async def mode(_session: AsyncSession, place: FeaturePlace, **_kw: object) -> str:
        return _MODES.get(place.tenant_id, "off")

    monkeypatch.setattr("daimon.core.turn.admission.audience_mode", mode)
    monkeypatch.setattr("daimon.core.channel_audience.audience_mode", mode)
    monkeypatch.setattr("daimon.core.host_memory.audience_mode", mode)


async def _mode(db_session: AsyncSession, tenant: TenantRow, value: str) -> None:
    _MODES[tenant.id] = value


async def _facts(
    db_session: AsyncSession, tenant: TenantRow, rooms: dict[str, RoomFacts | StoredRoom]
) -> None:
    now = datetime.now(UTC)
    await publish_facts(
        db_session,
        tenant_id=tenant.id,
        rooms={k: v if isinstance(v, StoredRoom) else StoredRoom(v) for k, v in rooms.items()},
        now=now,
        fresh_until=now + timedelta(hours=1),
    )
    await db_session.commit()


async def _admit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    tenant: TenantRow,
    *,
    org: RequesterOrg = RequesterOrg.HOST,
    thread_id: str | None = None,
    is_dm: bool = False,
    **fields: object,
) -> Admission:
    deps = _deps(sessionmaker=factory, defaults_root=tmp_path, router=_admittable_router(tenant))
    return await admit_chat(
        deps,
        tenant,
        platform="discord",
        external_user_id=USER,
        channel_id=ROOM,
        thread_id=thread_id,
        now=_NOW,
        role=Role.USER,
        requester_org=org,
        is_dm=is_dm,
        **fields,
    )


async def test_a_contained_room_holds_the_turn_and_writes_no_permanent_mark(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec checks 1 and 2 at admission; the room is not recorded as shared for good."""
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: CONTAINED})

    admission = await _admit(db_session_factory, tmp_path, tenant)

    assert admission.hold == TurnHold("discord", ROOM, client=False)
    assert admission.shared_channel is True
    assert not await is_channel_shared(
        db_session, tenant_id=tenant.id, platform="discord", channel_id=ROOM
    )


async def test_the_room_opens_again_when_its_outsider_leaves(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec check 10: the next turn after the facts relax is not held."""
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: CONTAINED})
    assert (await _admit(db_session_factory, tmp_path, tenant)).hold is not None

    await _facts(db_session, tenant, {ROOM: OPEN})

    assert (await _admit(db_session_factory, tmp_path, tenant)).hold is None


async def test_a_room_with_no_facts_is_contained_in_mode_on(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec check 8 at admission: unknown is contained."""
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")

    admission = await _admit(db_session_factory, tmp_path, tenant)

    assert admission.hold is not None and admission.memory_read_only, "unknown is narrow too"


async def test_a_thread_under_a_contained_room_is_held_to_the_room(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: CONTAINED})

    admission = await _admit(db_session_factory, tmp_path, tenant, thread_id=THREAD)

    assert admission.hold == TurnHold("discord", ROOM, client=False)


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_modes_off_and_shadow_hold_nothing_and_shadow_logs(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mode: str,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, mode)
    await _facts(db_session, tenant, {ROOM: CONTAINED})

    with structlog.testing.capture_logs() as logs:
        admission = await _admit(db_session_factory, tmp_path, tenant)

    assert admission.hold is None and admission.shared_channel is False
    shadow = [e for e in logs if e["event"] == "audience.shadow"]
    if mode == "off":
        assert shadow == []
        return
    shadow = [e for e in shadow if e["kind"] == "turn_held"]
    assert [(e["kind"], e["channel_id"], e["reason"]) for e in shadow] == [
        ("turn_held", ROOM, "room_contained")
    ]
    assert set(shadow[0]) == {
        "event",
        "log_level",
        "kind",
        "tenant_id",
        "channel_id",
        "agent",
        "reason",
    }


async def test_a_narrow_room_makes_memory_read_only_even_for_its_own_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """The agent pinned to a room is exempt from a seal, not from a narrow room."""
    policy = TenantAccessPolicy(agent_rules={"daimon": AgentRule(runs_in=(ROOM,))})
    tenant = await _seed_admittable_tenant(db_session, policy=policy)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: OPEN})
    assert not (await _admit(db_session_factory, tmp_path, tenant)).memory_read_only

    await _facts(db_session, tenant, {ROOM: NARROW})

    narrow = await _admit(db_session_factory, tmp_path, tenant)
    assert narrow.memory_read_only and narrow.hold is None


async def test_reauthorize_holds_a_turn_whose_room_became_contained_and_never_relaxes(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: OPEN})
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    admission = await _admit(db_session_factory, tmp_path, tenant)
    assert admission.hold is None

    await _facts(db_session, tenant, {ROOM: CONTAINED})
    held = await reauthorize(deps, admission)
    assert held.hold == TurnHold("discord", ROOM, client=False) and held.shared_channel

    await _facts(db_session, tenant, {ROOM: OPEN})
    assert (await reauthorize(deps, held)).hold == held.hold


async def _relay_agent_admit(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    tenant: TenantRow,
    *,
    names: tuple[str, ...] = ("grove",),
    unscoped: frozenset[str] = frozenset({"grove"}),
):
    router = resolved_agent_env_router(
        ma_agent(
            id="ag_1",
            name="daimon",
            tenant_id=tenant.id,
            mcp_servers=[
                *(
                    {"type": "url", "name": n, "url": f"{RELAY_APP}/mcp-identity/{n}"}
                    for n in names
                ),
                {"type": "url", "name": "other", "url": "https://tools.example.com/mcp"},
            ],
        ),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    deps = replace(
        _deps(sessionmaker=db_session_factory, defaults_root=tmp_path, router=router),
        public_url=f"{RELAY_APP}/mcp",
        relay_unscoped=unscoped,
    )
    CACHE.clear()
    admission = await admit(
        deps,
        origin="chat",
        tenant_id=tenant.id,
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id=USER,
        channel_id=ROOM,
        role=Role.USER,
        now=_NOW,
    )
    return deps, admission


@pytest.mark.parametrize(("mode", "hidden"), [("off", False), ("shadow", False), ("on", True)])
async def test_a_contained_room_hides_signed_identity_servers_and_keeps_room_memory(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mode: str,
    hidden: bool,
) -> None:
    """Spec check 1 and 11 at admission: no signed-identity server, plain hold, room memory."""
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, mode)
    await _facts(db_session, tenant, {ROOM: CONTAINED})

    with structlog.testing.capture_logs() as logs:
        _deps_, admission = await _relay_agent_admit(
            db_session, db_session_factory, tmp_path, tenant
        )

    names = {server.name for server in admission.agent.mcp_servers}
    assert ("grove" not in names) is hidden and "other" in names
    if hidden:
        assert admission.hold == TurnHold("discord", ROOM, client=False), "plain, not a relay hold"
        assert not admission.relay_scoped
        assert admission.identity.channel_memory, "the room keeps its own memory"
    if mode == "shadow":
        kinds = [e["kind"] for e in logs if e["event"] == "audience.shadow"]
        assert "tool_removed" in kinds


async def test_a_room_that_became_contained_loses_the_identity_servers_at_reauthorize(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: OPEN})
    deps, admission = await _relay_agent_admit(db_session, db_session_factory, tmp_path, tenant)
    assert "grove" in {s.name for s in admission.agent.mcp_servers}

    await _facts(db_session, tenant, {ROOM: CONTAINED})
    held = await reauthorize(deps, admission)

    assert "grove" not in {s.name for s in held.agent.mcp_servers}
    assert "grove" not in {
        t.mcp_server_name for t in held.agent.tools if hasattr(t, "mcp_server_name")
    }


async def test_a_dm_with_no_recorded_source_uses_the_budget_channel_only_when_none_was_recorded(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """C4: the budget channel stands in for a DM's source only when no source was recorded."""
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: CONTAINED})
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await db_session.commit()
    deps = _deps(
        native_member_admin=False,
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=_admittable_router(tenant),
    )
    dm = await admit_chat(
        deps, tenant, platform="discord", external_user_id=USER, channel_id="dm-1",
        is_dm=True, role=Role.USER, verified_membership=True, now=_NOW,
    )  # fmt: skip
    assert dm.grant is not None and not dm.grant.dm_no_source
    unrecorded = replace(dm, budget_channel_id=ROOM)
    with pytest.raises(DmSourceSealedError):
        await reauthorize(deps, unrecorded)

    assert unrecorded.grant is not None
    private = replace(unrecorded, grant=replace(unrecorded.grant, dm_no_source=True))
    await reauthorize(deps, private)


async def test_an_outsider_asking_in_a_room_is_held_and_refused_every_private_surface(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Spec check 4: OUTSIDER gets room turns only (DM, hub, agent chat, routines, agent setup)."""
    from daimon.core.authz import Action, Surface, authorize

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    admission = await _admit(db_session_factory, tmp_path, tenant, org=RequesterOrg.OUTSIDER)
    assert admission.grant is not None
    subject = admission.grant.subject
    assert subject.client
    policy = TenantAccessPolicy()
    for surface in (Surface.DM, Surface.HUB, Surface.AGENT_CHAT, Surface.ROUTINE):
        assert not authorize(policy, subject=subject, action=Action.START_TURN, surface=surface)
    for action in (Action.SAVE_ROUTINE, Action.CREATE_AGENT, Action.CONFIGURE):
        assert not authorize(policy, subject=subject, action=action)
    with pytest.raises(AdmissionDenied):
        await _admit(db_session_factory, tmp_path, tenant, org=RequesterOrg.OUTSIDER, is_dm=True)


_BOTH = {"names": ("grove", "tools"), "unscoped": frozenset({"tools"})}


async def test_a_contained_room_strips_scoped_and_unscoped_identity_servers_alike(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """`channel_scoped=false` changes a relay agent's hold, not the room rule."""
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: CONTAINED})
    deps, admission = await _relay_agent_admit(
        db_session, db_session_factory, tmp_path, tenant, **_BOTH
    )
    assert {s.name for s in admission.agent.mcp_servers} == {"other"}

    await _facts(db_session, tenant, {ROOM: OPEN})
    deps, open_admission = await _relay_agent_admit(
        db_session, db_session_factory, tmp_path, tenant, **_BOTH
    )
    assert {s.name for s in open_admission.agent.mcp_servers} == {"grove", "tools", "other"}
    assert open_admission.relay_scoped, "the scoped server still holds an open room's turn"

    await _facts(db_session, tenant, {ROOM: CONTAINED})
    held = await reauthorize(deps, open_admission)
    assert {s.name for s in held.agent.mcp_servers} == {"other"}, "a reused turn loses both"


async def test_an_open_room_keeps_an_agent_with_only_an_unscoped_identity_server_unheld(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: OPEN})

    _deps_, admission = await _relay_agent_admit(
        db_session,
        db_session_factory,
        tmp_path,
        tenant,
        names=("tools",),
        unscoped=frozenset({"tools"}),
    )

    assert admission.hold is None and not admission.relay_scoped
    assert {s.name for s in admission.agent.mcp_servers} == {"tools", "other"}


async def test_a_mode_off_mention_turn_reads_the_mode_once_and_no_audience_rows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Query count: standing, hold and facts share one mode read; off touches no facts,
    members or policy-role rows for them."""
    from sqlalchemy import event

    tenant = await _seed_admittable_tenant(db_session, policy=None)
    reads: list[str] = []
    real = _real_audience_mode

    async def counting(*args: object, **kwargs: object) -> str:
        # The policy's own per-transaction read (`stores.access_policy`) has no channel.
        if isinstance(args[1], FeaturePlace) and args[1].channel_id is not None:
            reads.append("mode")
        return await real(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr("daimon.core.turn.admission.audience_mode", counting)
    monkeypatch.setattr("daimon.core.channel_audience.audience_mode", counting)
    statements: list[str] = []
    sync_target = db_session_factory.kw["bind"].sync_connection  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType]

    def record(_conn: object, _cur: object, statement: str, *_a: object) -> None:
        statements.append(statement)

    event.listen(sync_target, "before_cursor_execute", record)
    try:
        admission = await _admit(
            db_session_factory, tmp_path, tenant, standing=StandingRequest(("1",))
        )
    finally:
        event.remove(sync_target, "before_cursor_execute", record)

    assert admission.hold is None
    assert reads == ["mode"], "one mode read serves standing, hold and facts"
    touched = [
        t
        for t in ("discord_channel_audience", "discord_audience_state", "workspace_members")
        for st in statements
        if t in st
    ]
    assert touched == [], "mode off reads no audience facts or member rows"


async def test_admission_works_out_the_standing_in_its_own_transaction(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from daimon.core.access_policy import TenantAccessPolicy

    tenant = await _seed_admittable_tenant(
        db_session, policy=TenantAccessPolicy(internal_role_ids=("9001",))
    )
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: OPEN})

    outsider = await _admit(db_session_factory, tmp_path, tenant, standing=StandingRequest(("1",)))
    internal = await _admit(
        db_session_factory, tmp_path, tenant, standing=StandingRequest(("9001",))
    )

    assert outsider.hold is not None and outsider.hold.client
    assert internal.hold is None


@pytest.mark.parametrize(("mode", "read_only"), [("off", False), ("shadow", False), ("on", True)])
async def test_a_dm_turns_memory_is_read_only_only_in_mode_on(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mode: str,
    read_only: bool,
) -> None:
    """The one audience rule (`audience_memory_read_only`) reaches a DM's mounted memory."""
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    await _mode(db_session, tenant, mode)
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await db_session.commit()
    deps = _deps(
        native_member_admin=False,
        sessionmaker=db_session_factory,
        defaults_root=tmp_path,
        router=_admittable_router(tenant),
    )

    dm = await admit_chat(
        deps,
        tenant,
        platform="discord",
        external_user_id=USER,
        channel_id="dm-1",
        is_dm=True,
        role=Role.USER,
        verified_membership=True,
        now=_NOW,
    )

    assert dm.memory_read_only is read_only
