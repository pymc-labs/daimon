"""Disabled-contract guard: Discord's built context carries no <keys> element.

`daimon.core.turn_keys` defines the `<keys>` element contract but nothing
wires it into any adapter yet (the reused-session refresh mechanism it would
depend on does not exist). This file proves that on Discord, for a real
built context, against an agent with stored keys:

  - no `<keys` element appears,
  - no stored value appears,
  - no stored key *name* appears either, since nothing injects them,
  - `context.py` carries no reference to the `turn_keys` module.

When a later phase wires `render_keys_element` into a builder deliberately,
every assertion here about key *names* being absent must be inverted (the
value-absence assertions must stay, unchanged). Finding this test red is the
signal that the wiring happened; go update the assertions here to match the
new, intended behaviour instead of deleting them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.context import (
    build_channel_context_xml,
    build_context_xml,
    build_delta_xml,
)
from daimon.core.stores.agent_files import put_agent_file
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_STORED_KEY_ONE = "OPENAI_API_KEY"
_STORED_VALUE_ONE = "sk-sentinel-discord-guard-4b7e1a"
_STORED_KEY_TWO = "TOGGL_TOKEN"
_STORED_VALUE_TWO = "toggl-sentinel-discord-guard-c2a9"


class _AsyncIter:
    """Async iterator adapter for mocked ``thread.history()`` / ``channel.history()``."""

    def __init__(self, items: list[discord.Message]) -> None:
        self._items = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> discord.Message:
        try:
            return next(self._items)
        except StopIteration as err:
            raise StopAsyncIteration from err


def _make_message(*, msg_id: int = 10, content: str = "what keys do you have?") -> discord.Message:
    msg = MagicMock(spec=discord.Message)
    msg.id = msg_id
    msg.content = content
    msg.author = MagicMock()
    msg.author.display_name = "Alice"
    msg.author.id = 200
    msg.author.bot = False
    msg.created_at = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)
    msg.attachments = []
    return msg


def _make_thread(messages: list[discord.Message]) -> discord.Thread:
    thread = MagicMock(spec=discord.Thread)
    thread.history = MagicMock(return_value=_AsyncIter(messages))
    thread.id = 900
    thread.parent_id = 800
    thread.name = "Chat with daimon"
    thread.starter_message = None
    thread.parent = None
    return thread


def _make_text_channel(messages: list[discord.Message]) -> discord.TextChannel:
    channel = MagicMock(spec=discord.TextChannel)
    channel.history = MagicMock(return_value=_AsyncIter(messages))
    return channel


async def _seed_stored_keys(session: AsyncSession) -> None:
    tenant = await make_tenant(session)
    for key, value in (
        (_STORED_KEY_ONE, _STORED_VALUE_ONE),
        (_STORED_KEY_TWO, _STORED_VALUE_TWO),
    ):
        await put_agent_file(
            session, tenant_id=tenant.id, agent_id=tenant.id, key=key, content=value
        )


def _assert_no_keys_element(xml: str, *, builder_name: str) -> None:
    assert "<keys" not in xml, (
        f"{builder_name} must not emit a <keys> element — nothing wires "
        "render_keys_element into any adapter yet"
    )
    for value in (_STORED_VALUE_ONE, _STORED_VALUE_TWO):
        assert value not in xml, (
            f"{builder_name} must never leak a stored key's value, wiring or no wiring"
        )
    for name in (_STORED_KEY_ONE, _STORED_KEY_TWO):
        assert name not in xml, (
            f"{builder_name} must not mention a stored key's name today — nothing injects "
            "key names into a turn yet. This expectation inverts once a later phase wires "
            "render_keys_element into this builder; when it does, update this assertion to "
            f"require the name instead of forbidding it."
        )


@pytest.mark.asyncio
async def test_build_context_xml_carries_no_keys_element(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    trigger = _make_message()
    thread = _make_thread([trigger])

    xml, _ = await build_context_xml(thread, trigger)

    _assert_no_keys_element(xml, builder_name="build_context_xml")


@pytest.mark.asyncio
async def test_build_delta_xml_carries_no_keys_element(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    trigger = _make_message(msg_id=11)
    thread = _make_thread([trigger])

    xml, _ = await build_delta_xml(thread, trigger, after_message_id=None)

    _assert_no_keys_element(xml, builder_name="build_delta_xml")


@pytest.mark.asyncio
async def test_build_channel_context_xml_carries_no_keys_element(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    trigger = _make_message(msg_id=12)
    thread = _make_thread([trigger])
    channel = _make_text_channel([trigger])

    xml, _ = await build_channel_context_xml(channel, trigger, thread=thread)

    _assert_no_keys_element(xml, builder_name="build_channel_context_xml")


def test_context_module_does_not_reference_turn_keys() -> None:
    source = Path("packages/adapters/discord/daimon/adapters/discord/context.py").read_text(
        encoding="utf-8"
    )

    assert "turn_keys" not in source, (
        "discord/context.py must not reference daimon.core.turn_keys — the <keys> element "
        "contract is intentionally unwired. If you are reading this because it failed, you "
        "wired it: do so deliberately, then invert the name-absence assertions in "
        "test_context_no_keys_element.py to match the new, intended behaviour."
    )
