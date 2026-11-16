"""`watch_approval`: skip drops the make and resume watch cards; the rest keep asking."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.confirmation import confirmed_tools_for_tenant, watch_card_skipped
from daimon.core.features.registry import FEATURES
from daimon.core.features.service import FeatureActor, change_feature, change_refusal
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.tool_safety import (
    WATCH_APPROVAL_TOOLS,
    WATCH_CONFIRMED_TOOLS,
    ToolSafetyPolicy,
)
from daimon.core.turn.deps import TurnDeps
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

AGENT = "agent_watch"
STILL_ASK = WATCH_CONFIRMED_TOOLS - WATCH_APPROVAL_TOOLS


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
        }
    )


def _deps(sessions: async_sessionmaker[AsyncSession]) -> TurnDeps:
    return cast(
        TurnDeps,
        SimpleNamespace(
            backend_selection_enabled=False,
            microsoft_send=False,
            tool_safety=ToolSafetyPolicy(),
            public_url=None,
            sessionmaker=sessions,
        ),
    )


async def _admin(_: object) -> bool:
    return True


async def _not_admin(_: object) -> bool:
    return False


async def _set(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, tier: str, value: str
) -> None:
    actor = FeatureActor(tenant_id, None, "web", None, _admin)
    place = FeaturePlace(tenant_id, "discord", None, agent_id=AGENT)
    await change_feature(
        sessions, _settings(), actor, key="watch_approval", tier=tier,  # type: ignore[arg-type]
        value=value, place=place,
    )  # fmt: skip


def test_the_setting_defaults_to_ask_at_the_workspace_and_agent_tiers() -> None:
    spec = FEATURES["watch_approval"]
    assert spec.default == "ask", "the card stays unless an admin skips it"
    assert spec.tiers == ("workspace", "agent"), "no channel, thread or deployment value"
    assert list(spec.values)[-1] == "ask", "an unreadable setting applies the strict value"


async def test_ask_keeps_every_watch_card(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    got = await confirmed_tools_for_tenant(_deps(db_session_factory), tenant.id, AGENT)
    assert got >= WATCH_CONFIRMED_TOOLS, "ask is unchanged: all watch and job tools ask"


@pytest.mark.parametrize("tier", ["workspace", "agent"])
async def test_skip_drops_only_make_and_resume(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tier: str
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _set(db_session_factory, tenant.id, tier, "skip")
    got = await confirmed_tools_for_tenant(_deps(db_session_factory), tenant.id, AGENT)
    assert not got & WATCH_APPROVAL_TOOLS, "create and resume run without a card"
    assert got >= STILL_ASK, "key rotation, long jobs and Fly keep asking"
    assert {"start_long_job", "rotate_watch_key", "fly_launch_session"} <= got
    assert "update_watch" in got, "editing a watch keeps its card, as it can change anything"


async def test_skip_for_one_agent_leaves_another_asking(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    await _set(db_session_factory, tenant.id, "agent", "skip")
    other = await confirmed_tools_for_tenant(_deps(db_session_factory), tenant.id, "agent_other")
    assert other >= WATCH_CONFIRMED_TOOLS, "the agent tier is per agent"
    assert await watch_card_skipped(db_session_factory, tenant.id, AGENT)
    assert not await watch_card_skipped(db_session_factory, tenant.id, None), "no agent: ask"


async def test_only_a_server_admin_may_set_it(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    actor = FeatureActor(tenant.id, None, "web", None, _not_admin, owns_agent=_admin)
    place = FeaturePlace(tenant.id, "discord", None, agent_id=AGENT)
    refused = await change_refusal(
        db_session_factory, _settings(), actor, key="watch_approval", tier="agent",
        place=place, value="skip",
    )  # fmt: skip
    assert refused is not None, "an agent owner who is not a server admin cannot skip the card"


async def test_an_unreadable_setting_keeps_the_card() -> None:
    class Broken:
        def __call__(self) -> object:
            raise RuntimeError("down")

    assert not await watch_card_skipped(Broken(), uuid.uuid4(), AGENT)  # pyright: ignore[reportArgumentType]
