"""Model and effort reactions on the trigger message of a turn."""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import discord
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_message_edit import _bot  # pyright: ignore[reportPrivateUsage]


def _admission(*, on: bool, model: str, effort: str) -> MagicMock:
    admission = MagicMock()
    admission.shown = MagicMock(return_value=on)
    admission.backend_revision = None
    admission.agent.model.id = model
    admission.agent.model.model_extra = {"effort": effort}
    return admission


def _marks(bot: DaimonBot, admission: MagicMock, thread_id: int = 7) -> tuple[str, ...]:
    """The marks a turn would ask for; as the lifecycle does once they are shown, record them."""
    marks = bot._model_marks(admission, thread_id)  # pyright: ignore[reportPrivateUsage]
    if marks:
        bot._record_model_marks(thread_id, marks)  # pyright: ignore[reportPrivateUsage]
    return marks


async def test_setting_off_adds_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With the setting off, no emoji are asked for."""
    bot = _bot(db_session_factory)
    marks = _marks(bot, _admission(on=False, model="claude-opus-4-8", effort="high"))
    assert marks == (), "nothing is shown while the setting is off"


async def test_setting_on_shows_the_pair_once_and_again_after_a_change(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The first prompt shows both emoji; the same pair stays quiet; a new pair shows."""
    bot = _bot(db_session_factory)
    first = _marks(bot, _admission(on=True, model="claude-opus-4-8", effort="high"))
    assert first == ("🎼", "🌓"), "the first prompt shows the model and the effort"
    again = _marks(bot, _admission(on=True, model="claude-opus-4-8", effort="high"))
    assert again == (), "the same pair is not shown twice"
    switched = _marks(bot, _admission(on=True, model="claude-haiku-5-5", effort="high"))
    assert switched == ("🍃", "🌓"), "a model change shows the new pair"
    other = _marks(bot, _admission(on=True, model="claude-opus-4-8", effort="high"), 8)
    assert other == ("🎼", "🌓"), "another thread has its own first prompt"


async def test_lifecycle_reacts_with_the_marks_on_the_trigger_message() -> None:
    """The lifecycle adds each emoji to the message that started the turn."""
    trigger = MagicMock(spec=discord.Message)
    trigger.add_reaction = AsyncMock()

    async def send(**kwargs: object) -> object:
        return types.SimpleNamespace(id=1)

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
    recorded: list[bool] = []

    async def send(**kwargs: object) -> object:
        return types.SimpleNamespace(id=1)

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
            on_model_marked=lambda: recorded.append(True),
        )
        await lifecycle.post_initial()
    assert recorded == [True], "only the turn whose reactions landed records the pair"
