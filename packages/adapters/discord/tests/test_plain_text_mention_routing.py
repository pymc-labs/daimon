"""on_message routing for a plain-text "@<bot name>" (the per-tenant switch).

Same fakes as `test_thread_participation_routing.py`: `_handle_mention` is
stubbed, so what is under test is whether a typed name starts the same turn a
real mention does, only with the switch on, and never twice.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from daimon.adapters.discord import thread_participation
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.tenants import set_plain_text_mentions
from daimon.core.thread_participation import ParticipationMode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_bot
from .test_thread_participation_routing import (
    GUILD_ID,
    THREAD_ID,
    _FakeClassifier,  # pyright: ignore[reportPrivateUsage]
    _follow_thread,  # pyright: ignore[reportPrivateUsage]
    _make_runtime,  # pyright: ignore[reportPrivateUsage]
    _make_thread,  # pyright: ignore[reportPrivateUsage]
    _stub_turn,  # pyright: ignore[reportPrivateUsage]
    _thread_message,  # pyright: ignore[reportPrivateUsage]
)


@pytest_asyncio.fixture
async def tenant_id(db_session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    result = await provision_tenant(
        db_session_factory,
        platform="discord",
        workspace_id=str(GUILD_ID),
        signup_credit=Decimal("10"),
    )
    return result.tenant_id


@pytest.fixture
def classifier(monkeypatch: pytest.MonkeyPatch) -> _FakeClassifier:
    fake = _FakeClassifier()
    monkeypatch.setattr(thread_participation, "classify", fake)
    return fake


async def _switch(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, *, on: bool
) -> None:
    async with sessionmaker() as session, session.begin():
        await set_plain_text_mentions(session, tenant_id=tenant_id, enabled=on)


def _bot(sessionmaker: async_sessionmaker[AsyncSession], **kwargs: float) -> DaimonBot:
    return make_bot(_make_runtime(sessionmaker, mode=ParticipationMode.OFF, **kwargs))


async def test_typed_name_starts_a_turn_when_the_switch_is_on(
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    classifier: _FakeClassifier,
) -> None:
    """With the switch on, "@Daimon" as text is answered like a real mention."""
    await _switch(db_session_factory, tenant_id, on=True)
    bot = _bot(db_session_factory)
    turns = _stub_turn(bot)
    message = _thread_message(_make_thread(), content="@Daimon what do you think?")

    await bot.on_message(message)

    assert turns == [(message, False)], "the typed name must start one ordinary, addressed turn"
    assert classifier.calls == [], "an addressed turn never goes through participation"


async def test_typed_name_is_ignored_when_the_switch_is_off(
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    classifier: _FakeClassifier,
) -> None:
    """Off (an admin's choice) keeps the old behaviour: text is not a mention."""
    await _switch(db_session_factory, tenant_id, on=False)
    bot = _bot(db_session_factory)
    turns = _stub_turn(bot)

    await bot.on_message(_thread_message(_make_thread(), content="@Daimon what do you think?"))

    assert turns == [], "with the switch off a typed name must start no turn"


async def test_bots_never_start_a_turn_by_writing_the_name(
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    classifier: _FakeClassifier,
) -> None:
    """daimon's own answers and other bots name the bot too; none of them may trigger it."""
    await _switch(db_session_factory, tenant_id, on=True)
    bot = _bot(db_session_factory)
    turns = _stub_turn(bot)

    await bot.on_message(
        _thread_message(_make_thread(), content="@Daimon loop", author_is_bot=True, author_id=999)
    )

    assert turns == [], "a bot-authored message must never start a turn by name"


async def test_real_mention_and_typed_name_together_run_one_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    classifier: _FakeClassifier,
) -> None:
    """A message with both forms is one mention, not two turns."""
    await _switch(db_session_factory, tenant_id, on=True)
    bot = _bot(db_session_factory)
    turns = _stub_turn(bot)
    message = _thread_message(_make_thread(), content="<@999> and @Daimon, look", mentions_bot=True)

    await bot.on_message(message)

    assert turns == [(message, False)], "exactly one addressed turn must run"


async def test_typed_name_takes_a_pending_participation_batch(
    db_session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    classifier: _FakeClassifier,
) -> None:
    """Like a real mention, the typed name supersedes a batch waiting to be judged."""
    await _follow_thread(db_session_factory, tenant_id)
    await _switch(db_session_factory, tenant_id, on=True)
    bot = _bot(db_session_factory, quiet_seconds=30.0)
    turns = _stub_turn(bot)
    thread = _make_thread()

    await bot.on_message(_thread_message(thread, content="hmm"))
    assert THREAD_ID in bot._participation_pending, "the follow-up opens a batch"  # pyright: ignore[reportPrivateUsage]
    named = _thread_message(thread, content="@daimon what do you think?")
    await bot.on_message(named)
    await asyncio.sleep(0)

    assert bot._participation_pending == {}, "the typed name took the batch"  # pyright: ignore[reportPrivateUsage]
    assert turns == [(named, False)], "one addressed turn, never a second unprompted one"
    assert classifier.calls == [], "the taken batch is never judged separately"
