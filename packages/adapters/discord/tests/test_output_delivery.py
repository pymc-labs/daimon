"""Discord adapter delivery through the shared session-output sweep."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.output_delivery import deliver_session_outputs
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
_MIB = 1024 * 1024


def _client_with_file(
    *, filename: str, size_bytes: int, content: bytes | None = None
) -> tuple[AsyncAnthropic, list[str]]:
    deleted: list[str] = []
    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id="file_chart",
                    created_at=NOW,
                    filename=filename,
                    mime_type="image/png",
                    size_bytes=size_bytes,
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
            ]
        ),
    )
    if content is not None:
        router.add(
            "GET",
            r"/v1/files/([^/]+)/content",
            lambda request, match: httpx.Response(200, content=content),
        )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deleted.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    return build_fake_anthropic(router.dispatch), deleted


def _thread(limit: int = 10 * _MIB) -> discord.Thread:
    thread = MagicMock(spec=discord.Thread)
    thread.id = 123
    thread.guild.id = 456
    thread.guild.filesize_limit = limit
    thread.send = AsyncMock()
    return cast(discord.Thread, thread)


async def _no_sleep(delay: float) -> None:
    pass


async def _allowed() -> bool:
    return True


async def test_delivers_file_and_deletes_only_after_discord_post() -> None:
    client, deleted = _client_with_file(filename="chart", size_bytes=9, content=b"png-bytes")
    thread = _thread()

    async def send(*args: object, **kwargs: object) -> None:
        assert deleted == [], "MA listing must still exist while Discord posts"
        file = kwargs["file"]
        assert isinstance(file, discord.File)
        assert file.filename == "chart.png"
        assert file.fp.read() == b"png-bytes"

    thread.send = AsyncMock(side_effect=send)
    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        sleep=_no_sleep,
    )

    assert thread.send.await_count == 1
    assert deleted == ["file_chart"]


def _history(*messages: object) -> MagicMock:
    async def history(**kwargs: object):
        assert kwargs["after"].id == 777, "only posts after the turn's card count"
        assert kwargs["before"].id == 999, "a later turn's posts never count"
        assert kwargs["limit"] is None, "the whole turn is read"
        for message in messages:
            yield message

    return MagicMock(side_effect=history)


def _posted(
    *,
    author_id: int,
    filename: str,
    size: int,
    content: bytes = b"png-bytes",
    webhook: bool = False,
) -> MagicMock:
    message = MagicMock()
    message.author.id = author_id
    message.webhook_id = 1 if webhook else None
    message.application_id = author_id if webhook else None
    attachment = MagicMock()
    attachment.filename = filename
    attachment.size = size
    attachment.read = AsyncMock(return_value=content)
    message.attachments = [attachment]
    return message


async def test_a_file_the_agent_already_posted_is_cleared_without_a_second_post() -> None:
    client, deleted = _client_with_file(filename="chart", size_bytes=9, content=b"png-bytes")
    thread = _thread()
    thread.guild.me.id = 42
    thread.history = _history(_posted(author_id=42, filename="chart.png", size=9))

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=(777, 999),
        sleep=_no_sleep,
    )

    assert thread.send.await_count == 0, "the agent's own post already delivered it"
    assert deleted == ["file_chart"], "the listing entry is cleared as delivered"


async def test_a_revised_file_with_the_same_name_and_size_still_posts() -> None:
    client, deleted = _client_with_file(filename="chart", size_bytes=9, content=b"png-bytes")
    thread = _thread()
    thread.guild.me.id = 42
    thread.history = _history(
        _posted(author_id=42, filename="chart.png", size=9, content=b"old-bytes")
    )

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=(777, 999),
        sleep=_no_sleep,
    )

    assert thread.send.await_count == 1, "different bytes are a new file, never dropped"
    assert deleted == ["file_chart"]


async def test_a_same_named_file_from_someone_else_or_another_size_still_posts() -> None:
    client, deleted = _client_with_file(filename="chart", size_bytes=9, content=b"png-bytes")
    thread = _thread()
    thread.guild.me.id = 42
    thread.history = _history(
        _posted(author_id=7, filename="chart.png", size=9),
        _posted(author_id=42, filename="chart.png", size=10),
    )
    thread.send = AsyncMock(return_value=SimpleNamespace(id=555))
    posted: list[int] = []

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=(777, 999),
        posted=posted,
        sleep=_no_sleep,
    )

    assert thread.send.await_count == 1
    assert deleted == ["file_chart"]
    assert posted == [555], "the sweep reports what it posted, for the summary move"


async def test_oversize_uses_guild_limit_and_posts_skip_notice() -> None:
    client, deleted = _client_with_file(filename="big chart", size_bytes=10 * _MIB + 1)
    thread = _thread()

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        sleep=_no_sleep,
    )

    thread.send.assert_awaited_once()
    notice = thread.send.await_args.args[0]
    assert "big_chart" in notice and "10 MiB" in notice
    assert deleted == ["file_chart"], "the entry is removed after the skip notice"


async def test_missing_attach_permission_keeps_file_and_posts_one_notice() -> None:
    client, deleted = _client_with_file(filename="chart.png", size_bytes=9, content=b"png-bytes")
    thread = _thread()
    response = MagicMock(status=403, reason="Forbidden")
    forbidden = discord.Forbidden(response, "Missing Permissions")
    notice_thread_ids: set[int] = set()
    thread.send = AsyncMock(side_effect=[forbidden, None, forbidden])

    for _ in range(2):
        await deliver_session_outputs(
            client,
            thread,
            session_id="sesn_1",
            may_post=_allowed,
            notice_thread_ids=notice_thread_ids,
            sleep=_no_sleep,
        )

    assert deleted == [], "a failed upload remains listed for retry"
    assert 123 in notice_thread_ids
    assert thread.send.await_count == 3
    assert "Attach Files" in thread.send.await_args_list[1].args[0]


async def test_protected_thread_posts_nothing_and_keeps_file() -> None:
    client, deleted = _client_with_file(filename="chart.png", size_bytes=9, content=b"png-bytes")
    thread = _thread()

    async def protected() -> bool:
        return False

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=protected,
        notice_thread_ids=set(),
        sleep=_no_sleep,
    )

    thread.send.assert_not_awaited()
    assert deleted == [], "protected-channel files remain for a later allowed sweep"


async def test_tool_turn_sweeps_detached_and_same_session_sweeps_chain() -> None:
    runtime = MagicMock()
    bot = DaimonBot(runtime=cast(DiscordRuntime, runtime), intents=discord.Intents.default())
    thread = _thread()
    tenant_id = uuid.uuid4()
    text_outcome = RunOutcome(
        TurnState(content=[TextBlock(kind="text", text="done")]), "sesn_1", None, False
    )
    tool_outcome = RunOutcome(
        TurnState(
            content=[
                ToolUseBlock(
                    kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
                )
            ]
        ),
        "sesn_1",
        None,
        False,
    )
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    calls = 0

    async def sweep(*args: object, **kwargs: object) -> None:
        nonlocal calls
        index = calls
        calls += 1
        started[index].set()
        await release[index].wait()

    with patch("daimon.adapters.discord.bot.deliver_session_outputs", side_effect=sweep):
        bot._schedule_output_sweep(text_outcome, thread=thread, tenant_id=tenant_id)  # pyright: ignore[reportPrivateUsage]
        assert bot._output_sweeps == {}  # pyright: ignore[reportPrivateUsage]
        bot._schedule_output_sweep(tool_outcome, thread=thread, tenant_id=tenant_id)  # pyright: ignore[reportPrivateUsage]
        await started[0].wait()
        bot._schedule_output_sweep(tool_outcome, thread=thread, tenant_id=tenant_id)  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0)
        assert calls == 1, "the successor must wait for the first sweep"
        release[0].set()
        await started[1].wait()
        release[1].set()
        await asyncio.gather(*list(bot._bg_tasks))  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0)
        assert bot._output_sweeps == {}  # pyright: ignore[reportPrivateUsage]
