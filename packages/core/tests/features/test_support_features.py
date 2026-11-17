"""Ask a person's features: the tenant-aware requirements, the feedback env map, own channel."""

from __future__ import annotations

from decimal import Decimal

import pytest
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    SupportSettings,
)
from daimon.core.features.service import import_env, set_workspace_feature
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores.feature_values import list_feature_values
from daimon.core.stores.support import set_limit, set_support_channel
from daimon.core.support_routing import feedback_destination, own_channel_allowed
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession


def _settings(features: dict[str, str] | None = None, **support: object) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "support": SupportSettings.model_validate(support),
            "features": features or {},
        }
    )


async def _limit(db_session: AsyncSession, tenant_id: object, person: str | None = None) -> None:
    await set_limit(
        db_session,
        tenant_id=tenant_id,  # pyright: ignore[reportArgumentType]
        scope="workspace",
        scope_id="",
        platform_user_id=person,
        shared=None,
        each_person=Decimal(5),
        window="total",
        starts_at=None,
        ends_at=None,
        set_by_account_id=None,
    )


async def test_the_workspaces_own_channel_meets_the_channel_requirement(
    db_session: AsyncSession,
) -> None:
    """No deployment channel: a workspace's own channel serves its platform only."""
    tenant = await make_tenant(db_session)
    await set_support_channel(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C9", set_by_account_id=None
    )
    settings = _settings()
    slack = await feature_in(
        db_session, settings, FeaturePlace(tenant.id, "slack"), "support_escalation"
    )
    teams = await feature_in(
        db_session, settings, FeaturePlace(tenant.id, "teams"), "support_escalation"
    )
    assert slack.on, "its own Slack channel"
    assert teams.unavailable == "requires", "no channel on Teams"

    locked = _settings({"support_own_channel": "off"})
    assert not own_channel_allowed(locked)
    held = await feature_in(
        db_session, locked, FeaturePlace(tenant.id, "slack"), "support_escalation"
    )
    assert held.unavailable == "requires", "the operator keeps every request in the default channel"


async def test_a_place_limit_gives_credits_when_everyone_has_none(
    db_session: AsyncSession,
) -> None:
    """Everyone at 0 needs a place's limit; a person's row only caps, so it is not enough."""
    tenant = await make_tenant(db_session)
    settings = _settings(slack_escalation_channel_id="C1", credits_per_user=0)
    place = FeaturePlace(tenant.id, "slack")
    assert (
        await feature_in(db_session, settings, place, "support_escalation")
    ).unavailable == "requires"

    await _limit(db_session, tenant.id, person="U1")
    person_only = await feature_in(db_session, settings, place, "support_escalation")
    assert person_only.unavailable == "requires", "a person's row adds no credits"

    await _limit(db_session, tenant.id)
    assert (await feature_in(db_session, settings, place, "support_escalation")).on


async def test_feedback_needs_a_channel_but_no_credits(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    settings = _settings(slack_escalation_channel_id="C1", credits_per_user=0)
    place = FeaturePlace(tenant.id, "slack")
    before = await feature_in(db_session, settings, place, "support_feedback")
    assert (before.on, before.unavailable) == (False, None), "off by default, yet available"

    await set_workspace_feature(
        db_session,
        settings,
        tenant_id=tenant.id,
        platform="slack",
        key="support_feedback",
        value="on",
        account_id=None,
        surface="web",
    )
    assert (await feature_in(db_session, settings, place, "support_feedback")).on
    nowhere = await feature_in(
        db_session, settings, FeaturePlace(tenant.id, "teams"), "support_feedback"
    )
    assert nowhere.unavailable == "requires"


async def test_turning_ask_a_person_on_with_only_an_own_channel_is_not_refused(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    settings = _settings()
    with pytest.raises(ChannelChangeRefused):
        await set_workspace_feature(
            db_session,
            settings,
            tenant_id=tenant.id,
            platform="slack",
            key="support_escalation",
            value="on",
            account_id=None,
            surface="web",
        )
    await set_support_channel(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C9", set_by_account_id=None
    )
    await set_workspace_feature(
        db_session,
        settings,
        tenant_id=tenant.id,
        platform="slack",
        key="support_escalation",
        value="on",
        account_id=None,
        surface="web",
    )


async def test_a_workspace_on_in_the_old_env_map_stays_on(db_session: AsyncSession) -> None:
    """`feedback_to_support` reads as the workspace tier until import-env stores it."""
    on, off = await make_tenant(db_session), await make_tenant(db_session)
    settings = _settings(
        slack_escalation_channel_id="C1", feedback_to_support={on.id: True, off.id: False}
    )
    got = await feature_in(db_session, settings, FeaturePlace(on.id, "slack"), "support_feedback")
    assert (got.on, got.tier) == (True, "workspace")
    assert not (
        await feature_in(db_session, settings, FeaturePlace(off.id, "slack"), "support_feedback")
    ).on

    assert await import_env(db_session, settings) == 2
    rows = await list_feature_values(
        db_session, tenant_id=on.id, keys=["support_feedback"], tier="workspace"
    )
    assert [row.value for row in rows] == ["on"]


async def test_feedback_goes_where_requests_go_only_while_on(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    settings = _settings(slack_escalation_channel_id="C1", feedback_to_support={tenant.id: True})
    place = FeaturePlace(tenant.id, "slack")
    default = await feedback_destination(db_session, settings, place)
    assert default is not None and (default.channel_id, default.own) == ("C1", False)
    await set_support_channel(
        db_session, tenant_id=tenant.id, platform="slack", channel_id="C9", set_by_account_id=None
    )
    own = await feedback_destination(db_session, settings, place)
    assert own is not None and (own.channel_id, own.own) == ("C9", True), "follows Requests go to"
    assert await feedback_destination(db_session, _settings(), place) is None, "off by default"
