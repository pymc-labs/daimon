"""Status reactions and the protected-channel reaction are read for a thread's own agent."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import bot as bot_module
from daimon.adapters.discord.bot import DaimonBot
from daimon.core.features.snapshot import FeaturePlace

TENANT = uuid.uuid4()


def _in_thread() -> Any:
    channel = MagicMock(spec=discord.Thread)
    channel.id, channel.parent_id = 20, 10
    return MagicMock(channel=channel)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[FeaturePlace]:
    places: list[FeaturePlace] = []

    async def place_feature(_sessions: Any, _settings: Any, place: FeaturePlace, _key: str) -> Any:
        places.append(place)
        return SimpleNamespace(on=True)

    binding = SimpleNamespace(responder_ma_agent_id="ag_1")
    monkeypatch.setattr(bot_module, "place_feature", place_feature)
    monkeypatch.setattr(bot_module, "get_binding", AsyncMock(return_value=binding))
    return places


def _bot() -> Any:
    session = MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    bot = SimpleNamespace(runtime=MagicMock(sessionmaker=MagicMock(return_value=session)))
    bot._thread_agent = DaimonBot._thread_agent.__get__(bot)  # pyright: ignore[reportPrivateUsage]
    bot._message_feature_on = DaimonBot._message_feature_on.__get__(bot)  # pyright: ignore[reportPrivateUsage]
    return bot


async def test_the_working_mark_reads_status_reactions_for_the_threads_agent(
    reads: list[FeaturePlace],
) -> None:
    assert await DaimonBot.working_mark(_bot(), _in_thread(), TENANT) == bot_module.WORKING
    assert [p.agent_id for p in reads] == ["ag_1"]
    assert (reads[0].channel_id, reads[0].thread_id) == ("10", "20")


def test_every_message_feature_read_is_for_the_threads_agent() -> None:
    """Status reactions (⏳ and 📨) and the protected-channel 🔒 alike."""
    tree = ast.parse(Path(bot_module.__file__).read_text())
    agent_keys = {"status_reactions", "protected_notice_reaction"}
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_message_feature_on"
        and isinstance(node.args[2], ast.Constant)
        and node.args[2].value in agent_keys
    ]
    assert len(calls) == 3, "the ⏳, the 📨 and the 🔒"
    for call in calls:
        per_agent = [k.value for k in call.keywords if k.arg == "per_agent"]
        assert per_agent and isinstance(per_agent[0], ast.Constant) and per_agent[0].value is True


async def test_a_channel_message_has_no_agent_to_read_for(reads: list[FeaturePlace]) -> None:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 10
    message = MagicMock(channel=channel)
    await _bot()._message_feature_on(message, TENANT, "status_reactions", per_agent=True)
    assert [p.agent_id for p in reads] == [None]
