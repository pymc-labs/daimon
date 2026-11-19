"""Participation and progress plans resolve through the feature resolver at runtime."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from daimon.core.config import McpSettings, ThreadParticipationSettings
from daimon.core.features.snapshot import CACHE
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.participation_gates import ParticipationGates
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import FeatureTarget, TenantRow
from daimon.core.stores.feature_values import clear_feature_value, set_feature_value
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.admission import admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.progress_plans import guidance_block, progress_plans_prefix
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.factories import make_ledger_entry, make_tenant, make_tenant_config
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CHANNEL, THREAD, AGENT = "chan-1", "thr-1", "ag_1"


def _deps(tenant: TenantRow, sessions: async_sessionmaker[AsyncSession], root: Path) -> TurnDeps:
    router = resolved_agent_env_router(
        ma_agent(id=AGENT, name="daimon", tenant_id=tenant.id),
        ma_environment(id="env_1", name="default", tenant_id=tenant.id),
    )
    return TurnDeps(
        anthropic=build_fake_anthropic(router.dispatch),
        sessionmaker=sessions,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=root,
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1.0"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        tool_safety=OPEN_TOOL_SAFETY,
    )


async def _set(
    session: AsyncSession,
    tenant: TenantRow,
    key: str,
    target: FeatureTarget,
    value: str,
    *,
    locked: bool = False,
) -> None:
    await set_feature_value(
        session,
        tenant_id=tenant.id,
        target=target,
        key=key,
        value=value,
        locked=locked,
        set_by=None,
        surface="web",
    )


async def test_a_locked_workspace_and_the_agent_tier_hold_on_the_runtime_paths(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant = await make_tenant(db_session)
    await make_tenant_config(
        db_session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    thread = FeatureTarget("thread", channel_id=CHANNEL, thread_key=THREAD)
    for key in ("participation", "progress_plans"):
        await _set(db_session, tenant, key, FeatureTarget("workspace"), "on", locked=True)
        await _set(db_session, tenant, key, thread, "off")
    await db_session.commit()
    CACHE.clear()
    deps = _deps(tenant, db_session_factory, tmp_path)
    gates = ParticipationGates(
        platform="discord",
        settings=ThreadParticipationSettings(),
        sessionmaker=db_session_factory,
        anthropic=MagicMock(),
        bot_display_name="daimon",
        markup=Decimal(1),
    )

    async def plans() -> str:
        admission = await admit(
            deps,
            origin="chat",
            tenant_id=tenant.id,
            requester_org=RequesterOrg.HOST,
            platform="discord",
            external_user_id="user-1",
            channel_id=CHANNEL,
            thread_id=THREAD,
            now=datetime.now(UTC),
        )
        resolved = admission.resolved("progress_plans")
        return progress_plans_prefix(resolved, platform="discord", chat_rendered=True)

    participation = await gates.resolve(tenant_id=tenant.id, channel_id=CHANNEL, thread_id=THREAD)
    assert (participation.on, participation.tier) == (True, "workspace"), (
        "the lock beats the thread"
    )
    assert await plans() == guidance_block(), "the lock beats the thread"

    await clear_feature_value(db_session, tenant_id=tenant.id, target=thread, key="progress_plans")
    await _set(db_session, tenant, "progress_plans", FeatureTarget("workspace"), "on")
    await _set(db_session, tenant, "progress_plans", FeatureTarget("agent", agent_id=AGENT), "off")
    await db_session.commit()
    CACHE.clear()
    assert await plans() == "", "Off at the agent tier beats an unlocked workspace On"


async def test_reach_limits_follow_the_reach_check_through_the_real_readers(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A limit's dependency resolves before it, whatever the registry order, in a read of
    every feature, of the limits alone and of the settings panel's views. Only the
    deployment can turn the reach check Off."""
    from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
    from daimon.core.features.registry import FEATURES
    from daimon.core.features.service import read_features
    from daimon.core.features.snapshot import FeaturePlace, resolve_features

    limits = ("outbound_threads", "outbound_places", "outbound_people")
    assert all(FEATURES[key].depends_on == ("reach_budget",) for key in limits), (
        "the limits this test covers depend on the reach check"
    )
    settings = Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "features": {"reach_budget": "off"},
        }
    )
    tenant = await make_tenant(db_session)
    CACHE.clear()
    place = FeaturePlace(tenant.id, "discord", channel_id=CHANNEL)

    every = await resolve_features(db_session_factory, settings, place)
    alone = await resolve_features(db_session_factory, settings, place, keys=limits)
    settings_views = {
        view.spec.key: view.resolved
        for view in await read_features(db_session_factory, settings, place, keys=limits)
    }
    for name, found in (
        ("all", every.features),
        ("alone", alone.features),
        ("views", settings_views),
    ):
        for key in limits:
            assert found[key].unavailable == "depends_on", f"{key} read as {name}"
    assert set(alone.features) == set(limits), "a read returns only the keys asked for"
