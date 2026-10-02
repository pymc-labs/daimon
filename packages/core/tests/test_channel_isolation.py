"""Channel isolation: own agents by pin, refusals and visibility."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.access_policy import TenantAccessPolicy, is_isolated, isolation_owner
from daimon.core.answering_map import (
    AnsweringMap,
    ChannelAnswer,
    SetupThreadRef,
    hide_across_isolation,
)
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.channel_isolation import (
    ChannelIsolationStatus,
    IsolationViewer,
    binding_refusal,
    channel_isolation_status,
    clear_refusal,
    is_thread_turn_refused,
    keeps_routine_inside,
    routine_destination_place,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_agent
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT = DeploymentDefault(agent_name="daimon")
TENANT = uuid.uuid4()
POLICY = TenantAccessPolicy(
    sealed_channel_ids=("c1",),
    isolated_channel_ids=("c1",),
    agent_channel_pins={"local": ("c1",), "roamer": ("c1", "c3")},
)


def test_an_isolated_channel_must_be_sealed() -> None:
    with pytest.raises(ValidationError, match="must also be sealed"):
        TenantAccessPolicy(isolated_channel_ids=("c1",))


def test_own_agents_are_those_pinned_to_the_channel_alone() -> None:
    assert isolation_owner(POLICY, ("local",)) == "c1"
    assert isolation_owner(POLICY, ("roamer",)) is None, "pinned beyond the channel"
    assert isolation_owner(POLICY, ("shared",)) is None, "unpinned"
    assert isolation_owner(POLICY, ("local", "roamer")) is None, "every name counts"
    sealed_only = POLICY.model_copy(update={"isolated_channel_ids": ()})
    assert isolation_owner(sealed_only, ("local",)) is None, "a pin alone is no isolation"


def test_status_shows_the_seal_the_dedicated_pins_and_the_marker() -> None:
    assert channel_isolation_status(POLICY, "c1") == ChannelIsolationStatus(
        is_private=True, dedicated_agent_names=("local",), is_hidden=True
    ), "roamer is pinned beyond c1, so it is not dedicated"
    ended = channel_isolation_status(POLICY.model_copy(update={"isolated_channel_ids": ()}), "c1")
    assert (ended.is_hidden, ended.is_liftable) == (False, True), (
        "after ending, the seal and the pin are still there to lift"
    )
    assert not channel_isolation_status(POLICY, "c3").is_liftable, "c3 has no seal of its own"


def test_isolated_location_counts_threads_under_the_channel() -> None:
    assert is_isolated(POLICY, channel_id="t1", parent_channel_id="c1")
    assert not is_isolated(POLICY, channel_id="c2"), "other channels stay open"
    assert not is_isolated(TenantAccessPolicy(), channel_id="c1"), "default policy isolates none"


def test_only_own_agents_answer_inside() -> None:
    def refused(*names: str, channel: str, setup: bool = False) -> str | None:
        place = Place(channel_id="t1", parent_channel_id=channel, setup_thread=setup)
        return authorize(
            POLICY,
            subject=Subject(),
            action=Action.RUN_AGENT,
            agent=AgentRef.of(*names),
            place=place,
        ).reason

    assert refused("local", channel="c1") is None
    assert refused("shared", channel="c1") == "channel_isolated"
    assert refused("roamer", channel="c1") == "channel_isolated", "pinned beyond the channel"
    assert refused("alias", "local", channel="c1") is None, "any of its names makes it own"
    assert refused("daimon", channel="c1", setup=True) is None, "setup answers as the built-in"
    assert refused("shared", channel="c2") is None, "outside is the pin's to judge"
    assert refused("local", channel="c2") == "agent_pinned_elsewhere"


def test_an_own_agent_never_posts_or_messages_outside() -> None:
    def reason(action: Action, channel: str | None) -> str | None:
        return authorize(
            POLICY,
            subject=Subject(),
            action=action,
            agent=AgentRef.of("local"),
            place=Place(channel_id=channel),
        ).reason

    assert reason(Action.POST, "c1") is None
    assert reason(Action.POST, "c2") == "channel_isolated"
    assert reason(Action.DIRECT_MESSAGE, None) == "channel_isolated"


def test_binding_refusals_keep_own_agents_in_and_others_out() -> None:
    assert binding_refusal(POLICY, agent_names=("local",), channel_id="c1") is None
    assert binding_refusal(POLICY, agent_names=("local",), channel_id="c2") == "agent_confined"
    assert binding_refusal(POLICY, agent_names=("local",), channel_id=None) == "agent_confined"
    assert binding_refusal(POLICY, agent_names=("shared",), channel_id="c1") == (
        "channel_needs_own_agent"
    )
    assert binding_refusal(POLICY, agent_names=("shared",), channel_id="c2") is None
    assert clear_refusal(POLICY, channel_id="c1") == "channel_isolated"
    assert clear_refusal(POLICY, channel_id="c2") is None


def _routine(agent: str, destination: str | None) -> RoutineRow:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return RoutineRow(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        created_by_user_id="u1",
        agent_id="a",
        agent_name=agent,
        cron_expr="0 * * * *",
        timezone="UTC",
        trigger_message="hi",
        enabled=True,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        destination_kind="channel" if destination else None,
        destination_id=destination,
        channel_id=destination,
        created_at=now,
        updated_at=now,
    )


def test_a_routine_of_an_own_agent_or_into_the_channel_stays_inside() -> None:
    assert keeps_routine_inside(POLICY, _routine("local", "c1")), "never falls back to a DM"
    assert keeps_routine_inside(POLICY, _routine("shared", "c1"))
    assert not keeps_routine_inside(POLICY, _routine("shared", "c2"))


def test_a_thread_routine_saved_without_its_parent_stays_inside_until_placed() -> None:
    """A Discord thread saved before its parent was recorded may lie under C: it
    never goes by DM while anything is isolated, until its parent is resolved."""
    legacy = _routine("shared", "t9").model_copy(
        update={"destination_kind": "thread", "channel_id": None}
    )
    assert keeps_routine_inside(POLICY, legacy), "an unknown parent fails closed"
    assert not keeps_routine_inside(TenantAccessPolicy(), legacy), "nothing isolated"
    assert keeps_routine_inside(POLICY, legacy, parent_channel_id="c1"), "resolved under C"
    assert not keeps_routine_inside(POLICY, legacy, parent_channel_id="c2")
    assert routine_destination_place(legacy, channel_id="t9").parent_unresolved
    slack = legacy.model_copy(update={"destination_id": "c1:1.2"})
    assert not routine_destination_place(slack, channel_id="c1").parent_unresolved, (
        "a Slack thread carries its channel"
    )


def test_a_viewer_sees_only_its_side() -> None:
    inside, outside = IsolationViewer(POLICY, "c1"), IsolationViewer(POLICY, None)
    assert inside.sees("local") and not inside.sees("shared")
    assert outside.sees("shared") and not outside.sees("local")
    assert inside.sees_place("c1") and not inside.sees_place(None)
    assert outside.sees_place("c2") and not outside.sees_place("c1")


def test_a_viewer_sees_only_its_side_of_the_routing() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    answering = AnsweringMap(
        channel_overrides=(
            ChannelAnswer(channel_id="c1", agent_name="local"),
            ChannelAnswer(channel_id="c2", agent_name="shared"),
        ),
        deployment_default="daimon",
        setup_threads=(
            SetupThreadRef(
                thread_id="t1", parent_channel_id="c1", target_name="local", updated_at=now
            ),
            SetupThreadRef(
                thread_id="t2", parent_channel_id="c2", target_name="shared", updated_at=now
            ),
        ),
    )
    inside = hide_across_isolation(answering, IsolationViewer(POLICY, "c1"))
    assert [row.channel_id for row in inside.channel_overrides] == ["c1"]
    assert inside.deployment_default is None, "the shared fallback is across the line"
    assert [ref.thread_id for ref in inside.setup_threads] == ["t1"]
    outside = hide_across_isolation(answering, IsolationViewer(POLICY, None))
    assert [row.channel_id for row in outside.channel_overrides] == ["c2"]
    assert outside.deployment_default == "daimon"
    assert [ref.thread_id for ref in outside.setup_threads] == ["t2"]


async def test_thread_precheck_refuses_a_handed_thread_inside(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="c1"),
        tenant_id=tenant.id,
        agent_name="local",
        mode="agent",
    )
    for thread, responder in (("t1", "shared"), ("t2", "local"), ("t4", "alias")):
        await create_binding(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=thread,
            parent_channel_id="c1",
            responder_ma_agent_id=f"agent_{responder}",
            responder_name=responder,
            kind="handoff",
        )
    await db_session.commit()
    router = MARouter()
    aliased = ma_agent(
        id="agent_alias",
        name="local",
        tenant_id=tenant.id,
        metadata={MA_METADATA_KEY_NAME: "alias"},
    )
    router.add(
        "GET", r"/v1/agents", lambda _r, _m: list_response([aliased.model_dump(mode="json")])
    )
    anthropic = build_fake_anthropic(router.dispatch)

    async def refused(thread: str) -> bool:
        return await is_thread_turn_refused(
            db_session_factory,
            anthropic,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="c1",
            thread_id=thread,
            default=DEFAULT,
        )

    assert not await refused("t1"), "nothing isolated yet"
    await set_access_policy(db_session, tenant_id=tenant.id, policy=POLICY)
    await db_session.commit()
    assert (await load_access_policy(db_session, tenant_id=tenant.id)) == POLICY
    assert await refused("t1")
    assert not await refused("t2")
    assert not await refused("t3"), "an unbound thread answers as the channel's own agent"
    assert not await refused("t4"), "its MA name makes it an own agent"
