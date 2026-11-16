"""A button, reaction or form re-checks "ask a person" at the conversation's agent."""

from __future__ import annotations

import contextlib
import uuid

import pytest
from daimon.core._models import ThreadSession
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    SupportSettings,
)
from daimon.core.features.registry import Tier
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_posts import record_post
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.support_offer import support_offered
from daimon.testing.factories import make_account, make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "support_escalation"
THREAD = "t1"


def _settings() -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "support": SupportSettings(escalation_channel_id="999"),
        }
    )


def _admin(tenant_id: uuid.UUID) -> FeatureActor:
    async def admin_for(_: DelegatedAction | None) -> bool:
        return True

    return FeatureActor(tenant_id, None, "web", None, admin_for)


async def _thread(
    db_session: AsyncSession, tenant_id: uuid.UUID, account_id: uuid.UUID, *, agent: str | None
) -> None:
    await create_thread_session(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        thread_id=THREAD,
        account_id=account_id,
        ma_session_id="sess_1",
        ma_agent_id=agent,
    )
    await db_session.commit()


async def _set(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    tier: Tier,
    value: str,
    agent: str | None = None,
) -> None:
    place = FeaturePlace.of(tenant_id, "discord", None, agent_id=agent)
    await change_feature(
        factory, _settings(), _admin(tenant_id), key=KEY, tier=tier, value=value, place=place
    )  # fmt: skip


async def _offered(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, thread: str | None
) -> bool:
    return await support_offered(
        factory, _settings(), tenant_id=tenant_id, platform="discord", thread_id=thread
    )


async def test_an_agent_set_to_off_is_not_offered_while_another_agent_still_is(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    account = await make_account(db_session, tenant=tenant)
    await _thread(db_session, tenant.id, account.id, agent="agent_off")
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="t2",
        account_id=account.id,
        ma_session_id="sess_2",
        ma_agent_id="agent_client",
    )
    await db_session.commit()
    await _set(db_session_factory, tenant.id, tier="agent", value="off", agent="agent_off")
    assert not await _offered(db_session_factory, tenant.id, THREAD)
    assert await _offered(db_session_factory, tenant.id, "t2"), "a client agent keeps it"


async def test_a_thread_without_a_session_uses_the_workspace_value(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    assert await _offered(db_session_factory, tenant.id, "none")
    assert await _offered(db_session_factory, tenant.id, None)
    await _set(db_session_factory, tenant.id, tier="workspace", value="off")
    assert not await _offered(db_session_factory, tenant.id, "none")


async def test_an_agent_cannot_turn_it_on_above_an_off_workspace(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    account = await make_account(db_session, tenant=tenant)
    await _thread(db_session, tenant.id, account.id, agent="agent_1")
    await _set(db_session_factory, tenant.id, tier="workspace", value="off")
    with contextlib.suppress(ChannelChangeRefused):  # a refused write is as good as ignored
        await _set(db_session_factory, tenant.id, tier="agent", value="on", agent="agent_1")
    assert not await _offered(db_session_factory, tenant.id, THREAD)


async def test_a_failed_session_lookup_refuses(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()

    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("down")

    monkeypatch.setattr("daimon.core.support_offer.list_thread_agents", boom)
    assert not await _offered(db_session_factory, tenant.id, THREAD)


async def test_the_agent_that_wrote_the_answer_decides_in_a_two_agent_thread(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    account = await make_account(db_session, tenant=tenant)
    for n, agent in enumerate(("agent_off", "agent_on")):
        await create_thread_session(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=THREAD,
            account_id=account.id,
            ma_session_id=f"sess_{n}",
            ma_agent_id=agent,
        )
        await db_session.commit()  # one transaction would stamp both rows alike
    await record_post(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id=THREAD,
        message_id="m_off",
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_off"),
    )
    await db_session.commit()
    await _set(db_session_factory, tenant.id, tier="agent", value="off", agent="agent_off")

    async def at(message_id: str | None) -> bool:
        return await support_offered(
            db_session_factory,
            _settings(),
            tenant_id=tenant.id,
            platform="discord",
            thread_id=THREAD,
            post=(THREAD, message_id) if message_id else None,
        )

    assert not await at("m_off"), "the answer's own agent is off, though another is newer"
    assert await at(None), "without the answer, the newest agent decides"


async def test_a_thread_with_only_ended_sessions_still_names_its_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    account = await make_account(db_session, tenant=tenant)
    for status in ("dead", "retired"):
        await _thread(db_session, tenant.id, account.id, agent="agent_off")
        await db_session.execute(
            update(ThreadSession).where(ThreadSession.thread_id == THREAD).values(status=status)
        )
        await db_session.commit()
        await _set(db_session_factory, tenant.id, tier="agent", value="off", agent="agent_off")
        assert not await _offered(db_session_factory, tenant.id, THREAD), status
        await db_session.execute(delete(ThreadSession).where(ThreadSession.thread_id == THREAD))
        await db_session.commit()
