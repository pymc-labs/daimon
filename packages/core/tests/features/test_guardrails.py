"""Guardrails combine stricter-wins: below the deployment a tier can tighten, never loosen."""

from __future__ import annotations

import uuid

import pytest
from daimon.core import headless_runner
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    OutboundSettings,
    Settings,
)
from daimon.core.features.deployment import deployment_value
from daimon.core.features.registry import FEATURES, Tier
from daimon.core.features.resolver import TierValue, resolve_feature
from daimon.core.features.service import FeatureActor, change_feature, change_refusal
from daimon.core.features.snapshot import (
    FeaturePlace,
    feature_in,
    feature_in_or_none,
    place_feature_or_none,
    place_guardrail,
)
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUARDRAILS = [key for key, spec in FEATURES.items() if spec.combine == "stricter"]


def _settings(**extra: object) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            **extra,
        }
    )


@pytest.mark.parametrize("key", GUARDRAILS)
def test_a_lower_tier_never_loosens_a_guardrail(key: str) -> None:
    spec = FEATURES[key]
    loose, strict = list(spec.values)[0], list(spec.values)[-1]
    lower: list[Tier] = [t for t in spec.tiers if t != "deployment"]
    for tier in lower:
        got = resolve_feature(spec, [TierValue("deployment", strict), TierValue(tier, loose)])
        assert (got.value, got.tier) == (strict, "deployment"), f"{tier} loosened {key}"
    if spec.default == strict and lower:
        got = resolve_feature(spec, [TierValue(lower[0], loose)])
        assert got.value == strict, "the default is the deployment's floor"
    if lower:
        got = resolve_feature(spec, [TierValue("deployment", loose), TierValue(lower[-1], strict)])
        assert (got.value, got.tier) == (strict, lower[-1]), "a lower tier tightens"


def test_new_guardrails_keep_the_old_variables_as_deployment_values() -> None:
    old = _settings(
        outbound=OutboundSettings(reach_budget_enabled=False),
        mcp=McpSettings(require_routine_destination=True),
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    assert deployment_value(old, FEATURES["reach_budget"]) == TierValue("deployment", "off")
    assert deployment_value(old, FEATURES["require_routine_destination"]) == TierValue(
        "deployment", "on"
    )
    assert deployment_value(old, FEATURES["tool_safety"]) == TierValue("deployment", "on")
    for key in ("reach_budget", "require_routine_destination", "tool_safety"):
        assert deployment_value(_settings(), FEATURES[key]) is None, "unset reads the default"


def test_the_tool_safety_feature_sets_the_policy_every_reader_holds() -> None:
    assert _settings().tool_safety.enabled is False
    assert _settings(features={"tool_safety": "on"}).tool_safety.enabled is True
    off = _settings(tool_safety=ToolSafetyPolicy(enabled=True), features={"TOOL_SAFETY": "off"})
    assert off.tool_safety.enabled is False, "the feature wins over the old variable"


async def _admin(_: object) -> bool:
    return True


async def test_a_looser_write_below_is_refused_before_and_at_the_write(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "discord", "C1", agent_id="agent_1")
    early = await change_refusal(
        db_session_factory, settings, actor, key="reach_budget", tier="channel", place=place,
        value="off",
    )  # fmt: skip
    assert early is not None and early.reason == "looser_than_above"
    assert "can only make it stricter" in str(early) and "Ask" in str(early)
    with pytest.raises(ChannelChangeRefused, match="can only make it stricter"):
        await change_feature(
            db_session_factory, settings, actor, key="reach_budget", tier="workspace",
            value="off", place=place,
        )  # fmt: skip
    await change_feature(
        db_session_factory, settings, actor, key="require_routine_destination",
        tier="workspace", value="on", place=place,
    )  # fmt: skip
    found = await feature_in(db_session, settings, place, "require_routine_destination")
    assert (found.value, found.tier) == ("on", "workspace"), "tightening is allowed"


async def test_an_unreadable_guardrail_applies_its_strictest_value() -> None:
    class Broken:
        def __call__(self) -> object:
            raise RuntimeError("down")

    place = FeaturePlace.of(uuid.uuid4(), "slack", None)
    found = await place_guardrail(Broken(), None, place, "unattended_capabilities")  # pyright: ignore[reportArgumentType]
    assert found.value == "off"


async def test_a_background_read_that_fails_returns_none_not_a_default(
    db_session: AsyncSession,
) -> None:
    """Workers skip the item on None; only the turn path may guess a registry default."""

    class Broken:
        def __call__(self) -> object:
            raise RuntimeError("down")

    place = FeaturePlace.of(uuid.uuid4(), "slack", None)
    got = await place_feature_or_none(Broken(), None, place, "routines")  # pyright: ignore[reportArgumentType]
    assert got is None, "an unreadable place is not read as the registry default"
    healthy = await feature_in_or_none(db_session, None, place, "routines")
    assert healthy is not None, "a readable place resolves normally"
    assert await feature_in_or_none(db_session, None, place, "no_such_feature") is None
    assert (await db_session.execute(text("select 1"))).scalar_one() == 1, "session still usable"


async def test_an_opted_in_routine_loses_its_builtins_when_the_guardrail_is_off(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    settings = _settings()
    allowed = headless_runner._routine_builtins_allowed  # pyright: ignore[reportPrivateUsage]
    assert await allowed(None, settings, tenant.id, "slack", "agent_1"), "no store: as before"
    assert await allowed(db_session_factory, settings, tenant.id, "slack", "agent_1")
    actor = FeatureActor(tenant.id, None, "web", None, _admin)
    place = FeaturePlace(tenant.id, "slack", None, agent_id="agent_1")
    await change_feature(
        db_session_factory, settings, actor, key="unattended_capabilities",
        tier="workspace", value="off", place=place,
    )  # fmt: skip
    assert not await allowed(db_session_factory, settings, tenant.id, "slack", "agent_1")
