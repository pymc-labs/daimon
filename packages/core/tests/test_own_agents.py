"""A channel kept to its own agents: own agents by rule, refusals and visibility."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.answering_map import (
    AnsweringMap,
    ChannelAnswer,
    ChannelEnvironment,
    SetupThreadRef,
    hide_across_homes,
)
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.channel_rules import ChannelRuleStatus, channel_rule_status
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.permissions import agent_permissions, channel_permissions
from daimon.core.rule_views import (
    RoutineOrigin,
    RuleViewer,
    binding_refusal,
    clear_refusal,
    is_routine_parent_unknown,
    is_thread_turn_refused,
    keeps_routine_inside,
    routine_destination_channel,
    routine_destination_place,
    routine_origin,
)
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DEFAULT = DeploymentDefault(agent_name="daimon")
TENANT = uuid.uuid4()
OWN = ChannelRule(readers="own", writers="own")
POLICY = TenantAccessPolicy(
    channel_rules={"c1": OWN},
    agent_rules={"local": AgentRule(runs_in=("c1",)), "roamer": AgentRule(runs_in=("c1", "c3"))},
)
INSIDE_ONLY = POLICY.model_copy(update={"channel_rules": {"c1": ChannelRule(readers="inside")}})


def test_own_agents_are_those_whose_rule_names_the_channel_alone() -> None:
    def home(*names: str, policy: TenantAccessPolicy = POLICY) -> str | None:
        return agent_permissions(policy, names).home

    assert home("local") == "c1"
    assert home("roamer") is None, "its rule names more than the channel"
    assert home("shared") is None, "no rule"
    assert home("local", "roamer") is None, "every name counts"
    assert home("local", policy=INSIDE_ONLY) is None, "an agent rule alone keeps no channel"


def test_status_shows_the_rule_and_the_agents_kept_there() -> None:
    assert channel_rule_status(POLICY, "c1") == ChannelRuleStatus(OWN, ("local",)), (
        "roamer's rule names more than c1, so it is not kept there"
    )
    assert channel_rule_status(INSIDE_ONLY, "c1").agents == ("local",), (
        "readers inside, the agent rule is still there to release"
    )
    assert channel_rule_status(POLICY, "c3").agents == (), "c3 keeps no agent"


def test_a_thread_lies_in_its_channel() -> None:
    assert channel_permissions(POLICY, channel_id="t1", parent_channel_id="c1").home == "c1"
    assert channel_permissions(POLICY, channel_id="c2").home is None, "other channels stay open"
    assert channel_permissions(TenantAccessPolicy(), channel_id="c1").home is None


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
    assert refused("shared", channel="c1") == "own_agents_only"
    assert refused("roamer", channel="c1") == "own_agents_only", "its rule names more"
    assert refused("alias", "local", channel="c1") is None, "any of its names makes it own"
    assert refused("daimon", channel="c1", setup=True) is None, "setup answers as the built-in"
    assert refused("shared", channel="c2") is None, "outside, its rule judges"
    assert refused("local", channel="c2") == "runs_elsewhere"


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
    assert reason(Action.POST, "c2") == "own_agents_only"
    assert reason(Action.DIRECT_MESSAGE, None) == "own_agents_only"


def test_binding_refusals_keep_own_agents_in_and_others_out() -> None:
    assert binding_refusal(POLICY, agent_names=("local",), channel_id="c1") is None
    assert binding_refusal(POLICY, agent_names=("local",), channel_id="c2") == "agent_has_home"
    assert binding_refusal(POLICY, agent_names=("local",), channel_id=None) == "agent_has_home"
    assert binding_refusal(POLICY, agent_names=("shared",), channel_id="c1") == "not_own_agent"
    assert binding_refusal(POLICY, agent_names=("shared",), channel_id="c2") is None
    assert clear_refusal(POLICY, channel_id="c1") == "keeps_own"
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
    never goes by DM while any channel is kept to its own agents, until its parent is resolved."""
    legacy = _routine("shared", "t9").model_copy(
        update={"destination_kind": "thread", "channel_id": None}
    )
    assert keeps_routine_inside(POLICY, legacy), "an unknown parent fails closed"
    assert not keeps_routine_inside(TenantAccessPolicy(), legacy), "no channel kept"
    assert keeps_routine_inside(POLICY, legacy, parent_channel_id="c1"), "resolved under C"
    assert not keeps_routine_inside(POLICY, legacy, parent_channel_id="c2")
    assert routine_destination_place(legacy, channel_id="t9").parent_unresolved
    slack = legacy.model_copy(update={"destination_id": "c1:1.2"})
    assert not routine_destination_place(slack, channel_id="c1").parent_unresolved, (
        "a Slack thread carries its channel"
    )
    no_channel = legacy.model_copy(update={"destination_id": ":1.2"})
    assert is_routine_parent_unknown(no_channel), "an id with an empty channel part names none"
    assert routine_destination_channel(no_channel) == ":1.2", "so it is placed by itself"


_TEAMS_CHANNEL = "19:abc-123@thread.tacv2"


@pytest.mark.parametrize(
    ("kind", "destination"),
    [("channel", _TEAMS_CHANNEL), ("thread", f"{_TEAMS_CHANNEL};messageid=1700000000000")],
)
def test_a_teams_routine_saved_without_its_channel_is_placed_by_its_id(
    kind: str, destination: str
) -> None:
    """Teams ids contain ":", so they are never split there: a legacy Teams row
    without a saved channel lies in the channel its id names, which the rule sees."""
    legacy = _routine("shared", destination).model_copy(
        update={"destination_kind": kind, "channel_id": None}
    )
    assert routine_destination_channel(legacy) == _TEAMS_CHANNEL, "the id names its channel"
    assert not is_routine_parent_unknown(legacy), "a Teams thread id carries its channel"
    kept = TenantAccessPolicy(channel_rules={_TEAMS_CHANNEL: OWN})
    assert keeps_routine_inside(kept, legacy), "so its result never goes by DM"


def test_a_routine_session_is_stamped_where_it_fires_with_the_rule_now() -> None:
    """Channel, thread and readers limits follow the destination; Slack names a thread by its
    ts, Discord by its id; every stamp is private to the routine's owner; a routine
    with no channel at all is headless."""
    channel = _routine("local", "c1")
    private = f"routine:{channel.id}"
    assert routine_origin(POLICY, channel, platform="discord") == RoutineOrigin(
        "c1", None, frozenset({"c1"}), private
    )
    discord = channel.model_copy(update={"destination_kind": "thread", "destination_id": "t9"})
    assert routine_origin(POLICY, discord, platform="discord") == RoutineOrigin(
        "c1", "t9", frozenset({"c1"}), private
    ), "a Discord thread sits under its saved parent"
    slack = channel.model_copy(update={"destination_kind": "thread", "destination_id": "c1:1.2"})
    assert routine_origin(POLICY, slack, platform="slack") == RoutineOrigin(
        "c1", "1.2", frozenset({"c1"}), private
    )
    open_channel = _routine("shared", "c2")
    assert routine_origin(POLICY, open_channel, platform="discord") == RoutineOrigin(
        "c2", None, frozenset(), f"routine:{open_channel.id}"
    ), "an open destination limits no readers"
    no_destination = _routine("shared", None).model_copy(update={"channel_id": "c1"})
    assert routine_origin(POLICY, no_destination, platform="discord") == RoutineOrigin(
        "c1", None, frozenset({"c1"}), f"routine:{no_destination.id}"
    ), "without a destination it runs where it was made"
    assert routine_origin(POLICY, _routine("shared", None), platform="discord") is None


def test_a_viewer_sees_only_its_side() -> None:
    inside, outside = RuleViewer(POLICY, "c1"), RuleViewer(POLICY, None)
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
    inside = hide_across_homes(answering, RuleViewer(POLICY, "c1"))
    assert [row.channel_id for row in inside.channel_overrides] == ["c1"]
    assert inside.deployment_default is None, "the shared fallback is across the line"
    assert [ref.thread_id for ref in inside.setup_threads] == ["t1"]
    outside = hide_across_homes(answering, RuleViewer(POLICY, None))
    assert [row.channel_id for row in outside.channel_overrides] == ["c2"]
    assert outside.deployment_default == "daimon"
    assert [ref.thread_id for ref in outside.setup_threads] == ["t2"]


def test_a_viewer_sees_only_its_side_of_the_environments() -> None:
    """Both setup panels draw environments from this map, so the line holds for them too."""
    answering = AnsweringMap(
        channel_environments=(
            ChannelEnvironment(channel_id="c1", environment_name="private"),
            ChannelEnvironment(channel_id="c2", environment_name="shared"),
        ),
        tenant_environment="workspace",
        deployment_environment="default",
    )
    inside = hide_across_homes(answering, RuleViewer(POLICY, "c1"))
    assert [row.channel_id for row in inside.channel_environments] == ["c1"], (
        "an insider sees only its own channel's environment"
    )
    assert (inside.tenant_environment, inside.deployment_environment) == (None, None), (
        "the shared fallbacks are across the line, as the agent defaults are"
    )
    outside = hide_across_homes(answering, RuleViewer(POLICY, None))
    assert [row.channel_id for row in outside.channel_environments] == ["c2"], (
        "an outsider never sees the kept channel's environment"
    )
    assert (outside.tenant_environment, outside.deployment_environment) == (
        "workspace",
        "default",
    )


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

    assert not await refused("t1"), "no channel kept yet"
    await set_access_policy(db_session, tenant_id=tenant.id, policy=POLICY)
    await db_session.commit()
    assert (await load_access_policy(db_session, tenant_id=tenant.id)) == POLICY
    assert await refused("t1")
    assert not await refused("t2")
    assert not await refused("t3"), "an unbound thread answers as the channel's own agent"
    assert not await refused("t4"), "its MA name makes it an own agent"
