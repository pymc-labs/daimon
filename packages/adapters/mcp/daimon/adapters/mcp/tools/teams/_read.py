"""Teams channel reads: list_channels, read_channel, read_thread, get_message,
list_threads, parse_link and search_messages.

Messages come from Graph under the team's resource-specific consent
(`ChannelMessage.Read.Group`), so `_directory.require_readable` decides who
may read before any Graph call. A 1:1 chat cannot be read back: that needs a
tenant-wide permission daimon does not ask for. Cursors are Graph skip tokens,
applied only to the channel or thread they were checked for. Graph has no
message search for an app, so search_messages scans recent posts, bounded.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import ChannelReadPolicy
from daimon.adapters.mcp.tools.teams._directory import (
    TeamsChannelRef,
    channels_of,
    graph_for,
    installed_teams,
    locate_channel,
    require_client,
    require_readable,
    split_thread,
    thread_id,
)
from daimon.adapters.mcp.tools.teams._models import (
    TeamsChannelResult,
    TeamsChannelRow,
    TeamsParsedLink,
    TeamsPost,
    TeamsReadMessage,
    TeamsSearchMatch,
    TeamsSearchResult,
    TeamsThreadResult,
    TeamsThreadRow,
)
from daimon.core.teams_graph import (
    CARD_ATTACHMENT_TYPE,
    FILE_ATTACHMENT_TYPES,
    MAX_PAGE,
    GraphMessage,
    GraphUnavailable,
    image_sources,
    message_text,
    skiptoken_of,
)
from fastmcp.exceptions import ToolError

_FILES = FILE_ATTACHMENT_TYPES
_MAX_TEXT = 4_000
_PREVIEW = 200
_MAX_SEARCH_RESULTS = 25
# Bounds one search: channels scanned, and pages of posts (with replies) per channel.
_SEARCH_CHANNELS = 10
_SEARCH_PAGES = 2
_LINK_HOSTS = frozenset({"teams.microsoft.com", "teams.cloud.microsoft"})
_CHAT = "a 1:1 chat cannot be read back on Teams; its recent messages are already in your context"


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _order(message: GraphMessage) -> int:
    # Teams message ids are epoch milliseconds.
    return int(message.id) if message.id.isdigit() else 0


def _row(message: GraphMessage, *, channel_id: str, root_id: str) -> TeamsReadMessage | None:
    """The message as a row; None for a system event or a deleted message."""
    if message.message_type != "message" or message.deleted_date_time is not None:
        return None
    user = message.sender.user if message.sender else None
    app = message.sender.application if message.sender else None
    author = user or app
    text = message_text(message)
    if not text and any(a.content_type == CARD_ATTACHMENT_TYPE for a in message.attachments):
        text = "[card]"
    html = message.body.content if message.body.content_type == "html" else None
    return TeamsReadMessage(
        id=message.id,
        thread_id=thread_id(channel_id, root_id),
        author_id=author.id if author else None,
        author_name=author.display_name if author else None,
        is_bot=user is None and app is not None,
        text=_clip(text),
        timestamp=message.created_date_time,
        subject=message.subject or None,
        files=[a.name or "file" for a in message.attachments if a.content_type in _FILES],
        images=len(image_sources(html)) if html else 0,
        web_url=message.web_url,
    )


def _post(message: GraphMessage, *, channel_id: str) -> TeamsPost | None:
    root = _row(message, channel_id=channel_id, root_id=message.id)
    if root is None:
        return None
    replies = [
        row
        for reply in sorted(message.replies, key=_order)
        if (row := _row(reply, channel_id=channel_id, root_id=message.id)) is not None
    ]
    return TeamsPost(
        **root.model_dump(), replies=replies, more_replies=message.replies_next_link is not None
    )


def _graph_refused(err: GraphUnavailable) -> ToolError:
    if err.status == 403:
        return ToolError(
            "Microsoft Graph refused the read: the app's permission to read this team's channel "
            "messages is missing. A team owner can re-add the app to grant it."
        )
    if err.status == 404:
        return ToolError("no such message or thread in that channel")
    if err.status == 429:
        return ToolError("Microsoft Graph is throttling reads; try again in a minute")
    return ToolError(f"Microsoft Graph could not be read ({err.status or err.reason})")


def _limit(limit: int) -> int:
    return max(1, min(limit, MAX_PAGE))


async def _located(
    runtime: McpRuntime,
    auth: AuthIdentity,
    conversation_id: str,
    read_policy: ChannelReadPolicy,
) -> tuple[TeamsChannelRef, str | None]:
    """The checked channel of a channel or thread id, and the thread's root."""
    client, caller = require_client(runtime, auth)
    if conversation_id.startswith("a:"):
        raise ToolError(_CHAT)
    channel, root = split_thread(conversation_id)
    ref = await locate_channel(runtime, auth, client, channel)
    target = conversation_id if root is not None else None
    await require_readable(client, ref, caller=caller, read_policy=read_policy, target=target)
    return ref, root


async def _teams_list_channels_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime, auth: AuthIdentity
) -> list[TeamsChannelRow]:
    """Channels of the teams daimon is in that the caller belongs to."""
    client, caller = require_client(runtime, auth)
    rows: list[TeamsChannelRow] = []
    for team in await installed_teams(runtime, auth):
        try:
            if not await client.is_member(team.team_id, caller):
                continue
            for ref in await channels_of(client, team):
                # A private or shared channel is listed only to its own members.
                if not ref.is_standard and not await client.is_member(ref.channel_id, caller):
                    continue
                rows.append(
                    TeamsChannelRow(
                        id=ref.channel_id,
                        name=ref.channel_name,
                        type=ref.channel_type,
                        team_id=ref.team_id,
                        team_name=ref.team_name,
                    )
                )
        except (httpx.HTTPError, ValueError):
            continue
    return rows


async def _teams_read_channel_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    limit: int,
    cursor: str | None,
    read_policy: ChannelReadPolicy,
) -> TeamsChannelResult:
    if split_thread(channel_id)[1] is not None:
        raise ToolError("that is a thread id: use read_thread")
    ref, _ = await _located(runtime, auth, channel_id, read_policy)
    client, _ = require_client(runtime, auth)
    try:
        page = await graph_for(client).list_channel_messages(
            ref.group_id, ref.channel_id, top=_limit(limit), expand_replies=True, skiptoken=cursor
        )
    except GraphUnavailable as err:
        raise _graph_refused(err) from err
    posts = [
        p
        for m in page.value
        if (p := _post(m, channel_id=ref.channel_id)) is not None
        and read_policy.allows(p.thread_id, ref.channel_id)
    ]
    posts.reverse()
    next_cursor = skiptoken_of(page.next_link)
    return TeamsChannelResult(
        channel_id=ref.channel_id,
        channel_name=ref.channel_name,
        team_name=ref.team_name,
        posts=posts,
        next_cursor=next_cursor,
        hint=(
            "Posts are ordered by their latest activity. Pass next_cursor as cursor for less "
            "recently active posts; read_thread pages a post's remaining replies."
            if next_cursor or any(p.more_replies for p in posts)
            else None
        ),
    )


async def _teams_read_thread_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    thread_id: str,
    limit: int,
    cursor: str | None,
    read_policy: ChannelReadPolicy,
) -> TeamsThreadResult:
    if thread_id.startswith("a:"):
        raise ToolError(_CHAT)
    root = split_thread(thread_id)[1]
    if root is None:
        raise ToolError("thread_id is <channel>;messageid=<root>; for a channel use read_channel")
    ref, _ = await _located(runtime, auth, thread_id, read_policy)
    client, _ = require_client(runtime, auth)
    graph = graph_for(client)
    try:
        replies = await graph.list_replies(
            ref.group_id, ref.channel_id, root, top=_limit(limit), skiptoken=cursor
        )
        messages = sorted(replies.value, key=_order)
        if cursor is None:
            messages.insert(0, await graph.get_message(ref.group_id, ref.channel_id, root))
    except GraphUnavailable as err:
        raise _graph_refused(err) from err
    rows = [
        row
        for message in messages
        if (row := _row(message, channel_id=ref.channel_id, root_id=root)) is not None
    ]
    next_cursor = skiptoken_of(replies.next_link)
    return TeamsThreadResult(
        thread_id=thread_id,
        messages=rows,
        next_cursor=next_cursor,
        hint="Older replies exist: pass next_cursor as cursor." if next_cursor else None,
    )


async def _teams_get_message_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    message_id: str,
    read_policy: ChannelReadPolicy,
) -> TeamsReadMessage:
    ref, root = await _located(runtime, auth, channel_id, read_policy)
    if root is None:
        # A post is its own thread's root, so a sealed thread's root stays sealed.
        read_policy.require(thread_id(ref.channel_id, message_id), ref.channel_id)
    client, _ = require_client(runtime, auth)
    try:
        message = await graph_for(client).get_message(
            ref.group_id, ref.channel_id, message_id, root_id=root
        )
    except GraphUnavailable as err:
        if err.status == 404 and root is None:
            raise ToolError(
                "no such post in that channel. A reply is addressed under its thread: pass "
                "channel_id=<channel>;messageid=<root>"
            ) from err
        raise _graph_refused(err) from err
    row = _row(message, channel_id=ref.channel_id, root_id=root or message.id)
    if row is None:
        raise ToolError("that message was deleted, or is a system event")
    return row


async def _teams_list_threads_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    read_policy: ChannelReadPolicy,
) -> list[TeamsThreadRow]:
    """The channel's most recently active posts: each post is a thread on Teams."""
    if split_thread(channel_id)[1] is not None:
        raise ToolError("that is a thread id: pass its channel")
    ref, _ = await _located(runtime, auth, channel_id, read_policy)
    client, _ = require_client(runtime, auth)
    try:
        page = await graph_for(client).list_channel_messages(
            ref.group_id, ref.channel_id, top=MAX_PAGE, expand_replies=True
        )
    except GraphUnavailable as err:
        raise _graph_refused(err) from err
    rows: list[TeamsThreadRow] = []
    for message in page.value:
        post = _post(message, channel_id=ref.channel_id)
        if post is None or not read_policy.allows(post.thread_id, ref.channel_id):
            continue
        rows.append(
            TeamsThreadRow(
                thread_id=post.thread_id,
                subject=post.subject,
                preview=_clip(post.text, _PREVIEW),
                author_name=post.author_name,
                created_at=post.timestamp,
                reply_count=len(post.replies),
                more_replies=post.more_replies,
            )
        )
    return rows


def _teams_parse_link_impl(url: str) -> TeamsParsedLink:  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    """IDs from a Teams channel or message link (teams.microsoft.com/l/...)."""
    parts = urlsplit(url.strip())
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if parts.scheme != "https" or parts.hostname not in _LINK_HOSTS or len(segments) < 3:
        raise ToolError("not a Teams channel or message link (https://teams.microsoft.com/l/...)")
    if segments[0] != "l" or segments[1] not in ("channel", "message"):
        raise ToolError("not a Teams channel or message link (https://teams.microsoft.com/l/...)")
    channel = segments[2]
    if not channel.startswith("19:"):
        raise ToolError("that link does not name a Teams channel")
    if segments[1] == "channel":
        return TeamsParsedLink(
            link_type="channel", channel_id=channel, hint=f"read_channel(channel_id={channel!r})"
        )
    if len(segments) < 4 or not segments[3].isdigit():
        raise ToolError("that message link has no message id")
    message = segments[3]
    parent = (parse_qs(parts.query).get("parentMessageId") or [message])[0]
    root = parent if parent.isdigit() and parent != "0" else message
    thread = thread_id(channel, root)
    return TeamsParsedLink(
        link_type="message",
        channel_id=channel,
        message_id=message,
        thread_id=thread,
        hint=(
            f"read_thread(thread_id={thread!r}) for the whole thread, or "
            f"get_message(channel_id={thread!r}, message_id={message!r}) for the message"
        ),
    )


async def _searchable(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_ids: list[str] | None,
    read_policy: ChannelReadPolicy,
) -> list[TeamsChannelRef]:
    client, caller = require_client(runtime, auth)
    if channel_ids:
        if len(channel_ids) > _SEARCH_CHANNELS:
            raise ToolError(f"search at most {_SEARCH_CHANNELS} channels at once")
        refs: list[TeamsChannelRef] = []
        for channel_id in channel_ids:
            ref, root = await _located(runtime, auth, channel_id, read_policy)
            if root is not None:
                raise ToolError("search channel_ids takes channels, not threads")
            refs.append(ref)
        return refs
    refs = []
    for team in await installed_teams(runtime, auth):
        try:
            if not await client.is_member(team.team_id, caller):
                continue
        except (httpx.HTTPError, ValueError):
            continue
        refs += [
            ref
            for ref in await channels_of(client, team)
            if (ref.is_standard or ref.channel_id in read_policy.origin_channel_ids)
            and read_policy.allows(ref.channel_id)
        ]
    return refs


async def _teams_search_messages_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    content: str,
    channel_ids: list[str] | None,
    author_ids: list[str] | None,
    limit: int,
    read_policy: ChannelReadPolicy,
) -> TeamsSearchResult:
    needle = content.strip().casefold()
    if not needle:
        raise ToolError("content must not be empty")
    authors = {a.strip().lower() for a in author_ids or [] if a.strip()}
    refs = await _searchable(runtime, auth, channel_ids=channel_ids, read_policy=read_policy)
    client, _ = require_client(runtime, auth)
    graph = graph_for(client)
    matches: list[TeamsSearchMatch] = []
    scanned_posts = 0
    complete = len(refs) <= _SEARCH_CHANNELS
    for ref in refs[:_SEARCH_CHANNELS]:
        skiptoken: str | None = None
        for _ in range(_SEARCH_PAGES):
            try:
                page = await graph.list_channel_messages(
                    ref.group_id, ref.channel_id, expand_replies=True, skiptoken=skiptoken
                )
            except GraphUnavailable as err:
                if channel_ids:
                    raise _graph_refused(err) from err
                complete = False
                break
            for post in page.value:
                scanned_posts += 1
                for message in (post, *post.replies):
                    row = _row(message, channel_id=ref.channel_id, root_id=post.id)
                    if (
                        row is not None
                        and needle in f"{row.subject or ''}\n{row.text}".casefold()
                        and (not authors or (row.author_id or "").lower() in authors)
                        and read_policy.allows(row.thread_id, ref.channel_id)
                    ):
                        matches.append(
                            TeamsSearchMatch(
                                channel_id=ref.channel_id,
                                channel_name=ref.channel_name,
                                message=row,
                            )
                        )
                complete = complete and post.replies_next_link is None
            skiptoken = skiptoken_of(page.next_link)
            if skiptoken is None:
                break
        else:
            complete = complete and skiptoken is None
    matches.sort(key=lambda m: int(m.message.id) if m.message.id.isdigit() else 0, reverse=True)
    return TeamsSearchResult(
        matches=matches[: max(1, min(limit, _MAX_SEARCH_RESULTS))],
        scanned_channels=min(len(refs), _SEARCH_CHANNELS),
        scanned_posts=scanned_posts,
        complete=complete,
        hint=(
            None
            if complete
            else "Teams has no message search for apps: this scanned the most recently active "
            "posts only. Scope channel_ids, or read_channel with its cursor, to look further."
        ),
    )
