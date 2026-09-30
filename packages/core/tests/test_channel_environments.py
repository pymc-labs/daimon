"""Channel environments: the shared write and the sentences every surface says."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.answering_map import AnsweringMap, ChannelEnvironment
from daimon.core.channel_environments import (
    ENVIRONMENT_OPTION_INHERIT,
    build_archive_environment_note,
    build_clear_environment_note,
    build_environment_resolution_note,
    build_set_environment_note,
    environment_choices,
    environment_option_value,
    list_environment_names,
    parse_environment_option,
    plan_environment_picker,
    save_scope_environment,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, ScopeContext, TenantScopeRef
from daimon.core.stores.scoped_config_read import get_scope, resolve
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing import ma_environment
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession


async def test_save_scope_environment_sets_replaces_and_clears_a_channel(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    actor = await make_account(db_session, tenant=tenant)
    scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="c1")

    first = await save_scope_environment(
        db_session,
        tenant_id=tenant.id,
        channel_id="c1",
        environment_name="science",
        actor_account_id=actor.id,
    )
    second = await save_scope_environment(
        db_session,
        tenant_id=tenant.id,
        channel_id="c1",
        environment_name="gpu",
        actor_account_id=actor.id,
    )
    resolved = await resolve(
        db_session,
        context=ScopeContext(tenant_id=tenant.id, channel_id="c1"),
        default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )

    assert first is None, "a channel with no row had no environment before"
    assert second == "science", "the replaced environment is reported"
    assert (resolved.environment_name, resolved.environment_name_tier) == ("gpu", "channel"), (
        "a turn in the channel must now resolve the channel's own environment"
    )
    assert resolved.agent_name == "daimon", "an environment-only row leaves the agent alone"

    cleared = await save_scope_environment(
        db_session,
        tenant_id=tenant.id,
        channel_id="c1",
        environment_name=None,
        actor_account_id=actor.id,
    )
    assert cleared == "gpu", "clearing reports what it removed"
    assert await get_scope(db_session, scope=scope) is None, (
        "a row left with no setting is removed, so the channel is back to no row at all"
    )


async def test_clearing_a_channel_environment_keeps_its_agent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    scope = ChannelScopeRef(tenant_id=tenant.id, channel_id="c1")
    await set_fields(
        db_session, scope=scope, tenant_id=tenant.id, agent_name="alpha", environment_name="gpu"
    )

    await save_scope_environment(
        db_session,
        tenant_id=tenant.id,
        channel_id="c1",
        environment_name=None,
        actor_account_id=None,
    )
    row = await get_scope(db_session, scope=scope)

    assert row is not None and row.agent_name == "alpha", "the channel's agent must survive"
    assert row.environment_name is None, "only the environment is cleared"


async def test_save_scope_environment_without_a_channel_writes_the_tenant_default(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    previous = await save_scope_environment(
        db_session,
        tenant_id=tenant.id,
        channel_id=None,
        environment_name="shared",
        actor_account_id=None,
    )
    noop = await save_scope_environment(
        db_session,
        tenant_id=tenant.id,
        channel_id="c9",
        environment_name=None,
        actor_account_id=None,
    )
    row = await get_scope(db_session, scope=TenantScopeRef(tenant_id=tenant.id))

    assert previous is None, "the tenant had no environment of its own"
    assert noop is None, "clearing a channel with nothing set is a no-op"
    assert row is not None and row.environment_name == "shared", "the tenant row names it"


def test_archive_note_counts_the_cleared_picks() -> None:
    assert "No channel or workspace picked it" in build_archive_environment_note(
        environment_name="gpu", cleared=0
    ), "an unpicked environment says nothing else changed"
    assert "cleared 1 pick of it" in build_archive_environment_note(
        environment_name="gpu", cleared=1
    ), "one pick is singular"
    assert "cleared 3 picks of it" in build_archive_environment_note(
        environment_name="gpu", cleared=3
    ), "the note says how many picks were cleared"


def test_environment_notes_name_the_scope_and_the_tier() -> None:
    assert "Channel c1 now runs in the gpu environment" in build_set_environment_note(
        environment_name="gpu", channel="c1"
    ), "a set names the channel and the environment"
    assert "workspace default" in build_set_environment_note(
        environment_name="gpu", channel=None
    ), "no channel is the workspace default"
    assert "nothing changed" in build_clear_environment_note(channel="c1", cleared=False), (
        "a clear with nothing set says so"
    )
    assert "falls through" in build_clear_environment_note(channel="c1", cleared=True), (
        "a clear says where the channel goes"
    )
    assert "workspace default" in build_environment_resolution_note(
        environment_name="shared", tier="tenant", channel_id="c1"
    ), "the note must say which tier chose the environment"
    assert "cannot start" in build_environment_resolution_note(
        environment_name=None, tier=None, channel_id="c1"
    ), "an unresolved environment must say a mention there fails"


def test_environment_choices_keeps_the_current_pick_when_the_list_is_cut() -> None:
    names = ["a", "b", "c", "d"]

    assert environment_choices(names, current="d", limit=3) == ("a", "b", "d"), (
        "the channel's own environment must stay selectable"
    )
    assert environment_choices(names, current="b", limit=3) == ("a", "b", "c"), (
        "a current pick already listed changes nothing"
    )
    assert environment_choices(names, current="gone", limit=3) == ("a", "b", "c"), (
        "a name no longer in the tenant is not invented"
    )


def test_environment_options_round_trip_and_refuse_anything_else() -> None:
    assert parse_environment_option(environment_option_value("gpu")) == "gpu", "round trip"
    assert parse_environment_option(ENVIRONMENT_OPTION_INHERIT) is None, "inherit clears"
    for forged in ("gpu", "env:", ""):
        with pytest.raises(ValueError, match="not an environment option"):
            parse_environment_option(forged)


def test_the_picker_names_the_channels_own_pick_and_what_it_inherits() -> None:
    answering_map = AnsweringMap(
        channel_environments=(ChannelEnvironment(channel_id="c1", environment_name="gpu"),),
        tenant_environment="shared",
        deployment_environment="default",
    )

    own = plan_environment_picker(
        answering_map, channel_id="c1", names=["a", "gpu"], limit=1, max_value_length=100
    )
    other = plan_environment_picker(
        answering_map, channel_id="c2", names=["a", "x" * 97], limit=5, max_value_length=100
    )

    assert own is not None and (own.own, own.inherited, own.names) == ("gpu", "shared", ("gpu",)), (
        "the channel's own pick survives a cut list"
    )
    assert other is not None and (other.own, other.names) == (None, ("a",)), (
        "a channel on the default has no pick of its own; a name too long to fit is left out"
    )
    assert (
        plan_environment_picker(
            AnsweringMap(), channel_id="c2", names=[], limit=5, max_value_length=100
        )
        is None
    ), "nothing to pick and nothing to clear offers no picker"


async def test_list_environment_names_reads_only_this_tenants_resolver_names() -> None:
    tenant_id = uuid.uuid4()
    router = MARouter()
    router.add_environment_list(
        ma_environment(id="env_1", name="Science", tenant_id=tenant_id),
        ma_environment(id="env_2", name="default", tenant_id=tenant_id),
        ma_environment(id="env_3", name="default", tenant_id=tenant_id),
        ma_environment(id="env_4", name="other", tenant_id=uuid.uuid4()),
        ma_environment(
            id="env_5", name="untagged", metadata={MA_METADATA_KEY_TENANT: str(tenant_id)}
        ),
    )

    names = await list_environment_names(build_fake_anthropic(router.dispatch), tenant_id=tenant_id)

    assert names == ["default", "Science"], (
        "deduplicated, case-insensitive order, this tenant only, and never a name a save "
        "could not find"
    )
