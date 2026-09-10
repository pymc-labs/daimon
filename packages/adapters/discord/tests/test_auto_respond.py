"""Tests for `AutoResponder` -- the Discord shell around the thread-participation decision.

Real Postgres for the scope rows and the ledger; a fake `discord.Thread` for
chat history; the classifier call is replaced by a recording fake so each test
can say what it returns and assert whether it was consulted at all. A gate
that skips must never spend a classifier call.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import auto_respond
from daimon.adapters.discord.auto_respond import AutoResponder
from daimon.core.config import ThreadParticipationSettings
from daimon.core.stores import thread_participation as store
from daimon.core.thread_participation import (
    ClassifierMessage,
    ClassifierVerdict,
    ParticipationMode,
    ParticipationScope,
    ResolvedParticipation,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOT_ID = 999
ALICE_ID = 111
THREAD_ID = 4242
PARENT_ID = 4200

ON_THREAD = ResolvedParticipation(ParticipationMode.ON, "thread")


def _msg(*, author_id: int, content: str, message_id: int) -> Any:
    author = SimpleNamespace(
        id=author_id, display_name=f"user-{author_id}", bot=author_id == BOT_ID
    )
    return SimpleNamespace(id=message_id, author=author, content=content, reactions=[])


def _thread(history: list[Any]) -> Any:
    """A `discord.Thread` stand-in. `history()` yields newest first, like discord.py."""
    thread = MagicMock(spec=discord.Thread)
    thread.id = THREAD_ID
    thread.parent_id = PARENT_ID

    def _history(*, limit: int | None = None, after: Any | None = None) -> AsyncIterator[Any]:
        async def _gen() -> AsyncIterator[Any]:
            newest_first = sorted(history, key=lambda m: m.id, reverse=True)
            for m in newest_first[: limit or len(newest_first)]:
                yield m

        return _gen()

    thread.history = _history
    thread.fetch_message = AsyncMock(return_value=None)
    return thread


def _candidates(*contents: str) -> list[Any]:
    return [
        _msg(author_id=ALICE_ID, content=content, message_id=900 + i)
        for i, content in enumerate(contents)
    ]


class _FakeClassifier:
    def __init__(self, decision: str = "respond") -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, anthropic: Any, **kwargs: Any) -> ClassifierVerdict:
        self.calls.append(kwargs)
        return ClassifierVerdict(self.decision, "fake", 0.9)


@pytest.fixture
def classifier(monkeypatch: pytest.MonkeyPatch) -> _FakeClassifier:
    fake = _FakeClassifier()
    monkeypatch.setattr(auto_respond, "classify", fake)
    return fake


def _responder(sessionmaker: async_sessionmaker[AsyncSession], **overrides: Any) -> AutoResponder:
    return AutoResponder(
        settings=ThreadParticipationSettings(**overrides),
        sessionmaker=sessionmaker,
        anthropic=MagicMock(),
        bot_user_id=BOT_ID,
        bot_display_name="daimon",
    )


async def _set(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    scope: ParticipationScope,
    scope_id: str | None,
    mode: ParticipationMode,
) -> None:
    await store.set_participation_mode(
        session,
        tenant_id=tenant_id,
        platform="discord",
        scope=scope,
        scope_id=scope_id,
        mode=mode,
    )


async def test_resolve_walks_the_cascade_from_real_rows(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await _set(
        db_session, tenant.id, ParticipationScope.THREAD, str(THREAD_ID), ParticipationMode.ON
    )
    await db_session.commit()
    responder = _responder(db_session_factory, mode="off")
    thread = _thread([])

    assert await responder.resolve(tenant_id=tenant.id, thread=thread) == ResolvedParticipation(
        ParticipationMode.ON, "thread"
    ), "a thread turned on overrides an off deployment"

    await _set(
        db_session,
        tenant.id,
        ParticipationScope.CHANNEL,
        str(PARENT_ID),
        ParticipationMode.DISABLED,
    )
    await db_session.commit()
    assert await responder.resolve(tenant_id=tenant.id, thread=thread) == ResolvedParticipation(
        ParticipationMode.DISABLED, "channel"
    ), "disabled at the channel is final for every thread under it"


async def test_rate_limited_thread_skips_without_a_classifier_call(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    classifier: _FakeClassifier,
) -> None:
    tenant = await make_tenant(db_session)
    for i in range(2):
        await store.record_auto_response(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id=str(THREAD_ID),
            message_id=str(100 + i),
            created_at=datetime.now(UTC) - timedelta(minutes=10 * (i + 1)),
        )
    await db_session.commit()

    assert (
        await _responder(db_session_factory, mode="on", max_per_hour=2).should_respond(
            _thread([]), _candidates("and again?"), tenant_id=tenant.id, resolved=ON_THREAD
        )
        is False
    )
    assert classifier.calls == [], "a pre-classifier skip must not spend a model call"

    assert (
        await _responder(db_session_factory, mode="on", max_per_hour=3).should_respond(
            _thread([]), _candidates("and again?"), tenant_id=tenant.id, resolved=ON_THREAD
        )
        is True
    ), "rows older than the window aside, one slot left is enough"


async def test_a_burst_is_one_classifier_call_over_the_window_around_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    classifier: _FakeClassifier,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    history = [
        _msg(author_id=ALICE_ID, content="fit this", message_id=1),
        _msg(author_id=BOT_ID, content="done, here is the fit", message_id=2),
    ]
    candidates = _candidates("so what does the posterior look like?", "and the prior?")
    thread = _thread([*history, *candidates])

    assert (
        await _responder(db_session_factory, mode="on").should_respond(
            thread, candidates, tenant_id=tenant.id, resolved=ON_THREAD
        )
        is True
    )
    (call,) = classifier.calls
    assert call["recent"] == [
        ClassifierMessage("user-111", "fit this", is_bot=False),
        ClassifierMessage("user-999", "done, here is the fit", is_bot=True),
    ], "window is oldest-first, bot messages flagged, the burst excluded"
    assert [c.content for c in call["candidates"]] == [
        "so what does the posterior look like?",
        "and the prior?",
    ]
    assert call["model"] == "claude-haiku-4-5"


async def test_classifier_silence_is_final(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    classifier: _FakeClassifier,
) -> None:
    classifier.decision = "silence"
    tenant = await make_tenant(db_session)
    await db_session.commit()

    assert (
        await _responder(db_session_factory, mode="on").should_respond(
            _thread([]), _candidates("thanks!"), tenant_id=tenant.id, resolved=ON_THREAD
        )
        is False
    )
    assert len(classifier.calls) == 1


async def test_a_mode_that_is_not_on_never_reaches_the_classifier(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    classifier: _FakeClassifier,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    assert (
        await _responder(db_session_factory, mode="on").should_respond(
            _thread([]),
            _candidates("hello?"),
            tenant_id=tenant.id,
            resolved=ResolvedParticipation(ParticipationMode.OFF, "deployment"),
        )
        is False
    )
    assert classifier.calls == []


async def test_record_writes_one_ledger_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    await _responder(db_session_factory).record(
        tenant_id=tenant.id, thread_id=THREAD_ID, message_id="777"
    )

    async with db_session_factory() as fresh:
        assert (
            await store.count_auto_responses_since(
                fresh,
                tenant_id=tenant.id,
                platform="discord",
                thread_id=str(THREAD_ID),
                since=datetime.now(UTC) - timedelta(minutes=1),
            )
            == 1
        )
