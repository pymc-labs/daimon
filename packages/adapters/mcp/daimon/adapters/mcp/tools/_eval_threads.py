"""`ThreadSource`s for Discord, Slack and Teams threads, over the read tools the caller already has.

Each reads through the same implementation and the same channel read policy (sealed channels,
channel-held keys, the Teams group-chat refusal) as `read_thread`; the caller sees nothing they
could not read there. A chat platform keeps no tool calls, so these are text
recordings. Files are fetched when the platform gives a link, else listed by name only.
"""

from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy
from daimon.adapters.mcp.tools.discord._read import (
    _parse_link_impl,  # pyright: ignore[reportPrivateUsage]
    _read_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._client import slack_web_client
from daimon.adapters.mcp.tools.slack._parse_link import (
    _slack_parse_link_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._read import (
    _slack_read_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.teams._read import (
    _teams_parse_link_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_read_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.evals.from_thread import (
    ThreadFile,
    ThreadMessage,
    ThreadNotFound,
    ThreadPerson,
    ThreadRecord,
    ThreadSource,
)
from fastmcp.exceptions import ToolError

MAX_MESSAGES: Final = 500
MAX_FILE_BYTES: Final = 20 * 1024 * 1024
_PAGE: Final = 100
_EPOCH: Final = datetime.fromtimestamp(0, UTC)


async def _fetch(url: str | None) -> bytes | None:
    """A file's bytes, or None when there is no link, it is too big or it cannot be fetched."""
    if not url:
        return None
    try:
        async with (
            httpx.AsyncClient(timeout=30, follow_redirects=True) as http,
            http.stream("GET", url) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data += chunk
                if len(data) > MAX_FILE_BYTES:
                    return None
            return bytes(data)
    except httpx.HTTPError:
        return None


async def _file(name: str, url: str | None, media_type: str | None = None) -> ThreadFile:
    guessed = media_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return ThreadFile(name=name, media_type=guessed, data=await _fetch(url))


def _time(value: str | None) -> datetime:
    if not value:
        return _EPOCH
    at = datetime.fromisoformat(value)
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _people(known: dict[str, ThreadPerson], person: ThreadPerson) -> None:
    known.setdefault(person.id, person)


class DiscordThreadSource:
    def __init__(
        self, runtime: McpRuntime, auth: AuthIdentity, read_policy: ChannelReadPolicy
    ) -> None:
        self._runtime, self._auth, self._policy = runtime, auth, read_policy

    async def thread(self, ref: str) -> ThreadRecord:
        try:
            link = _parse_link_impl(ref, caller_guild_id=self._auth.external_id)
        except ToolError:
            raise ThreadNotFound(ref) from None
        pages: list[list[ThreadMessage]] = []
        people: dict[str, ThreadPerson] = {}
        before: str | None = None
        count = 0
        # A thread's id is its starter message's id, so a message link may name the thread.
        for thread_id in dict.fromkeys([link.message_id or link.channel_id, link.channel_id]):
            try:
                while count < MAX_MESSAGES:
                    result = await _read_thread_impl(
                        self._runtime, self._auth, thread_id=thread_id, limit=_PAGE,
                        before=before, read_policy=self._policy,
                    )  # fmt: skip
                    batch: list[ThreadMessage] = []
                    for row in result.rows:
                        bot = row.role == "assistant"
                        _people(
                            people,
                            ThreadPerson(
                                id=row.author_id, name=row.author_username or row.author_id,
                                handle=row.author_username or None, is_bot=bot,
                            ),
                        )  # fmt: skip
                        files = [await _file(a.filename, a.url) for a in row.attachments]
                        batch.append(
                            ThreadMessage(
                                id=row.id, author=row.author_id, at=_time(row.timestamp),
                                text=row.content, files=files,
                            )
                        )  # fmt: skip
                    pages.append(batch)
                    count += len(batch)
                    before = result.next_before
                    if before is None:
                        break
                break
            except ToolError:
                if thread_id == link.channel_id:
                    raise ThreadNotFound(ref) from None
                before = None
        messages = [m for page in reversed(pages) for m in page]
        return ThreadRecord(
            platform="discord", ref=ref, conversation="thread", people=list(people.values()),
            messages=messages,
            place_ids=[i for i in (link.guild_id, link.channel_id, link.message_id) if i],
        )  # fmt: skip


class SlackThreadSource:
    def __init__(
        self, runtime: McpRuntime, auth: AuthIdentity, read_policy: ChannelReadPolicy
    ) -> None:
        self._runtime, self._auth, self._policy = runtime, auth, read_policy

    async def thread(self, ref: str) -> ThreadRecord:
        team_id = self._auth.external_id
        if team_id is None:
            raise ThreadNotFound(ref)
        try:
            link = _slack_parse_link_impl(ref)
        except ToolError:
            raise ThreadNotFound(ref) from None
        thread_id = f"{link.channel_id}:{link.thread_ts or link.message_ts}"
        bot_client = await slack_web_client(self._runtime, team_id=team_id)
        me = await bot_client.auth_test()  # pyright: ignore[reportUnknownMemberType]
        bot_id = str(cast(dict[str, Any], me.data)["user_id"])  # pyright: ignore[reportUnknownMemberType]
        found: dict[str, ThreadMessage] = {}
        people: dict[str, ThreadPerson] = {}
        cursor: str | None = None
        while len(found) < MAX_MESSAGES:
            try:
                result = await _slack_read_thread_impl(
                    self._runtime, self._auth, thread_id=thread_id, limit=_PAGE, cursor=cursor,
                    read_policy=self._policy,
                )  # fmt: skip
            except ToolError:
                raise ThreadNotFound(ref) from None
            for row in result.messages:
                who = row.user_id or "unknown"
                _people(
                    people,
                    ThreadPerson(
                        id=who, name=row.username or who, handle=row.username, is_bot=who == bot_id
                    ),
                )  # fmt: skip
                files = [await _file(f.name, f.url, f.mimetype) for f in row.files]
                found[row.ts] = ThreadMessage(
                    id=row.ts, author=who, at=datetime.fromtimestamp(float(row.ts), UTC),
                    text=row.text, files=files,
                )  # fmt: skip
            cursor = result.next_cursor
            if not result.has_more or cursor is None:
                break
        messages = [found[ts] for ts in sorted(found, key=float)]
        return ThreadRecord(
            platform="slack", ref=ref, conversation="thread", people=list(people.values()),
            messages=messages, place_ids=[team_id, link.channel_id],
        )  # fmt: skip


class TeamsThreadSource:
    def __init__(
        self, runtime: McpRuntime, auth: AuthIdentity, read_policy: ChannelReadPolicy
    ) -> None:
        self._runtime, self._auth, self._policy = runtime, auth, read_policy

    async def thread(self, ref: str) -> ThreadRecord:
        try:
            link = _teams_parse_link_impl(ref)
        except ToolError:
            raise ThreadNotFound(ref) from None
        if link.thread_id is None:
            raise ThreadNotFound(ref)
        found: dict[str, ThreadMessage] = {}
        people: dict[str, ThreadPerson] = {}
        cursor: str | None = None
        while len(found) < MAX_MESSAGES:
            try:
                result = await _teams_read_thread_impl(
                    self._runtime, self._auth, thread_id=link.thread_id, limit=_PAGE,
                    cursor=cursor, read_policy=self._policy,
                )  # fmt: skip
            except ToolError:
                raise ThreadNotFound(ref) from None
            for row in result.messages:
                who = row.author_id or "unknown"
                _people(
                    people,
                    ThreadPerson(id=who, name=row.author_name or who, is_bot=row.is_bot),
                )  # fmt: skip
                files = [await _file(f.name, f.url) for f in row.files]
                found[row.id] = ThreadMessage(
                    id=row.id, author=who, at=_time(row.timestamp), text=row.text, files=files
                )  # fmt: skip
            cursor = result.next_cursor
            if cursor is None:
                break
        messages = sorted(found.values(), key=lambda m: m.at)
        return ThreadRecord(
            platform="teams", ref=ref, conversation="thread", people=list(people.values()),
            messages=messages, place_ids=[link.channel_id, link.thread_id],
        )  # fmt: skip


def thread_source(
    runtime: McpRuntime, auth: AuthIdentity, read_policy: ChannelReadPolicy
) -> ThreadSource:
    """The reader for the caller's own platform, under the caller's channel read policy."""
    sources = {
        "discord": DiscordThreadSource,
        "slack": SlackThreadSource,
        "teams": TeamsThreadSource,
    }
    source = sources.get(auth.platform or "")
    if source is None:
        raise ToolError("Cases can be made from Discord, Slack and Teams threads here.")
    return source(runtime, auth, read_policy)
