"""Wiring guard: Discord's built context names stored keys, never their values.

`daimon.core.turn_keys` defines the `<keys>` element contract and Discord's
`context.py` now wires `render_keys_element` into every builder
(`build_context_xml`, `build_delta_xml`, `build_channel_context_xml`) via a
`key_names` parameter the adapter fills from `list_mounted_key_names` after
`bind_session`. This file proves that on Discord, for a real built context:

  - passing key names renders a `<keys>` element naming exactly those keys,
  - no stored *value* ever appears, named or not,
  - passing no names renders no `<keys>` element at all,
  - a stale mounted `.env` (hash mismatch) also renders no `<keys>` element,
    because `list_mounted_key_names` itself returns no names in that case,
  - `context.py` does reference `daimon.core.turn_keys` — the tripwire for
    this wiring existing at all.

This file is the inverse of the guard that existed before this wiring
landed: that version asserted key *names* never appeared either. If you are
re-disabling this wiring, invert these assertions back rather than deleting
them.
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
from daimon.core.turn_keys import list_mounted_key_names
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_STORED_KEY_ONE = "OPENAI_API_KEY"
_STORED_VALUE_ONE = "sk-sentinel-discord-guard-4b7e1a"
_STORED_KEY_TWO = "TOGGL_TOKEN"
_STORED_VALUE_TWO = "toggl-sentinel-discord-guard-c2a9"
_STORED_NAMES = (_STORED_KEY_ONE, _STORED_KEY_TWO)


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
            session,
            tenant_id=tenant.id,
            agent_id=tenant.id,
            key=key,
            content=value,
            set_by_account_id=None,
        )


def _assert_keys_named_never_valued(xml: str, names: tuple[str, ...], *, builder_name: str) -> None:
    assert "<keys" in xml, f"{builder_name} must emit a <keys> element when names are passed"
    for name in names:
        assert name in xml, f"{builder_name} must name every passed key — missing {name!r}"
    for value in (_STORED_VALUE_ONE, _STORED_VALUE_TWO):
        assert value not in xml, (
            f"{builder_name} must never leak a stored key's value — names ride, values never do"
        )


def _assert_no_keys_element(xml: str, *, builder_name: str) -> None:
    assert "<keys" not in xml, (
        f"{builder_name} must not emit a <keys> element when no names are passed"
    )
    for value in (_STORED_VALUE_ONE, _STORED_VALUE_TWO):
        assert value not in xml, (
            f"{builder_name} must never leak a stored key's value, names or no names"
        )


@pytest.mark.asyncio
async def test_build_context_xml_names_keys_never_values(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    trigger = _make_message()
    thread = _make_thread([trigger])

    xml, _ = await build_context_xml(thread, trigger, key_names=_STORED_NAMES)

    _assert_keys_named_never_valued(xml, _STORED_NAMES, builder_name="build_context_xml")


@pytest.mark.asyncio
async def test_build_delta_xml_names_keys_never_values(db_session: AsyncSession) -> None:
    await _seed_stored_keys(db_session)
    trigger = _make_message(msg_id=11)
    thread = _make_thread([trigger])

    xml, _ = await build_delta_xml(thread, trigger, after_message_id=None, key_names=_STORED_NAMES)

    _assert_keys_named_never_valued(xml, _STORED_NAMES, builder_name="build_delta_xml")


@pytest.mark.asyncio
async def test_build_channel_context_xml_names_keys_never_values(
    db_session: AsyncSession,
) -> None:
    await _seed_stored_keys(db_session)
    trigger = _make_message(msg_id=12)
    thread = _make_thread([trigger])
    channel = _make_text_channel([trigger])

    xml, _ = await build_channel_context_xml(
        channel, trigger, thread=thread, key_names=_STORED_NAMES
    )

    _assert_keys_named_never_valued(xml, _STORED_NAMES, builder_name="build_channel_context_xml")


@pytest.mark.asyncio
async def test_build_context_xml_omits_keys_when_no_names_are_passed(
    db_session: AsyncSession,
) -> None:
    await _seed_stored_keys(db_session)
    trigger = _make_message(msg_id=13)
    thread = _make_thread([trigger])

    xml, _ = await build_context_xml(thread, trigger)

    _assert_no_keys_element(xml, builder_name="build_context_xml")


@pytest.mark.asyncio
async def test_build_context_xml_omits_keys_when_the_mounted_env_is_stale(
    db_session: AsyncSession,
) -> None:
    """`list_mounted_key_names` returns no names for a hash that doesn't match

    today's `agent_files` rows, so a builder fed its result renders nothing —
    a session running an older `.env` must never be told about keys it can't
    actually read.
    """
    tenant = await make_tenant(db_session)
    await put_agent_file(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        key=_STORED_KEY_ONE,
        content=_STORED_VALUE_ONE,
        set_by_account_id=None,
    )

    key_names = await list_mounted_key_names(
        db_session,
        tenant_id=tenant.id,
        agent_id=tenant.id,
        env_sha256="0" * 64,  # deliberately mismatched — never a real hash
    )
    assert key_names == (), "a mismatched hash must yield no names to pass to the builder"

    trigger = _make_message(msg_id=14)
    thread = _make_thread([trigger])

    xml, _ = await build_context_xml(thread, trigger, key_names=key_names)

    _assert_no_keys_element(xml, builder_name="build_context_xml")


def test_context_module_references_turn_keys() -> None:
    source = Path("packages/adapters/discord/daimon/adapters/discord/context.py").read_text(
        encoding="utf-8"
    )

    assert "turn_keys" in source, (
        "discord/context.py must reference daimon.core.turn_keys — this is the wiring's "
        "tripwire. If you are reading this because it failed, you unwired render_keys_element: "
        "do so deliberately, then invert the assertions in this file back to name-absence."
    )
