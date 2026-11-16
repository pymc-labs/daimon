"""Model and effort reactions on the trigger message of a turn."""

from __future__ import annotations

import types
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.thread_sessions import record_model_marks
from daimon.testing.factories import make_tenant, make_thread_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_message_edit import _bot  # pyright: ignore[reportPrivateUsage]


def _admission(*, on: bool, model: str, effort: str) -> MagicMock:
    admission = MagicMock()
    admission.shown = MagicMock(return_value=on)
    admission.backend_revision = None
    admission.agent.model.id = model
    admission.agent.model.model_extra = {"effort": effort}
    return admission


async def _marks(
    bot: DaimonBot,
    admission: MagicMock,
    sessions: async_sessionmaker[AsyncSession],
    tenant: TenantRow,
    thread_id: int = 7,
    *,
    new_session: bool = True,
) -> tuple[str, ...]:
    """The marks a turn would ask for; as a turn does once they are shown, record them.

    By default each turn runs on a fresh session row, as after a reset or a new agent.
    """
    marks = await bot._model_marks(admission, tenant_id=tenant.id, thread_id=thread_id)  # pyright: ignore[reportPrivateUsage]
    if marks:
        async with sessions.begin() as session:
            if new_session:
                await make_thread_session(session, tenant=tenant, thread_id=str(thread_id))
            await record_model_marks(
                session,
                tenant_id=tenant.id,
                platform="discord",
                thread_id=str(thread_id),
                marks=marks,
            )
    return marks


async def test_setting_off_adds_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With the setting off, no emoji are asked for."""
    tenant = await _tenant(db_session_factory)
    off = _admission(on=False, model="claude-opus-4-8", effort="high")
    marks = await _marks(_bot(db_session_factory), off, db_session_factory, tenant)
    assert marks == (), "nothing is shown while the setting is off"


async def test_setting_on_shows_the_pair_once_and_again_after_a_change(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first prompt shows both emoji; the same pair stays quiet, after a restart too."""
    tenant = await _tenant(db_session_factory)
    opus = _admission(on=True, model="claude-opus-4-8", effort="high")
    first = await _marks(_bot(db_session_factory), opus, db_session_factory, tenant)
    assert first == ("🎼", "🌓"), "the first prompt shows the model and the effort"
    restarted = _bot(db_session_factory)
    again = await _marks(restarted, opus, db_session_factory, tenant)
    assert again == (), "the same pair is not shown twice, even by a restarted bot"
    haiku = _admission(on=True, model="claude-haiku-5-5", effort="high")
    switched = await _marks(restarted, haiku, db_session_factory, tenant)
    assert switched == ("🍃", "🌓"), "a model change shows the new pair"
    other = await _marks(restarted, opus, db_session_factory, tenant, 8)
    assert other == ("🎼", "🌓"), "another thread has its own first prompt"


async def test_sessions_of_one_thread_share_the_pair_last_shown(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Turns alternating between two sessions of a thread compare with the thread's last pair."""
    tenant = await _tenant(db_session_factory)
    async with db_session_factory.begin() as session:
        for _ in range(2):
            await make_thread_session(session, tenant=tenant, thread_id="9")
    bot = _bot(db_session_factory)
    opus = _admission(on=True, model="claude-opus-4-8", effort="high")
    haiku = _admission(on=True, model="claude-haiku-5-5", effort="high")
    shown = [
        await _marks(bot, admission, db_session_factory, tenant, 9, new_session=False)
        for admission in (haiku, opus, haiku, haiku)
    ]
    assert shown == [("🍃", "🌓"), ("🎼", "🌓"), ("🍃", "🌓"), ()], (
        "each change shows, whichever session ran the turn before"
    )


async def _tenant(sessions: async_sessionmaker[AsyncSession]) -> TenantRow:
    async with sessions.begin() as session:
        return await make_tenant(session)


async def test_lifecycle_reacts_with_the_marks_on_the_trigger_message() -> None:
    """The lifecycle adds each emoji to the message that started the turn."""
    trigger = MagicMock(spec=discord.Message)
    trigger.add_reaction = AsyncMock()

    async def send(**kwargs: object) -> discord.Message:
        return cast("discord.Message", types.SimpleNamespace(id=1))

    async def edit(ref: object, **kwargs: object) -> None:
        return None

    for marks in (("🎼", "🌓"), ()):
        trigger.add_reaction.reset_mock()
        lifecycle = DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="agent",
            model_id="m",
            trigger_message=trigger,
            model_marks=marks,
        )
        await lifecycle.post_initial()
        reacted = tuple(call.args[0] for call in trigger.add_reaction.await_args_list)
        assert reacted == marks, f"the trigger message gets exactly {marks}"


async def test_marks_are_recorded_only_after_the_reactions_land() -> None:
    """A failed reaction leaves the thread unmarked, so the next prompt tries again."""
    trigger = MagicMock(spec=discord.Message)
    marked: list[bool] = []

    async def send(**kwargs: object) -> discord.Message:
        return cast("discord.Message", types.SimpleNamespace(id=1))

    async def edit(ref: object, **kwargs: object) -> None:
        return None

    for fails in (True, False):
        trigger.add_reaction = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=403), "no") if fails else None
        )
        lifecycle = DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="agent",
            model_id="m",
            trigger_message=trigger,
            model_marks=("🎼", "🌓"),
        )
        await lifecycle.post_initial()
        marked.append(lifecycle.model_marked)
    assert marked == [False, True], "only the turn whose reactions landed records the pair"
