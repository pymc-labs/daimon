"""Shared channel MCP tools with per-platform dispatch.

One registration serves every platform: ``auth.platform == "slack"`` routes to
``tools/slack/`` impls, ``"teams"`` to ``tools/teams/``, anything else to
``tools/discord/`` impls (which raise their own identity errors for non-discord
callers). Slack-unsupported tools raise a uniform ToolError.
"""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import (
    OPEN_READ_POLICY,
    ChannelReadPolicy,
    load_read_policy,
)
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.direct_messages import DirectMessageResult, send_direct_message_impl
from daimon.adapters.mcp.tools.discord import (
    ChannelRow,
    DisplayIdentityRow,
    MessageRow,
    ParsedLink,
    ReadChannelResult,
    ReadThreadResult,
    SearchResult,
    ThreadRow,
    _create_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _get_message_impl,  # pyright: ignore[reportPrivateUsage]
    _list_channels_impl,  # pyright: ignore[reportPrivateUsage]
    _list_threads_impl,  # pyright: ignore[reportPrivateUsage]
    _parse_link_impl,  # pyright: ignore[reportPrivateUsage]
    _read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _read_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _rename_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _search_messages_impl,  # pyright: ignore[reportPrivateUsage]
    _send_message_impl,  # pyright: ignore[reportPrivateUsage]
    _set_display_identity_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._models import (
    SlackChannelResult,
    SlackChannelRow,
    SlackMessageRow,
    SlackParsedLink,
    SlackSearchResult,
    SlackThreadResult,
)
from daimon.adapters.mcp.tools.slack._parse_link import (
    _slack_parse_link_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._read import (
    _slack_get_message_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_list_channels_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_read_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._search import (
    _slack_search_messages_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._send import (
    _slack_create_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _slack_send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.teams._models import (
    TeamsChannelResult,
    TeamsChannelRow,
    TeamsParsedLink,
    TeamsReadMessage,
    TeamsSearchResult,
    TeamsThreadResult,
    TeamsThreadRow,
)
from daimon.adapters.mcp.tools.teams._read import (
    _teams_get_message_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_list_channels_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_list_threads_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_parse_link_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_read_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_read_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_search_messages_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.teams._send import (
    TeamsMessageRow,
    _teams_create_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _teams_send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def _slack_unsupported(tool_name: str) -> ToolError:
    raise ToolError(f"{tool_name} is not supported on Slack yet")


async def _read_policy(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> ChannelReadPolicy:
    # Teams reads a private or shared channel only from inside it, so its
    # origin is resolved even when nothing is sealed.
    return await load_read_policy(
        runtime,
        auth,
        origin_context_id=origin_context_id,
        resolve_without_seals=auth.platform == "teams",
    )


def register_channel_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def send_direct_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, recipient_id: str, content: str
    ) -> DirectMessageResult:
        """Privately message one human member of the current server/workspace.

        Pass a platform user ID, not a channel or mention. Both sender and
        recipient must still belong to this tenant. Teams: the recipient's
        Entra object id; both of you must be in a team daimon is in, and the
        message arrives in their 1:1 chat with daimon. Tenant policy may disable
        delivery or restrict recipients to an allowlist. Plain text only, up to
        19000 characters, split into bounded messages. Returns all delivery IDs;
        a partial failure states how many messages were already sent, so do not
        blindly resend the whole message. No attachments or cross-tenant DMs.
        """
        return await send_direct_message_impl(
            runtime, await _auth(ctx), recipient_id=recipient_id, content=content
        )

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def list_channels(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str | None = None
    ) -> list[ChannelRow] | list[SlackChannelRow] | list[TeamsChannelRow]:
        """List channels in this server/workspace that you can view.

        From inside a confidential channel (its own agent, or origin_context_id
        placing this turn there) only that channel is listed. Teams: the
        channels of every team daimon is in that you belong to, with each
        channel's team. A team appears once daimon has seen activity in it
        since being added.
        """
        auth = await _auth(ctx)
        read_policy = await _read_policy(runtime, auth, origin_context_id)
        if auth.platform == "slack":
            return await _slack_list_channels_impl(runtime, auth, read_policy)
        if auth.platform == "teams":
            return await _teams_list_channels_impl(runtime, auth, read_policy)
        return await _list_channels_impl(runtime, auth, read_policy)

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def read_channel(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        limit: int = 50,
        before: str | None = None,
        cursor: str | None = None,
        origin_context_id: str | None = None,
    ) -> ReadChannelResult | SlackChannelResult | TeamsChannelResult:
        """Read channel messages, oldest-first, with pagination metadata.

        For threads use read_thread. Each platform takes only its own
        pagination parameter — the other is rejected. Discord: at most 200
        messages per call; use before to fetch older messages. Slack: use
        cursor to fetch the next page; at most 200 messages per call, and
        Slack may return a smaller page according to the workspace limits.
        Teams: channel_id is the channel (parent_channel_id in turn_controls);
        returns up to 50 posts with their replies, ordered by latest activity;
        pass next_cursor as cursor for less recently active posts. A 1:1 chat
        cannot be read back.

        A channel the workspace sealed is readable only from a conversation
        inside it: pass this turn's origin_context_id when reading one. On
        Teams that also holds for private and shared channels.
        """
        auth = await _auth(ctx)
        read_policy = await _read_policy(runtime, auth, origin_context_id)
        # MCP clients often send "" for an optional param they mean to omit —
        # treat it as absent, not as the wrong platform's cursor.
        before = before or None
        cursor = cursor or None
        if auth.platform == "teams":
            if before is not None:
                raise ToolError("before is Discord-only — pass cursor to paginate on Teams")
            return await _teams_read_channel_impl(
                runtime,
                auth,
                channel_id=channel_id,
                limit=limit,
                cursor=cursor,
                read_policy=read_policy,
            )
        if auth.platform == "slack":
            if before is not None:
                raise ToolError("before is Discord-only — pass cursor to paginate on Slack")
            return await _slack_read_channel_impl(
                runtime,
                auth,
                channel_id=channel_id,
                limit=limit,
                cursor=cursor,
                read_policy=read_policy,
            )
        if cursor is not None:
            raise ToolError("cursor is Slack/Teams-only — pass before to paginate on Discord")
        return await _read_channel_impl(
            runtime,
            auth,
            channel_id=channel_id,
            limit=limit,
            before=before,
            read_policy=read_policy,
        )

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def read_thread(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        thread_id: str,
        limit: int = 50,
        before: str | None = None,
        origin_context_id: str | None = None,
        cursor: str | None = None,
    ) -> ReadThreadResult | SlackThreadResult | TeamsThreadResult:
        """Read messages from a thread, oldest-first.

        Discord: thread_id is the thread's channel id; use before for older messages.
        Slack: thread_id is channel_id:thread_ts (e.g. C0123456789:1717171717.123456).
        Returns the root and the newest replies; pass next_cursor as cursor for
        older ones. At most 200 messages per call, the root included; a limit
        below 2 still reads the root and one reply. Slack may return fewer.
        Teams: thread_id is <channel>;messageid=<root> (thread_id
        in turn_controls); returns the root and the newest 50 replies; pass
        next_cursor as cursor for older ones. cursor is Slack/Teams-only. Threads
        under a sealed channel need origin_context_id, as read_channel does.
        """
        auth = await _auth(ctx)
        read_policy = await _read_policy(runtime, auth, origin_context_id)
        before = before or None
        cursor = cursor or None
        if auth.platform == "teams":
            if before is not None:
                raise ToolError("before is Discord-only — pass cursor to paginate on Teams")
            return await _teams_read_thread_impl(
                runtime,
                auth,
                thread_id=thread_id,
                limit=limit,
                cursor=cursor,
                read_policy=read_policy,
            )
        if auth.platform == "slack":
            if before is not None:
                raise ToolError("before is Discord-only — slack read_thread pages with cursor")
            return await _slack_read_thread_impl(
                runtime,
                auth,
                thread_id=thread_id,
                limit=limit,
                cursor=cursor,
                read_policy=read_policy,
            )
        if cursor is not None:
            raise ToolError("cursor is Slack/Teams-only — discord read_thread pages with before")
        return await _read_thread_impl(
            runtime,
            auth,
            thread_id=thread_id,
            limit=limit,
            before=before,
            read_policy=read_policy,
        )

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def get_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        message_id: str,
        origin_context_id: str | None = None,
    ) -> MessageRow | SlackMessageRow | TeamsReadMessage:
        """Fetch a single message by channel and message id (Slack: the message ts).

        Teams: a reply is addressed under its thread, channel_id
        <channel>;messageid=<root>; a post by its channel. Sealed channels
        need origin_context_id, as read_channel does.
        """
        auth = await _auth(ctx)
        read_policy = await _read_policy(runtime, auth, origin_context_id)
        if auth.platform == "teams":
            return await _teams_get_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                message_id=message_id,
                read_policy=read_policy,
            )
        if auth.platform == "slack":
            return await _slack_get_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                message_id=message_id,
                read_policy=read_policy,
            )
        return await _get_message_impl(
            runtime,
            auth,
            channel_id=channel_id,
            message_id=message_id,
            read_policy=read_policy,
        )

    @mcp.tool(tags={"discord", "teams"})  # pyright: ignore[reportArgumentType]
    async def list_threads(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        origin_context_id: str | None = None,
    ) -> list[ThreadRow] | list[TeamsThreadRow]:
        """List active and archived public threads for a channel.

        Archived private threads are not listed. Teams: every post is a
        thread; lists the 50 most recently active with their reply counts.
        Sealed channels need origin_context_id, as read_channel does.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            raise _slack_unsupported("list_threads")
        read_policy = await _read_policy(runtime, auth, origin_context_id)
        if auth.platform == "teams":
            return await _teams_list_threads_impl(
                runtime, auth, channel_id=channel_id, read_policy=read_policy
            )
        return await _list_threads_impl(
            runtime, auth, channel_id=channel_id, read_policy=read_policy
        )

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def create_thread(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        name: str,
        content: str,
    ) -> ThreadRow | SlackMessageRow | TeamsMessageRow:
        """Create a new thread and post content as its first message.

        ``content`` is required on every platform and becomes the thread's
        first message — Slack cannot open a thread without a root message,
        and a Discord forum post is rejected by the API without a starter
        message.

        Discord: ``name`` is the thread's title (1-100 characters). On a text
        channel this creates a PUBLIC thread; on a forum channel it creates a
        post. ``name`` is ignored on Slack and Teams.

        Slack: posts ``content`` as a channel-root message and returns its
        ``ts``. Combine that ``ts`` with ``channel_id`` to address the new
        thread afterward: ``send_message`` and ``read_thread`` both take the
        composite ``channel_id:ts`` form (e.g. if this call returns
        ``ts="1717171717.123456"`` for channel ``C0123456789``, reply with
        ``send_message(channel_id="C0123456789:1717171717.123456", ...)``).

        Teams: ``channel_id`` is the channel (``parent_channel_id`` in
        turn_controls, no ``;messageid=``); this starts a new post and returns
        its ``conversation_id``, which ``send_message`` takes to reply in it.
        Text only, and you must be a member of the channel.

        Only create a thread when the user asked for one. Refused in channels
        the workspace marked protected.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            return await _slack_create_thread_impl(
                runtime, auth, channel_id=channel_id, content=content
            )
        if auth.platform == "teams":
            return await _teams_create_thread_impl(
                runtime, auth, channel_id=channel_id, content=content
            )
        return await _create_thread_impl(
            runtime, auth, channel_id=channel_id, name=name, content=content
        )

    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def rename_thread(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        thread_id: str,
        name: str,
    ) -> ThreadRow:
        """Rename a Discord thread; ``name`` is the new title (1-100 characters).

        Use when the user asks to rename or retitle a thread, including the
        one you are chatting in — ``thread_id`` is the thread's channel id.
        Anyone who can post in a thread daimon opened may rename it; other
        threads need Manage Threads. Discord-only: Slack threads have no title.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            raise _slack_unsupported("rename_thread")
        return await _rename_thread_impl(runtime, auth, thread_id=thread_id, name=name)

    @mcp.tool(tags={"discord"})  # pyright: ignore[reportArgumentType]
    async def set_display_identity(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        display_name: str | None = None,
        avatar_url: str | None = None,
        origin_context_id: str | None = None,
    ) -> DisplayIdentityRow:
        """Change how daimon appears in this Discord server: its display name,
        its avatar, or both.

        Use when the user asks you to rename yourself or change your profile
        picture. ``display_name`` is the new nickname (1-32 characters).
        ``avatar_url`` is the URL of an image the user attached to a message
        (a cdn.discordapp.com or media.discordapp.net link); png, jpeg, gif
        and webp work. Both apply to the whole server: Discord has no
        per-channel identity, so tell the user when they asked for one
        channel. There is no reset yet: an empty ``display_name`` is treated
        as omitted, so a name cannot be cleared back to the default. Needs a
        server admin. Pass this turn's origin_context_id: a pinned agent, or a
        turn in a confidential channel, can't change it. Discord-only.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            raise _slack_unsupported("set_display_identity")
        # MCP clients often send "" for an optional param they mean to omit.
        return await _set_display_identity_impl(
            runtime,
            auth,
            display_name=display_name or None,
            avatar_url=avatar_url or None,
            origin_context_id=origin_context_id or None,
        )

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def parse_link(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        url: str,
    ) -> ParsedLink | SlackParsedLink | TeamsParsedLink:
        """Extract IDs from a channel or message link.

        Discord: supports discord.com, ptb.discord.com, and
        canary.discord.com URLs.
        - If link_type is "channel": use read_channel(channel_id)
        - If link_type is "message_or_thread": try read_thread(thread_id) first;
          if read_thread fails, it is a message: use get_message(channel_id, message_id)

        Slack: supports a workspace permalink
        (.../archives/<channel>/p<digits>). Returns channel_id, the dotted
        message ts, and thread_ts when the link is a reply. Try
        read_thread(thread_id=f"{channel_id}:{thread_ts or message_ts}")
        first; if that fails, use get_message(channel_id, message_ts).

        Teams: supports teams.microsoft.com/l/channel/... and /l/message/...
        links (teams.cloud.microsoft too). A message link returns its
        thread_id for read_thread and get_message.
        """
        auth = await _auth(ctx)
        if auth.platform == "slack":
            return _slack_parse_link_impl(url)
        if auth.platform == "teams":
            return _teams_parse_link_impl(url)
        return _parse_link_impl(url, caller_guild_id=auth.external_id)

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def send_message(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        content: str,
        attachments: list[dict[str, str]] | None = None,
        file_handles: list[str] | None = None,
    ) -> MessageRow | SlackMessageRow | TeamsMessageRow:
        """Post a message to a channel.

        For TEXT: only call this when the user explicitly asks you to send or
        post something to a channel. Do NOT call it to share results,
        summaries, or outputs unless the user specifically requested a post.
        Deliver text output in your reply instead.

        For FILES that rule does not apply, because a reply cannot carry an
        attachment. Posting a file in the thread you were invoked from is
        delivery, not a duplicate — asking you to "attach it here" is a
        request to call this tool with ``file_handles``. Read the two
        paragraphs above together or you will conclude, wrongly, that you
        should hand a file back "in your reply" and silently deliver nothing.

        ``file_handles=[handle_id, ...]`` references a file daimon is
        holding: to post a file you made in your sandbox, call
        ``create_file_upload_url`` first and PUT the bytes to the URL it
        returns — never base64 a file into a tool argument. Discord also
        takes ``attachments=[{url, filename}]``, fetched over https from
        Discord's own CDN hosts only (<=25 MiB each). Combined cap of 10
        files per message. Slack accepts file_handles and signed file-proxy
        links from its read tools in attachments (<=20 MiB each), for files
        the requester can read where they were shared. Other URLs are
        refused. Include a short caption: the text posts first, then files
        upload into that thread. If upload fails, the text remains posted;
        do not send it again. Requires the bot's files:write scope.

        Slack: ``channel_id`` may be ``channel_id:thread_ts`` (e.g.
        ``C0123456789:1717171717.123456``) to post into a thread. Content is
        sent as-is — nothing is escaped, so ``<@U…>`` mentions work — and is
        capped at 12,000 characters. daimon must already be in the channel
        (a member can run ``/invite @daimon``).

        Teams: ``channel_id`` is a conversation id — ``thread_id`` from
        turn_controls replies here; a 1:1 chat is ``a:…``, a channel thread
        ``19:…@thread.tacv2;messageid=…``. Markdown, capped at 6,000
        characters, and you must be a member of that conversation. In a
        channel, files are saved to its Files tab and the message links
        them; that needs the team's SharePoint site granted to daimon, and
        a private or shared channel takes none. In a 1:1 chat each file
        is a card the person accepts to save it to their OneDrive; content
        may be empty when sending files. A group chat takes no files.
        Channels the workspace marked protected, and threads under them,
        refuse every post — tell the caller rather than retrying elsewhere
        unasked.
        """
        auth = await _auth(ctx)
        if auth.platform == "teams":
            return await _teams_send_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                content=content,
                attachments=attachments,
                file_handles=file_handles,
            )
        if auth.platform == "slack":
            return await _slack_send_message_impl(
                runtime,
                auth,
                channel_id=channel_id,
                content=content,
                attachments=attachments,
                file_handles=file_handles,
                read_policy=(
                    await _read_policy(runtime, auth, None) if attachments else OPEN_READ_POLICY
                ),
            )
        return await _send_message_impl(
            runtime,
            auth,
            channel_id=channel_id,
            content=content,
            attachments=attachments,
            file_handles=file_handles,
        )

    _VALID_AUTHOR_TYPES = frozenset({"user", "bot", "webhook"})
    _VALID_HAS = frozenset({"image", "video", "file", "sticker", "embed", "link", "poll", "sound"})

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def search_messages(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        content: str | None = None,
        channel_ids: list[str] | None = None,
        author_ids: list[str] | None = None,
        author_types: list[str] | None = None,
        mentions: list[str] | None = None,
        has: list[str] | None = None,
        limit: int = 25,
        offset: int = 0,
        origin_context_id: str | None = None,
    ) -> SearchResult | SlackSearchResult | TeamsSearchResult:
        """Search messages with server-side filters.

        Limit caps at 25 per page — paginate with offset. When the search is
        scoped to channel_ids, total_results is the exact count for those
        channels (you must be able to view them; they are rejected before
        searching). Unscoped searches report only the visible rows — the
        server-wide count is withheld because it includes channels you cannot
        view. Slack: only content + limit are supported (other filters are
        Discord-only), and 1:1 DM hits are only returned in a DM with daimon.
        Teams: Microsoft has no message search for apps, so this scans the
        most recently active posts (and their replies) of up to 10 channels
        for content; only content, channel_ids, author_ids (Entra ids) and
        limit are supported, and complete=false says the scan stopped short.
        Hits in sealed channels are withheld unless origin_context_id places
        this turn inside that channel; scoping to one from outside is refused.
        """
        auth = await _auth(ctx)
        read_policy = await _read_policy(runtime, auth, origin_context_id)
        if auth.platform == "teams":
            if content is None:
                raise ToolError("teams search requires a content query")
            if any((author_types, mentions, has, offset)):
                raise ToolError(
                    "teams search supports only content, channel_ids, author_ids and limit"
                )
            return await _teams_search_messages_impl(
                runtime,
                auth,
                content=content,
                channel_ids=channel_ids,
                author_ids=author_ids,
                limit=limit,
                read_policy=read_policy,
            )
        if auth.platform == "slack":
            if content is None:
                raise ToolError("slack search requires a content query")
            if any((channel_ids, author_ids, author_types, mentions, has, offset)):
                raise ToolError(
                    "slack search supports only content and limit — other filters are Discord-only"
                )
            return await _slack_search_messages_impl(
                runtime, auth, content=content, limit=limit, read_policy=read_policy
            )
        # Validate enum-typed params at the tool boundary (shell) so the impl
        # receives the precise Literal types without type-ignore suppressions.
        if author_types and any(a not in _VALID_AUTHOR_TYPES for a in author_types):
            raise ToolError(f"invalid author_type; valid: {sorted(_VALID_AUTHOR_TYPES)}")
        if has and any(h not in _VALID_HAS for h in has):
            raise ToolError(f"invalid has value; valid: {sorted(_VALID_HAS)}")
        return await _search_messages_impl(
            runtime,
            auth,
            content=content,
            channel_ids=channel_ids,
            author_ids=author_ids,
            author_types=author_types,  # type: ignore[arg-type]  # validated above
            mentions=mentions,
            has=has,  # type: ignore[arg-type]  # validated above
            limit=limit,
            offset=offset,
            read_policy=read_policy,
        )
