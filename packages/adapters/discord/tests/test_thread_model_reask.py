"""Discord's ask-again card for a conversation's model, and its persistent Confirm."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import MISSING, fields, replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord import thread_model_reask as module
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.thread_model_flow import ThreadConfirm
from daimon.core.thread_model_reask import ThreadReask
from daimon.testing.turn_fakes import deps_for
from mux.contracts.ids import ModelRef

THREAD = 222222222222222222
CONFIRM = ThreadConfirm("openai:gpt-6", "a" * 64, 3)
LINES = ("Model: GPT-6", "Cost: $2 per 1M tokens")


def _runtime() -> DiscordRuntime:
    needed = [
        f.name
        for f in fields(DiscordRuntime)
        if f.default is MISSING and f.default_factory is MISSING
    ]
    return DiscordRuntime(**{name: MagicMock() for name in needed})  # type: ignore[arg-type]  # fmt: skip


def _ask(confirm: ThreadConfirm | None = CONFIRM) -> ThreadReask:
    return ThreadReask(
        tenant_id=MagicMock(),
        workspace_id="700000009",
        platform="discord",
        channel_id="111111111111111111",
        thread_id=str(THREAD),
        conversation_id=str(THREAD),
        agent_name="Helper",
        lines=LINES,
        confirm=confirm,
    )


def _client() -> tuple[MagicMock, AsyncMock]:
    thread = MagicMock(spec=discord.Thread)
    thread.send = AsyncMock()
    client = MagicMock()
    client.get_channel.return_value = thread
    return client, thread.send


def test_the_custom_id_carries_the_shown_revision_and_digest() -> None:
    custom_id = module.build_custom_id(CONFIRM)

    match = re.fullmatch(module.CUSTOM_ID_TEMPLATE, custom_id)
    assert match is not None and len(custom_id) <= 100
    assert (int(match["revision"]), match["digest"]) == (CONFIRM.revision, CONFIRM.digest)


async def test_the_ask_again_card_is_posted_in_the_thread_with_one_confirm() -> None:
    client, send = _client()

    await module.discord_thread_reask_notifier(client)(_ask())

    client.get_channel.assert_called_once_with(THREAD)
    kwargs = send.call_args.kwargs
    assert "Confirm to switch this conversation." in kwargs["embed"].description
    (button,) = kwargs["view"].children
    assert button.custom_id == module.build_custom_id(CONFIRM)


async def test_a_refused_preview_is_posted_without_a_button() -> None:
    client, send = _client()

    await module.discord_thread_reask_notifier(client)(_ask(None))

    assert "view" not in send.call_args.kwargs


async def test_a_failed_post_raises_so_the_next_message_asks_again() -> None:
    client, send = _client()
    send.side_effect = discord.HTTPException(MagicMock(status=500), "down")

    with pytest.raises(discord.HTTPException):
        await module.discord_thread_reask_notifier(client)(_ask())


def test_the_bot_wires_the_ask_again_card_into_its_turns() -> None:
    runtime = replace(_runtime(), turn_deps=deps_for())

    wired = module.with_thread_reask_notifier(runtime, MagicMock())

    assert wired.turn_deps.thread_reask_notifier is not None
    stand_in = MagicMock()
    assert module.with_thread_reask_notifier(stand_in, MagicMock()) is stand_in


@pytest.fixture
def click(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A click in the thread with every seam replaced; returns the seams."""
    seams: dict[str, Any] = {
        "may": AsyncMock(return_value=True),
        "confirm": AsyncMock(return_value="Switched."),
        "choice": AsyncMock(return_value=ModelRef(provider="openai", id="gpt-6")),
    }
    monkeypatch.setattr(module, "resolve_tenant_for_interaction", AsyncMock(return_value="t"))
    monkeypatch.setattr(module, "may_switch_thread", seams["may"])
    monkeypatch.setattr(module, "is_guild_admin", AsyncMock(return_value=False))
    monkeypatch.setattr(module, "thread_pick_of", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(module, "get_thread_choice", seams["choice"])
    monkeypatch.setattr(module, "confirm_thread_pick", seams["confirm"])

    @asynccontextmanager
    async def session() -> AsyncIterator[MagicMock]:
        yield MagicMock()

    interaction = MagicMock()
    channel = MagicMock(spec=discord.Thread)
    channel.id = THREAD
    interaction.channel = channel
    interaction.client.runtime.sessionmaker = session
    interaction.response.is_done.return_value = True
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    seams["interaction"] = interaction
    return seams


async def test_confirm_keeps_the_stored_pick_at_the_digest_it_was_shown(
    click: dict[str, Any],
) -> None:
    button = module.ReaskConfirmButton(revision=CONFIRM.revision, digest=CONFIRM.digest)

    await button.callback(click["interaction"])

    confirm = click["confirm"].await_args
    assert confirm.args[1] == CONFIRM
    assert confirm.kwargs["conversation_id"] == str(THREAD)
    assert click["interaction"].followup.send.await_args.args == ("Switched.",)


async def test_confirm_from_someone_who_may_not_changes_nothing(click: dict[str, Any]) -> None:
    click["may"].return_value = False
    button = module.ReaskConfirmButton(revision=CONFIRM.revision, digest=CONFIRM.digest)

    await button.callback(click["interaction"])

    click["confirm"].assert_not_awaited()
    (text,) = click["interaction"].followup.send.await_args.args
    assert text.endswith("Nothing changed.")
