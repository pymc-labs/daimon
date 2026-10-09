"""Discord adapter delivery through the shared session-output sweep."""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import FileMetadata
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.output_delivery import (
    MAX_ATTACHMENTS_PER_MESSAGE,
    TURN_END_GRACE,
    AnswerMessage,
    deliver_session_outputs,
)
from daimon.adapters.discord.post_transport import DiscordPostTransport, _webhooks
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.testing.ma import MARouter, build_fake_anthropic, list_response

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)
# The turn's card is message 777; the turn ended a minute after the files were written.
_WINDOW = (777, discord.utils.time_snowflake(NOW + timedelta(minutes=1), high=True))
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
        assert kwargs["before"].id == _WINDOW[1], "a later turn's posts never count"
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
        turn_window=_WINDOW,
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
        turn_window=_WINDOW,
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

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_WINDOW,
        sleep=_no_sleep,
    )

    assert thread.send.await_count == 1
    assert deleted == ["file_chart"]


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


# --- Files go onto the turn's answer -----------------------------------------

_ANSWER_ID = 888


def _client_with_files(
    *files: tuple[str, str, bytes, datetime],
) -> tuple[AsyncAnthropic, list[str]]:
    """A session listing of ``(file id, filename, bytes, created_at)`` entries."""
    deleted: list[str] = []
    contents = {file_id: content for file_id, _name, content, _at in files}
    router = MARouter()
    router.add(
        "GET",
        r"/v1/files",
        lambda request, match: list_response(
            [
                FileMetadata(
                    id=file_id,
                    created_at=created_at,
                    filename=filename,
                    mime_type="application/pdf",
                    size_bytes=len(content),
                    type="file",
                    downloadable=True,
                ).model_dump(mode="json")
                for file_id, filename, content, created_at in files
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/files/([^/]+)/content",
        lambda request, match: httpx.Response(200, content=contents[match.group(1)]),
    )

    def on_delete(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        deleted.append(match.group(1))
        return httpx.Response(200, json={"id": match.group(1), "type": "file_deleted"})

    router.add("DELETE", r"/v1/files/([^/]+)", on_delete)
    return build_fake_anthropic(router.dispatch), deleted


def _attachment(filename: str) -> MagicMock:
    attachment = MagicMock(spec=discord.Attachment)
    attachment.filename = filename
    return attachment


def _answer_message(attachments: list[object]) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.id = _ANSWER_ID
    message.attachments = attachments
    return message


def _window_after(created_at: datetime) -> tuple[int, int]:
    """A turn window that closed a minute after ``created_at``."""
    end = discord.utils.time_snowflake(created_at + timedelta(minutes=1), high=True)
    return (777, end)


def _answer_thread(fetched: MagicMock) -> discord.Thread:
    thread = _thread()
    thread.guild.me.id = 42
    thread.fetch_message = AsyncMock(return_value=fetched)

    async def history(**kwargs: object):
        return
        yield

    thread.history = MagicMock(side_effect=history)
    return thread


def _recording_edit(
    deleted: list[str],
) -> tuple[AnswerMessage, list[tuple[object, dict[str, object]]]]:
    """An answer edit that records each call and returns the message as it now stands."""
    calls: list[tuple[object, dict[str, object]]] = []

    async def edit(message: object, **kwargs: object) -> discord.Message:
        calls.append((message, {**kwargs, "_deleted_at_edit": list(deleted)}))
        attachments = cast(list[object], kwargs["attachments"])
        kept = [a for a in attachments if not isinstance(a, discord.File)]
        added = [_attachment(f.filename) for f in attachments if isinstance(f, discord.File)]
        return cast(discord.Message, _answer_message([*kept, *added]))

    return AnswerMessage(message_id=_ANSWER_ID, edit=edit), calls


async def test_files_are_added_to_the_answer_keeping_what_is_already_there() -> None:
    client, deleted = _client_with_files(
        ("file_a", "brief_short.pdf", b"pdf-a", NOW),
        ("file_b", "chart.pdf", b"pdf-b", NOW),
    )
    table = _attachment("table.png")
    thread = _answer_thread(_answer_message([table]))
    answer, calls = _recording_edit(deleted)

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_window_after(NOW),
        answer=answer,
        sleep=_no_sleep,
    )

    thread.send.assert_not_awaited()
    cast(AsyncMock, thread.fetch_message).assert_awaited_once_with(_ANSWER_ID)
    assert len(calls) == 2, "one edit per file"
    first, second = (kwargs for _message, kwargs in calls)
    assert first["_allow_replacement"] is False, "an uneditable answer is never reposted"
    first_kept = cast(list[object], first["attachments"])
    assert first_kept[0] is table, "the table image stays on the answer"
    assert first["_deleted_at_edit"] == [], "the listing entry outlives the edit"
    second_list = cast(list[object], second["attachments"])
    assert [getattr(a, "filename", None) for a in second_list] == [
        "table.png",
        "brief_short.pdf",
        "chart.pdf",
    ], "each edit starts from the message the previous edit returned"
    assert sorted(deleted) == ["file_a", "file_b"]


async def test_a_failed_answer_edit_posts_the_file_and_never_reposts_the_answer() -> None:
    """Identity on: the answer is a webhook message, and its webhook token is gone."""
    _webhooks.clear()
    client, deleted = _client_with_files(("file_a", "brief_short.pdf", b"pdf-a", NOW))
    fetched = _answer_message([])
    fetched.webhook_id = 30
    fetched.application_id = 10
    thread = _answer_thread(fetched)
    thread.parent = MagicMock(spec=discord.TextChannel)
    thread.parent.id = 20
    thread.locked = False
    bot = MagicMock()
    bot.application_id = 10
    bot.http.channel_webhooks = AsyncMock(return_value=[])
    transport = DiscordPostTransport(
        bot, thread, name="Research", avatar_url=None, builtin=False, identity_enabled=True
    )
    transport_send = AsyncMock()
    transport.send = transport_send

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_window_after(NOW),
        answer=AnswerMessage(message_id=_ANSWER_ID, edit=transport.edit),
        sleep=_no_sleep,
    )

    transport_send.assert_not_awaited()
    thread.send.assert_awaited_once()
    sent = thread.send.await_args.kwargs
    assert set(sent) == {"file"}, "the fallback is the file alone, not the answer's text"
    assert sent["file"].filename == "brief_short.pdf"
    assert deleted == ["file_a"]


async def test_after_one_failed_edit_the_rest_post_on_their_own() -> None:
    client, deleted = _client_with_files(
        ("file_a", "a.pdf", b"pdf-a", NOW), ("file_b", "b.pdf", b"pdf-b", NOW)
    )
    thread = _answer_thread(_answer_message([]))
    edit = AsyncMock(side_effect=discord.ClientException("own webhook token unavailable"))

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_window_after(NOW),
        answer=AnswerMessage(message_id=_ANSWER_ID, edit=edit),
        sleep=_no_sleep,
    )

    assert edit.await_count == 1, "a broken answer is not retried for every file"
    assert thread.send.await_count == 2
    assert sorted(deleted) == ["file_a", "file_b"]


async def test_files_past_the_ten_attachment_limit_post_on_their_own() -> None:
    client, deleted = _client_with_files(
        ("file_a", "a.pdf", b"pdf-a", NOW), ("file_b", "b.pdf", b"pdf-b", NOW)
    )
    existing = [_attachment(f"table{i}.png") for i in range(MAX_ATTACHMENTS_PER_MESSAGE - 1)]
    thread = _answer_thread(_answer_message(list(existing)))
    answer, calls = _recording_edit(deleted)

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_window_after(NOW),
        answer=answer,
        sleep=_no_sleep,
    )

    assert len(calls) == 1, "the first file fills the answer's last slot"
    assert len(cast(list[object], calls[0][1]["attachments"])) == MAX_ATTACHMENTS_PER_MESSAGE
    thread.send.assert_awaited_once()
    assert thread.send.await_args.kwargs["file"].filename == "b.pdf"
    assert sorted(deleted) == ["file_a", "file_b"]


async def test_a_file_listed_after_the_turn_ended_is_left_for_the_next_sweep() -> None:
    later = NOW + timedelta(minutes=5)
    client, deleted = _client_with_files(
        ("file_mine", "brief.pdf", b"pdf-a", NOW),
        ("file_next", "next_turn.pdf", b"pdf-b", later),
    )
    thread = _answer_thread(_answer_message([]))
    answer, calls = _recording_edit(deleted)

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_window_after(NOW),
        answer=answer,
        sleep=_no_sleep,
    )

    names = [
        getattr(a, "filename", None)
        for _m, kwargs in calls
        for a in cast(list[object], kwargs["attachments"])
    ]
    assert names == ["brief.pdf"], "the next turn's file is not this answer's"
    thread.send.assert_not_awaited()
    assert deleted == ["file_mine"], "the next turn's file stays listed for its own sweep"


async def test_a_file_the_agent_already_sent_is_not_added_to_the_answer() -> None:
    client, deleted = _client_with_files(("file_chart", "chart.pdf", b"png-bytes", NOW))
    thread = _answer_thread(_answer_message([]))
    window = _window_after(NOW)
    agent_post = _posted(author_id=42, filename="chart.pdf", size=9)

    async def history(**kwargs: object):
        assert kwargs["after"].id == window[0]
        yield agent_post

    thread.history = MagicMock(side_effect=history)
    edit = AsyncMock()

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=window,
        answer=AnswerMessage(message_id=_ANSWER_ID, edit=edit),
        sleep=_no_sleep,
    )

    edit.assert_not_awaited()
    thread.send.assert_not_awaited()
    assert deleted == ["file_chart"], "the agent's own post delivered it"


async def test_an_answer_that_cannot_be_fetched_falls_back_to_a_post() -> None:
    client, deleted = _client_with_files(("file_a", "a.pdf", b"pdf-a", NOW))
    thread = _answer_thread(_answer_message([]))
    thread.fetch_message = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "Unknown Message")
    )
    edit = AsyncMock()

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_window_after(NOW),
        answer=AnswerMessage(message_id=_ANSWER_ID, edit=edit),
        sleep=_no_sleep,
    )

    edit.assert_not_awaited()
    thread.send.assert_awaited_once()
    assert deleted == ["file_a"]


async def test_a_file_indexed_seconds_after_the_turn_ended_is_still_attached() -> None:
    """created_at is the index time, about 5 s after the write, so a file written
    in the turn's last seconds is stamped after it ended and must still go out."""
    turn_end = NOW
    client, deleted = _client_with_files(
        ("file_last", "brief.pdf", b"pdf-a", turn_end + timedelta(seconds=5)),
        ("file_next", "next_turn.pdf", b"pdf-b", turn_end + TURN_END_GRACE),
    )
    thread = _answer_thread(_answer_message([]))
    answer, calls = _recording_edit(deleted)

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=(777, discord.utils.time_snowflake(turn_end, high=True)),
        answer=answer,
        sleep=_no_sleep,
    )

    names = [
        getattr(a, "filename", None)
        for _m, kwargs in calls
        for a in cast(list[object], kwargs["attachments"])
    ]
    assert names == ["brief.pdf"], "a file indexed inside the grace is this turn's"
    assert deleted == ["file_last"], "one indexed past the grace waits for the next sweep"


class _ServerAnswer:
    """Discord's copy of the answer: an edit applies here even when its response fails."""

    def __init__(self, fail_after_apply: list[BaseException], *, apply: bool = True) -> None:
        self.attachments: list[object] = []
        self._fail = fail_after_apply
        self._apply = apply
        self.edits = 0

    def message(self) -> MagicMock:
        return _answer_message(list(self.attachments))

    async def fetch(self, message_id: int) -> MagicMock:
        assert message_id == _ANSWER_ID
        return self.message()

    async def edit(self, message: object, **kwargs: object) -> discord.Message:
        self.edits += 1
        assert kwargs["_allow_replacement"] is False
        if self._apply:
            new: list[object] = []
            for item in cast(list[object], kwargs["attachments"]):
                if isinstance(item, discord.File):
                    attachment = _attachment(item.filename)
                    attachment.size = len(item.fp.read())
                    new.append(attachment)
                else:
                    new.append(item)
            self.attachments = new
        if self._fail:
            raise self._fail.pop(0)
        return cast(discord.Message, self.message())


def _server_error(status: int) -> discord.HTTPException:
    return discord.DiscordServerError(MagicMock(status=status, reason="Gateway Timeout"), "")


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), _server_error(504)],
    ids=["timeout", "504"],
)
async def test_an_edit_that_applied_before_failing_counts_and_keeps_the_file(
    failure: BaseException,
) -> None:
    """The response failed but Discord applied the edit: no second copy, and the
    next edit starts from the answer as it now stands, so the file stays on it."""
    client, deleted = _client_with_files(
        ("file_a", "a.pdf", b"pdf-a", NOW), ("file_b", "b.pdf", b"pdf-bb", NOW)
    )
    server = _ServerAnswer([failure])
    thread = _answer_thread(server.message())
    thread.fetch_message = AsyncMock(side_effect=server.fetch)

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_WINDOW,
        answer=AnswerMessage(message_id=_ANSWER_ID, edit=server.edit),
        sleep=_no_sleep,
    )

    assert sorted(a.filename for a in cast(list[MagicMock], server.attachments)) == [
        "a.pdf",
        "b.pdf",
    ], "each file is on the answer exactly once"
    thread.send.assert_not_awaited()
    assert sorted(deleted) == ["file_a", "file_b"]


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(), _server_error(504)],
    ids=["timeout", "504"],
)
async def test_an_edit_that_never_applied_falls_back_to_one_post(failure: BaseException) -> None:
    client, deleted = _client_with_files(("file_a", "a.pdf", b"pdf-a", NOW))
    server = _ServerAnswer([failure], apply=False)
    thread = _answer_thread(server.message())
    thread.fetch_message = AsyncMock(side_effect=server.fetch)

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=_WINDOW,
        answer=AnswerMessage(message_id=_ANSWER_ID, edit=server.edit),
        sleep=_no_sleep,
    )

    assert server.attachments == [], "the answer never got it"
    thread.send.assert_awaited_once()
    assert thread.send.await_args.kwargs["file"].filename == "a.pdf"
    assert deleted == ["file_a"]


async def test_a_file_indexed_after_the_next_turn_started_waits_for_that_turn() -> None:
    """The grace ends early once the next turn on the session has started: a file
    indexed after that may be the next turn's, so its sweep delivers it."""
    turn_end = NOW
    client, deleted = _client_with_files(
        ("file_mine", "brief.pdf", b"pdf-a", turn_end + timedelta(seconds=1)),
        ("file_next", "next_turn.pdf", b"pdf-b", turn_end + timedelta(seconds=7)),
    )
    thread = _answer_thread(_answer_message([]))
    answer, calls = _recording_edit(deleted)
    next_start: list[datetime] = []

    async def settle(delay: float) -> None:
        # The next turn starts while this sweep is still settling.
        next_start[:] = [turn_end + timedelta(seconds=2)]

    await deliver_session_outputs(
        client,
        thread,
        session_id="sesn_1",
        may_post=_allowed,
        notice_thread_ids=set(),
        turn_window=(777, discord.utils.time_snowflake(turn_end, high=True)),
        answer=answer,
        next_turn_start=lambda: next_start[0] if next_start else None,
        sleep=settle,
    )

    names = [
        getattr(a, "filename", None)
        for _m, kwargs in calls
        for a in cast(list[object], kwargs["attachments"])
    ]
    assert names == ["brief.pdf"]
    assert deleted == ["file_mine"], "the file indexed after the next turn started stays listed"


async def test_the_sweep_reads_the_next_turns_start_when_it_runs() -> None:
    runtime = MagicMock()
    bot = DaimonBot(runtime=cast(DiscordRuntime, runtime), intents=discord.Intents.default())
    thread = _thread()
    captured: dict[str, object] = {}

    async def sweep(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    with patch("daimon.adapters.discord.bot.deliver_session_outputs", side_effect=sweep):
        await bot._sweep_session_outputs(None, thread, uuid.uuid4(), "sesn_1")  # pyright: ignore[reportPrivateUsage]

    read_start = cast(Callable[[], datetime | None], captured["next_turn_start"])
    assert read_start() is None
    bot._note_turn_start("sesn_1")  # pyright: ignore[reportPrivateUsage]
    started = read_start()
    assert started is not None, "a turn that starts after scheduling still bounds the sweep"
    bot._note_turn_start("sesn_2")  # pyright: ignore[reportPrivateUsage]
    assert read_start() == started, "another session's turn does not"
