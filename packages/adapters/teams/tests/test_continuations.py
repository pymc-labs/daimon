"""Teams continuations: work a handoff queued runs after the turn, as the requester."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams import app as app_module
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.commands import fresh_start
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.continuity.continuation import ContinuationRequest, record_continuation
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn.admission import AdmissionDenied
from daimon.testing.factories import make_account
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    FakeSender,
    build_teams_runtime,
)

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
_TARGET = "agt_stats"


async def _bot_token() -> str:
    return "bot-token"


def _inbound(text: str) -> TeamsInbound:
    return TeamsInbound(
        kind="dm",
        entra_tenant_id=ENTRA_TENANT_ID,
        user_id=AAD_OBJECT_ID,
        conversation_id=CONVERSATION_ID,
        channel_id=CONVERSATION_ID,
        activity_id=str(uuid.uuid4()),
        text=text,
        service_url=SERVICE_URL,
    )


async def _app(
    db: async_sessionmaker[AsyncSession], sender: FakeSender
) -> tuple[TeamsApp, uuid.UUID]:
    """The app over a live tenant whose MA knows the handoff target, and the requester."""
    await provision_tenant(db, platform="teams", workspace_id=ENTRA_TENANT_ID)
    async with db() as session:
        tenant = await get_tenant(session, TENANT)
        assert tenant is not None
        account = await make_account(session, tenant=tenant)
        await session.commit()
    router = MARouter()
    router.add_agent(
        ma_agent(
            id=_TARGET, name="stats-bot", model="m", tenant_id=TENANT, created_at=datetime.now(UTC)
        )
    )
    runtime = build_teams_runtime(db, anthropic=build_fake_anthropic(router.dispatch))
    teams = TeamsApp(
        runtime=runtime, sender=sender, commands={"new": fresh_start}, bot_token=_bot_token
    )
    return teams, account.id


async def _hand_off(db: async_sessionmaker[AsyncSession], account_id: uuid.UUID) -> uuid.UUID:
    """What hand_off_task records mid-turn."""
    key = uuid.uuid4()
    await record_continuation(
        db,
        ContinuationRequest(
            tenant_id=TENANT,
            platform="teams",
            parent_channel_id=CONVERSATION_ID,
            thread_id=CONVERSATION_ID,
            requester_account_id=account_id,
            requester_external_user_id=AAD_OBJECT_ID,
            target_ma_agent_id=_TARGET,
            target_name="stats-bot",
            requested_work="finish the writeup",
            reason="task_handoff",
            idempotency_key=key,
        ),
    )
    return key


async def _status(db: async_sessionmaker[AsyncSession], key: uuid.UUID) -> tuple[str, str | None]:
    async with db() as session:
        row = await get_continuation(session, idempotency_key=key)
    assert row is not None
    return row.status, row.skip_reason


@pytest.mark.asyncio
async def test_a_handoff_runs_after_the_turn_as_the_requester(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams, account_id = await _app(db_session_factory, FakeSender())
    calls: list[tuple[TeamsInbound, dict[str, Any]]] = []
    keys: list[uuid.UUID] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID, **kw: Any) -> None:
        calls.append((inbound, kw))
        if len(calls) == 1:
            keys.append(await _hand_off(db_session_factory, account_id))

    with patch.object(TeamsApp, "_run_turn", _turn):
        await teams._orchestrate(_inbound("hand this to stats-bot"), TENANT)

    (_, _), (follow, kw) = calls
    assert (follow.text, follow.user_id, follow.conversation_id) == (
        "finish the writeup",
        AAD_OBJECT_ID,
        CONVERSATION_ID,
    )
    assert kw["reraise"] is True and kw["handoff"] is not None
    assert await _status(db_session_factory, keys[0]) == ("delivered", None)


@pytest.mark.asyncio
async def test_a_newer_message_supersedes_the_queued_work(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    teams, account_id = await _app(db_session_factory, FakeSender())
    ran: list[str] = []
    keys: list[uuid.UUID] = []

    async def _turn(self: TeamsApp, inbound: TeamsInbound, tenant_id: uuid.UUID, **kw: Any) -> None:
        ran.append(inbound.text)
        if len(ran) == 1:
            keys.append(await _hand_off(db_session_factory, account_id))
            await teams._orchestrate(_inbound("actually, stop"), TENANT)

    with patch.object(TeamsApp, "_run_turn", _turn):
        await teams._orchestrate(_inbound("hand this to stats-bot"), TENANT)

    assert ran == ["hand this to stats-bot", "actually, stop"], "the stale work never runs"
    assert await _status(db_session_factory, keys[0]) == ("skipped", "skip_superseded")


@pytest.mark.asyncio
async def test_a_refused_continuation_raises_after_telling_the_person(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender = FakeSender()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={"new": fresh_start},
        bot_token=_bot_token,
    )
    denied = AsyncMock(side_effect=AdmissionDenied(reason="balance_depleted"))
    with patch.object(app_module, "admit", denied), pytest.raises(AdmissionDenied):
        await teams._run_turn(_inbound("w"), TENANT, reraise=True)
    assert [a.text for a in sender.activities] == [app_module._BALANCE_DEPLETED]
