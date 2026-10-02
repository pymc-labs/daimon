"""Teams organic thread participation: the cascade keys, the batch and the silent failures.

The gates are the shared ones over a real DB; the classifier is the real call
over a fake Messages API, so only the transport is faked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import Coroutine
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from anthropic.types import Message, TextBlock, Usage
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.participation import TeamsParticipation
from daimon.adapters.teams.thread_reader import ThreadReader
from daimon.core.config import ThreadParticipationSettings
from daimon.core.participation_gates import ParticipationGates
from daimon.core.stores import tenant_ledger
from daimon.core.stores.thread_participation import set_participation_mode
from daimon.core.teams_graph import GraphUnavailable
from daimon.core.thread_participation import (
    ClassifierMessage,
    ParticipationMode,
    ParticipationScope,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import CHANNEL_ID, OTHER_AAD_OBJECT_ID, THREAD_ID, make_inbound


class _Reader:
    """A `ThreadReader` stand-in: the window it returns, or the error it raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[frozenset[str]] = []

    async def read_window(
        self, inbound: TeamsInbound, *, exclude_ids: frozenset[str], limit: int
    ) -> list[ClassifierMessage]:
        self.calls.append(exclude_ids)
        if self.error is not None:
            raise self.error
        return [ClassifierMessage(author_name="Grace", content="when do we ship?", is_bot=False)]


class _Rig:
    """One `TeamsParticipation` over real gates, recording spawns, fires and prompts."""

    def __init__(
        self,
        db: async_sessionmaker[AsyncSession],
        *,
        decision: str = "respond",
        reader: _Reader | None = None,
        quiet_seconds: float = 30.0,
    ) -> None:
        self.prompts: list[str] = []
        self.fired: list[TeamsInbound] = []
        self.timers: list[asyncio.Task[None]] = []

        def _messages(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/messages"
            self.prompts.append(json.loads(request.content)["messages"][0]["content"])
            text = json.dumps({"decision": decision, "reason": "x"})
            return httpx.Response(200, json=_classifier_reply(text))

        settings = ThreadParticipationSettings(quiet_seconds=quiet_seconds)
        gates = ParticipationGates(
            platform="teams",
            settings=settings,
            sessionmaker=db,
            anthropic=build_fake_anthropic(_messages),
            bot_display_name="daimon",
            billing_config=None,
            markup=Decimal("1.0"),
        )

        def _spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
            task = asyncio.create_task(coro, name=name)
            self.timers.append(task)
            return task

        async def _fire(trigger: TeamsInbound, tenant_id: uuid.UUID) -> None:
            self.fired.append(trigger)

        self.reader = reader if reader is not None else _Reader()
        self.participation = TeamsParticipation(
            gates=gates,
            settings=settings,
            reader=cast(ThreadReader, self.reader),
            spawn=_spawn,
            fire=_fire,
            is_busy=lambda _key: False,
        )

    async def observe(self, inbound: TeamsInbound, tenant_id: uuid.UUID, live: bool = True) -> int:
        """Observe `inbound`; how many liveness reads it cost."""
        reads = 0

        async def _is_live() -> bool:
            nonlocal reads
            reads += 1
            return live

        await self.participation.observe(inbound, tenant_id, is_live=_is_live)
        return reads


def _classifier_reply(text: str) -> dict[str, Any]:
    return Message(
        id="msg_classifier",
        type="message",
        role="assistant",
        model="claude-haiku-4-5",
        content=[TextBlock(type="text", text=text)],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage(
            input_tokens=120,
            output_tokens=9,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    ).model_dump(mode="json")


def _reply(text: str, *, user: str | None = None, name: str = "Ada") -> TeamsInbound:
    inbound = make_inbound(text, conversation=THREAD_ID, kind="channel", **_user(user))
    return dataclasses.replace(inbound, channel_id=CHANNEL_ID, unprompted=True, user_name=name)


def _user(user: str | None) -> dict[str, str]:
    return {} if user is None else {"user": user}


async def _funded_tenant(session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(session, platform="teams")
    await tenant_ledger.insert_entry(
        session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    return tenant.id


async def _follow(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    scope: ParticipationScope,
    scope_id: str | None,
    *,
    platform: str = "teams",
) -> None:
    await set_participation_mode(
        session,
        tenant_id=tenant_id,
        platform=platform,
        scope=scope,
        scope_id=scope_id,
        mode=ParticipationMode.ON,
    )
    await session.commit()


async def test_an_unfollowed_thread_costs_one_read_and_nothing_else(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await db_session.commit()
    rig = _Rig(db_session_factory)

    assert await rig.observe(_reply("anyone?"), tenant_id) == 0, "no liveness read"
    assert rig.timers == [], "no batch, no timer"


@pytest.mark.parametrize(
    ("scope", "scope_id"),
    [
        (ParticipationScope.THREAD, THREAD_ID),
        (ParticipationScope.CHANNEL, CHANNEL_ID),
        (ParticipationScope.WORKSPACE, None),
    ],
)
async def test_each_cascade_tier_keys_on_the_teams_ids(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    scope: ParticipationScope,
    scope_id: str | None,
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await _follow(db_session, tenant_id, scope, scope_id)
    rig = _Rig(db_session_factory)

    assert await rig.observe(_reply("anyone?"), tenant_id) == 1
    assert len(rig.timers) == 1, f"a {scope.value} row follows the thread"
    rig.participation.cancel_all()


async def test_a_discord_row_does_not_follow_a_teams_thread(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await _follow(db_session, tenant_id, ParticipationScope.WORKSPACE, None, platform="discord")
    rig = _Rig(db_session_factory)

    await rig.observe(_reply("anyone?"), tenant_id)
    assert rig.timers == []


async def test_a_burst_restarts_the_timer_and_is_judged_once_for_its_last_author(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await _follow(db_session, tenant_id, ParticipationScope.THREAD, THREAD_ID)
    rig = _Rig(db_session_factory)
    first, other, last = (
        _reply("is the release on?"),
        _reply("no idea", user=OTHER_AAD_OBJECT_ID, name="Grace"),
        _reply("@nobody when is it?"),
    )

    for inbound in (first, other, last):
        await rig.observe(inbound, tenant_id)
    await asyncio.sleep(0)
    assert [t.cancelled() for t in rig.timers] == [True, True, False], "each reply restarts it"

    rig.timers[-1].cancel()
    await rig.participation.judge(THREAD_ID, tenant_id)
    assert rig.fired == [last], "one turn, for the newest message"
    assert rig.reader.calls == [frozenset({first.activity_id, last.activity_id})]
    assert "is the release on?" in rig.prompts[0], "the author's whole burst is judged"
    assert "no idea" not in rig.prompts[0], "another person's message is not their candidate"


async def test_a_silence_verdict_runs_no_turn(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await _follow(db_session, tenant_id, ParticipationScope.THREAD, THREAD_ID)
    rig = _Rig(db_session_factory, decision="silence")

    await rig.observe(_reply("thanks all"), tenant_id)
    rig.timers[-1].cancel()
    await rig.participation.judge(THREAD_ID, tenant_id)
    assert len(rig.prompts) == 1 and rig.fired == []


async def test_an_unfunded_tenant_is_not_classified(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = (await make_tenant(db_session, platform="teams")).id
    await _follow(db_session, tenant_id, ParticipationScope.THREAD, THREAD_ID)
    rig = _Rig(db_session_factory)

    await rig.observe(_reply("anyone?"), tenant_id)
    rig.timers[-1].cancel()
    await rig.participation.judge(THREAD_ID, tenant_id)
    assert rig.prompts == [] and rig.reader.calls == [], "no classifier, no Graph read"
    assert rig.fired == []


async def test_unreadable_history_is_silent(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await _follow(db_session, tenant_id, ParticipationScope.THREAD, THREAD_ID)
    rig = _Rig(db_session_factory, reader=_Reader(GraphUnavailable("forbidden", status=403)))

    await rig.observe(_reply("anyone?"), tenant_id)
    rig.timers[-1].cancel()
    await rig.participation.judge(THREAD_ID, tenant_id)
    assert rig.prompts == [] and rig.fired == []


async def test_a_followed_thread_without_graph_or_a_live_tenant_stays_mention_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await _follow(db_session, tenant_id, ParticipationScope.THREAD, THREAD_ID)
    rig = _Rig(db_session_factory)

    assert await rig.observe(_reply("anyone?"), tenant_id, live=False) == 1
    assert rig.timers == [], "a protected channel or a parked tenant starts no batch"
    no_graph = _Rig(db_session_factory)
    no_graph.participation._reader = None  # pyright: ignore[reportPrivateUsage]
    assert await no_graph.observe(_reply("anyone?"), tenant_id) == 0
    assert no_graph.timers == []


async def test_a_quiet_thread_is_judged_by_its_timer(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id = await _funded_tenant(db_session)
    await _follow(db_session, tenant_id, ParticipationScope.THREAD, THREAD_ID)
    rig = _Rig(db_session_factory, quiet_seconds=0.01)
    reply = _reply("is the release on?")

    await rig.observe(reply, tenant_id)
    await rig.timers[-1]
    assert rig.fired == [reply]
