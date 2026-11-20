"""The Private answer from a contained room (`audience_hold`, BUILD-PLAN C4, E3).

The offer shows in mode `on` only, to an internal asker, in a contained room that is
not narrow, unless the press would certainly be refused (DMs off, or an agent that never
DMs). A press is checked again. The private answer and its follow-up run as real DM
turns (admission, session binding, the fake model backend) with no source. Ids are
fictional. Mode `on` comes from the table in
`test_admission_audience`, because the registry has no `on` value yet.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.core import direct_messages
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, authorize
from daimon.core.channel_audience import channel_has_outsiders
from daimon.core.direct_messages import admit_cold_dm, reply_to_dm_result, start_cold_dm
from daimon.core.private_answer import offer_private_answer, private_answer_refusal
from daimon.core.stores import usage_events
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.direct_messages import (
    COLD_DM_ORIGIN,
    PRIVATE_ANSWER_ORIGIN,
    DirectMessageRow,
    get_conversation,
    set_dm_enabled,
)
from daimon.core.stores.dm_carry_overs import put_carry_over
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.turn.admission import Admission
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn_hold import RequesterOrg
from mux.testing.drivers.anthropic.ma import list_response
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from .test_admission import _deps, _seed_admittable_tenant
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
from .test_chokepoint import _chokepoint_router

INTERNAL = "9001"
OTHER = "9002"
DM_ROUTE = "7501"
ROOM_WORDS = "ROOM-ONLY-WORDS-4417"


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
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sent: list[str],
) -> tuple[TenantRow, TurnDeps]:
    """A server in mode `on` with DMs on and the room contained. The fake model backend
    answers every turn with one billed model call; `sent` collects what reached it."""
    tenant = await _seed_admittable_tenant(db_session, policy=_policy())
    await _mode(db_session, tenant, "on")
    await _facts(db_session, tenant, {ROOM: CONTAINED})
    await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await db_session.commit()
    bodies: list[dict[str, Any]] = []
    streams: list[str] = []
    router = _chokepoint_router(tenant_id=tenant.id, session_bodies=bodies)

    def created(session_id: str) -> httpx.Response:
        """The session as created, its memory mounts included."""
        body = bodies[int(session_id.removeprefix("sess_")) - 1]
        row = ma_session(
            id=session_id,
            agent=ma_session_agent(id=body["agent"], name="daimon"),
            environment_id=body["environment_id"],
            resources=body.get("resources", ()),
        )
        return httpx.Response(200, json=row.model_dump(mode="json"))

    def session(_request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        return created(match["sid"])

    # Read, update (a reused session's metadata) and archive return it as created.
    router.add("GET", r"/v1/sessions/(?P<sid>sess_\d+)", session)
    router.add("POST", r"/v1/sessions/(?P<sid>sess_\d+)(?:/archive)?", session)
    router.add("GET", r"/v1/sessions/[^/]+/events", lambda _r, _m: list_response([]))

    def dispatch(request: httpx.Request) -> httpx.Response:
        response = router.dispatch(request)
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return created(json.loads(response.content)["id"])
        if request.method == "POST" and request.url.path.endswith("/events"):
            sent.append(request.content.decode())
        if request.url.path.endswith("/events/stream"):  # one model call per turn
            streams.append(request.url.path)
            content = response.content.replace(b"evt_span", f"evt_span_{len(streams)}".encode())
            return httpx.Response(200, headers=response.headers, content=content)
        return response

    deps = _deps(
        native_member_admin=False,
        sessionmaker=factory,
        defaults_root=tmp_path,
        router=SimpleNamespace(dispatch=dispatch),  # type: ignore[arg-type]
    )
    return tenant, deps


async def _admit_private(deps: TurnDeps, tenant: TenantRow, *, private: bool) -> Admission:
    return await admit_cold_dm(
        deps,
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


async def _current(factory: async_sessionmaker[AsyncSession]) -> DirectMessageRow:
    async with factory() as session:
        row = await get_conversation(
            session, platform="discord", route_key=DM_ROUTE, external_user_id=USER
        )
    assert row is not None
    return row


async def test_a_private_answer_and_its_follow_up_run_with_no_source(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUILD-PLAN C4 end to end: the press replaces the asker's DM, then the answer and a
    follow-up run as real turns (admission, session binding, the fake model backend).
    Each turn has no source, imports nothing from the room, is billed to the asker, and
    cannot post into the contained room."""
    sent: list[str] = []
    tenant, deps = await _private_world(db_session, db_session_factory, tmp_path, sent)
    start = dict(
        tenant_id=tenant.id,
        workspace_id=tenant.external_id,
        route_key=DM_ROUTE,
        external_user_id=USER,
    )
    plain = await _admit_private(deps, tenant, private=False)
    existing = await start_cold_dm(deps, plain, **start)  # type: ignore[arg-type]
    assert existing is not None and existing.source_url == COLD_DM_ORIGIN
    async with db_session_factory.begin() as session:  # room words waiting for the DM
        await put_carry_over(
            session,
            tenant_id=tenant.id,
            platform="discord",
            external_user_id=USER,
            agent_name="daimon",
            source_name="the room",
            exported_at=datetime.now(UTC),
            history=[{"role": "user", "text": ROOM_WORDS}],
            context=ROOM_WORDS,
        )

    pressed = await _admit_private(deps, tenant, private=True)
    private = await start_cold_dm(
        deps,
        pressed,
        **start,  # type: ignore[arg-type]
        expected_scope_id=existing.scope_id,
        private_answer=True,
    )
    assert private is not None and private.scope_id != existing.scope_id
    assert private.source_url == PRIVATE_ANSWER_ORIGIN and private.history == []

    prepared: list[PreparedTurn] = []
    real_bind = direct_messages.bind_session

    async def bind(*args: Any, **kwargs: Any) -> PreparedTurn:
        turn = await real_bind(*args, **kwargs)
        prepared.append(turn)
        return turn

    monkeypatch.setattr(direct_messages, "bind_session", bind)

    for message_id in ("101", "102"):  # the answer, then a follow-up
        conversation = await _current(db_session_factory)  # reloaded between turns
        assert conversation.scope_id == private.scope_id
        assert conversation.private_answer and conversation.source_channel_id is None
        reply = await reply_to_dm_result(
            deps,
            platform="discord",
            route_key=DM_ROUTE,
            external_user_id=USER,
            message_id=message_id,
            expected_scope_id=conversation.scope_id,
            text=f"question {message_id}",
            role=Role.USER,
            role_ids=(INTERNAL,),
            verified_membership=True,
        )
        assert reply is not None

    assert len(prepared) == 2
    assert not prepared[0].reused and prepared[1].reused, "the follow-up keeps the session"
    for turn in prepared:
        grant = turn.admission.grant
        assert grant is not None and grant.dm_no_source and grant.dm_source is None
        assert grant.subject.platform_user_id == USER
        assert turn.admission.hold is None, "no room to post into"
        assert turn.admission.memory_read_only, "shared memory is read-only"
    after = await _current(db_session_factory)
    assert after.context == "" and after.source_channel_id is None
    assert [t["text"] for t in after.history if t["role"] == "user"] == [
        "question 101",
        "question 102",
    ]
    assert any("question 101" in body for body in sent)
    assert not any(ROOM_WORDS in body for body in sent), "no room history reached the model"

    async with db_session_factory() as session:
        usage = await usage_events.list_for_tenant(session, tenant_id=tenant.id)
        audience = await channel_has_outsiders(
            session, tenant_id=tenant.id, platform="discord", channel_id=ROOM
        )
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert [row.platform_user_id for row in usage] == [USER, USER], "billed to the asker"
    post = authorize(
        policy,
        subject=prepared[-1].admission.grant.subject,  # type: ignore[union-attr]
        action=Action.POST,
        place=Place(channel_id=ROOM),
        hold=prepared[-1].admission.hold,
        audience=audience,
    )
    assert not post and post.reason == "audience_contained"
