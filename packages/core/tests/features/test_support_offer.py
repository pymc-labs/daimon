"""A button, reaction or form re-checks "ask a person" at the agent that wrote the answer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.agent_posts import record_post
from daimon.core.stores.domain import FeatureTarget
from daimon.core.stores.feature_values import set_feature_value
from daimon.core.support_offer import support_offered
from daimon.testing.factories import make_tenant, make_thread_session
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import update
from sqlalchemy.exc import OperationalError
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


async def _value(
    session: AsyncSession, tenant_id: uuid.UUID, value: str, *, agent: str | None = None
) -> None:
    """`support_escalation` set at the workspace, or at `agent`'s own tier when given."""
    await set_feature_value(
        session,
        tenant_id=tenant_id,
        target=FeatureTarget(tier="agent", agent_id=agent) if agent else FeatureTarget("workspace"),
        key=KEY,
        value=value,
        set_by=None,
        surface="web",
    )
    await session.commit()


async def _offered(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    thread: str = THREAD,
    message_id: str = "m_unrecorded",
) -> bool:
    return await support_offered(
        factory,
        _settings(),
        tenant_id=tenant_id,
        platform="discord",
        thread_id=thread,
        post=(thread, message_id),
    )


async def _answer(
    db_session: AsyncSession, tenant_id: uuid.UUID, agent: str, message_id: str
) -> None:
    await record_post(
        db_session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=THREAD,
        message_id=message_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent),
    )
    await db_session.commit()


async def test_an_agent_set_to_off_is_not_offered_while_another_agent_still_is(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await make_thread_session(db_session, tenant=tenant, thread_id=THREAD, ma_agent_id="agent_off")
    await make_thread_session(db_session, tenant=tenant, thread_id="t2", ma_agent_id="agent_other")
    await _value(db_session, tenant.id, "off", agent="agent_off")
    assert not await _offered(db_session_factory, tenant.id), "its own agent is off"
    assert await _offered(db_session_factory, tenant.id, "t2"), "another agent keeps it"


async def test_a_thread_without_a_session_uses_the_workspace_value(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    assert await _offered(db_session_factory, tenant.id), "on by default"
    await _value(db_session, tenant.id, "off")
    assert not await _offered(db_session_factory, tenant.id), "the workspace decides"


async def test_an_agent_cannot_turn_it_on_above_an_off_workspace(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await make_thread_session(db_session, tenant=tenant, thread_id=THREAD, ma_agent_id="agent_1")
    await _value(db_session, tenant.id, "off")

    async def admin_for(_: DelegatedAction | None) -> bool:
        return True

    with pytest.raises(ChannelChangeRefused):
        await change_feature(
            db_session_factory,
            _settings(),
            FeatureActor(tenant.id, None, "web", None, admin_for),
            key=KEY,
            tier="agent",
            value="on",
            place=FeaturePlace.of(tenant.id, "discord", None, agent_id="agent_1"),
        )
    assert not await _offered(db_session_factory, tenant.id), "off above is a ceiling"


async def test_a_failed_lookup_or_read_refuses(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await db_session.commit()
    assert await _offered(db_session_factory, tenant.id), "on by default"

    async def boom(*_a: object, **_k: object) -> None:
        raise OperationalError("select", {}, Exception("down"))

    with monkeypatch.context() as patch:
        patch.setattr("daimon.core.support_offer.get_post", boom)
        assert not await _offered(db_session_factory, tenant.id), "lookup failed"
    monkeypatch.setattr("daimon.core.features.snapshot.resolve_features", boom)
    assert not await _offered(db_session_factory, tenant.id), "the read failed"


async def test_the_agent_that_wrote_the_answer_decides_in_a_two_agent_thread(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    now = datetime.now(UTC)
    for age, agent in ((2, "agent_off"), (1, "agent_on")):
        await make_thread_session(
            db_session,
            tenant=tenant,
            thread_id=THREAD,
            ma_agent_id=agent,
            created_at=now - timedelta(minutes=age),
        )
    await _answer(db_session, tenant.id, "agent_off", "m_off")
    await _value(db_session, tenant.id, "off", agent="agent_off")
    assert not await _offered(db_session_factory, tenant.id, message_id="m_off"), (
        "the answer's own agent is off, though another is newer"
    )
    assert await _offered(db_session_factory, tenant.id), (
        "an answer with no record falls back to the newest agent"
    )


async def test_an_answer_outside_the_threads_sessions_still_names_its_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    await _answer(db_session, tenant.id, "agent_off", "m_off")
    await _answer(db_session, tenant.id, "agent_plain", "m_plain")
    await _value(db_session, tenant.id, "off", agent="agent_off")
    assert not await _offered(db_session_factory, tenant.id, message_id="m_off"), "own value"
    assert await _offered(db_session_factory, tenant.id, message_id="m_plain"), "workspace"
    await _value(db_session, tenant.id, "off")
    assert not await _offered(db_session_factory, tenant.id, message_id="m_plain"), "workspace off"


@pytest.mark.parametrize("status", ["dead", "retired"])
async def test_a_thread_with_only_ended_sessions_still_names_its_agent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="g1")
    row = await make_thread_session(
        db_session, tenant=tenant, thread_id=THREAD, ma_agent_id="agent_off"
    )
    await db_session.execute(
        update(ThreadSession).where(ThreadSession.id == row.id).values(status=status)
    )
    await _value(db_session, tenant.id, "off", agent="agent_off")
    assert not await _offered(db_session_factory, tenant.id), f"a {status} session names it"
