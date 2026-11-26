"""Long jobs and cloud computers spend money in the background: only a server admin sets
them."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.admin_actions import DelegatedAction, admin_for_action
from daimon.core.config import (
    AnthropicSettings,
    ComputeSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
)
from daimon.core.features.registry import FEATURES, Tier
from daimon.core.features.service import FeatureActor, may_change
from daimon.core.features.snapshot import CACHE, FeaturePlace, resolve_features
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.testing.factories import make_account, make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_LONG_JOB_KEYS = ("long_jobs", "long_jobs_max_usd", "long_jobs_max_deadline", "compute")
_TIERS: tuple[Tier, ...] = ("workspace", "agent")
#: A deployment with cloud computers set up, so only the tiers decide.
_SET_UP = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "discord": DiscordSettings(bot_token=SecretStr("test")),
        "compute": ComputeSettings(
            enabled=True,
            deployment_id="d",
            cloud_run_project="p",
            cloud_run_service_account="s@p.iam.gserviceaccount.com",
        ),
    }
)


def _actor(
    *, server_admin: bool, delegated_admin: bool = False, owner: bool = False
) -> FeatureActor:
    """An actor whose admin answer is core's `admin_for_action`, as chat and MCP give it."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return admin_for_action(
            platform_admin=server_admin, daimon_admin=server_admin or delegated_admin, action=action
        )

    async def owns_agent(_: str) -> bool:
        return owner

    return FeatureActor(uuid.uuid4(), None, "chat", None, admin_for, owns_agent=owns_agent)


@pytest.mark.parametrize("key", _LONG_JOB_KEYS)
@pytest.mark.parametrize("tier", _TIERS)
async def test_an_owner_and_a_delegated_admin_cannot_change_a_long_job_feature(
    key: str, tier: Tier
) -> None:
    spec = FEATURES[key]
    owner = _actor(server_admin=False, owner=True)
    delegated = _actor(server_admin=False, delegated_admin=True)
    assert not await may_change(owner, spec, tier, locked=False, agent_id="ag"), (
        "an agent's owner must not raise a long job cap or turn long jobs or computers on"
    )
    assert not await may_change(delegated, spec, tier, locked=False, agent_id="ag"), (
        "a delegated admin is not a server admin"
    )


@pytest.mark.parametrize("key", _LONG_JOB_KEYS)
@pytest.mark.parametrize("tier", _TIERS)
async def test_a_server_admin_changes_a_long_job_feature(key: str, tier: Tier) -> None:
    actor = _actor(server_admin=True)
    assert await may_change(actor, FEATURES[key], tier, locked=False, agent_id="ag"), (
        "a server admin keeps the right on both tiers"
    )


async def test_an_agent_owner_still_sets_an_ordinary_agent_feature() -> None:
    owner = _actor(server_admin=False, owner=True)
    assert await may_change(owner, FEATURES["routines"], "agent", locked=False, agent_id="ag"), (
        "only the spending features drop the owner path"
    )


async def test_an_owners_choice_stored_before_the_default_changed_keeps_its_value(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Cloud computers became Off by default and server-admin only. A value an agent's owner
    stored before stays: an On keeps that agent on, an Off stays off, the owner can no
    longer change either, and a deployment On does not lift the narrower Off."""
    tenant = await make_tenant(db_session)
    owner = await make_account(db_session, tenant=tenant)
    for agent, value in (("ag_on", "on"), ("ag_off", "off")):
        await set_feature_value(
            db_session,
            tenant_id=tenant.id,
            target=FeatureTarget("agent", agent_id=agent),
            key="compute",
            value=value,
            set_by=owner.id,
            surface="chat",
        )
    await db_session.commit()

    async def found(agent: str, settings: Settings) -> tuple[str, str]:
        CACHE.clear()
        place = FeaturePlace(tenant.id, "discord", None, agent_id=agent)
        snapshot = await resolve_features(db_session_factory, settings, place, keys=("compute",))
        resolved = snapshot.get("compute")
        return resolved.value, resolved.tier

    deployment_on = _SET_UP.model_copy(update={"features": {"compute": "on"}})
    assert await found("ag_on", _SET_UP) == ("on", "agent"), "the owner's On is kept"
    assert await found("ag_off", _SET_UP) == ("off", "agent"), "the owner's Off is kept"
    assert await found("ag_other", _SET_UP) == ("off", "default"), "nothing stored: Off"
    assert (await found("ag_off", deployment_on))[0] == "off", "a deployment On is not a lift"
    assert (await found("ag_other", deployment_on))[0] == "on"
    who = _actor(server_admin=False, owner=True)
    assert not await may_change(who, FEATURES["compute"], "agent", locked=False, agent_id="ag_on")
