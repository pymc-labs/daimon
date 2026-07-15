"""Discord search_messages implementation.

All filters go server-side on ``GET /guilds/{guild_id}/messages/search``.
Thread hits survive visibility filtering via the response ``threads[]``
parent map (fallback per-hit resolution).  202 index-building bodies are
detected by body shape and raise a retry hint.
"""

from __future__ import annotations

from typing import Literal

import discord
import discord.http
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import AttachmentRow, MessageRow, SearchResult
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_thread_view,  # pyright: ignore[reportPrivateUsage]
    _check_view_permission,  # pyright: ignore[reportPrivateUsage]
)
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, ValidationError

# ---------------------------------------------------------------------------
# Strict response models (module-private)
# ---------------------------------------------------------------------------


class _SearchAuthor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    username: str
    bot: bool = False


class _SearchAttachment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    filename: str
    url: str
    size: int


class _SearchHit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    channel_id: str
    author: _SearchAuthor
    content: str = ""
    timestamp: str = ""
    attachments: list[_SearchAttachment] = []
    hit: bool = False


class _SearchThread(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    parent_id: str | None = None
    type: int


class _SearchResponse(BaseModel):
    """Strict parse of the guild-search success body.

    ``messages`` is required — a 202 body lacking it triggers ValidationError,
    which the caller converts to the retry ToolError.
    """

    model_config = ConfigDict(extra="ignore")

    messages: list[list[_SearchHit]]
    total_results: int
    threads: list[_SearchThread] | None = None


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------


async def _search_messages_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    content: str | None = None,
    channel_ids: list[str] | None = None,
    author_ids: list[str] | None = None,
    author_types: list[Literal["user", "bot", "webhook"]] | None = None,
    mentions: list[str] | None = None,
    has: list[Literal["image", "video", "file", "sticker", "embed", "link", "poll", "sound"]]
    | None = None,
    limit: int = 25,
    offset: int = 0,
) -> SearchResult:
    # 1. Auth gates
    user_id = _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    token = _require_bot_token(runtime)

    # Require at least one filter
    if (
        content is None
        and not channel_ids
        and not author_ids
        and not author_types
        and not mentions
        and not has
    ):
        raise ToolError("provide at least one search filter")

    # 2. Clamp limit/offset to the endpoint's real constraints
    clamped_limit = max(1, min(limit, 25))
    clamped_offset = max(0, min(offset, 9975))

    async with rest_client(token) as c:
        # 3. Resolve member FIRST (load-bearing ordering)
        guild, member = await _resolve_member(c, guild_id, user_id)

        # 4. Pre-validate channel_ids BEFORE the search route is hit.
        #    Any denial raises without leaking total_results. The guild check
        #    comes first because the admin early-exits in the view checks are
        #    keyed to the caller's perms in THIS guild and must not validate a
        #    channel belonging to another tenant's guild.
        if channel_ids:
            for ch_id in channel_ids:
                raw_ch = await _resolve_channel(c, ch_id)
                guild_ch = _require_guild_channel(raw_ch, guild_id)
                if isinstance(guild_ch, discord.Thread):
                    await _check_thread_view(c, guild_ch, member, user_id)
                else:
                    _check_view_permission(guild_ch, member)

        # 5. Build per-channel visibility data from fetch_channels.
        #    Absence does NOT mean invisible — it means "resolve it".
        channels = await guild.fetch_channels()
        view_by_channel_id: dict[str, bool] = {}
        manage_threads_by_channel_id: dict[str, bool] = {}
        for ch in channels:
            ch_id_str = str(ch.id)
            perms = ch.permissions_for(member)
            view_by_channel_id[ch_id_str] = perms.view_channel
            manage_threads_by_channel_id[ch_id_str] = perms.manage_threads

        # 6. Build server-side filter params
        params: dict[str, str | int | list[str]] = {
            "limit": clamped_limit,
            "offset": clamped_offset,
        }
        if content is not None:
            params["content"] = content
        if channel_ids:
            params["channel_id"] = [str(c_id) for c_id in channel_ids]
        if author_ids:
            params["author_id"] = [str(a_id) for a_id in author_ids]
        if author_types:
            params["author_type"] = list(author_types)
        if mentions:
            params["mentions"] = [str(m_id) for m_id in mentions]
        if has:
            params["has"] = list(has)

        route = discord.http.Route(
            "GET",
            "/guilds/{guild_id}/messages/search",
            guild_id=int(guild_id),
        )
        body: dict[str, object] = await c.http.request(route, params=params)  # type: ignore[assignment]  # discord.py http.request returns Any

        # 7. Parse strictly — fail loudly on unexpected shapes
        try:
            parsed = _SearchResponse.model_validate(body)
        except ValidationError as err:
            # 202 detection: body lacks "messages" and has 202 markers
            if "messages" not in body and (
                "retry_after" in body or "documents_indexed" in body or body.get("code") == 110000
            ):
                raise ToolError("Search index is building. Retry in a few seconds.") from err
            raise ToolError("unexpected search response format — please retry") from err

        # 8. Build thread parent map from response threads[]
        thread_by_id: dict[str, _SearchThread] = {}
        if parsed.threads:
            for t in parsed.threads:
                thread_by_id[t.id] = t

        # 9. Visibility filter per hit (cache decisions per channel_id)
        visible_hits: list[_SearchHit] = []
        view_cache: dict[str, bool] = {}

        for group in parsed.messages:
            # Hit selection: pick hit=true, fallback to group[0]
            hit = next((m for m in group if m.hit), group[0] if group else None)
            if hit is None:
                continue

            ch_id = hit.channel_id
            if ch_id in view_cache:
                if view_cache[ch_id]:
                    visible_hits.append(hit)
                continue

            can_view = False

            if ch_id in view_by_channel_id:
                # Channel is in fetch_channels — use the cached perm
                can_view = view_by_channel_id[ch_id]
            elif ch_id in thread_by_id:
                # Hit is in a thread from the response threads[] map
                thread_info = thread_by_id[ch_id]
                parent_id = thread_info.parent_id
                if parent_id:
                    # Check parent view permission
                    parent_view = view_by_channel_id.get(parent_id)
                    if parent_view is None:
                        # Parent not in fetch_channels — resolve it
                        try:
                            raw_parent = await _resolve_channel(c, parent_id)
                            if isinstance(raw_parent, (discord.abc.GuildChannel, discord.Thread)):
                                _check_view_permission(raw_parent, member)
                                parent_view = True
                            else:
                                parent_view = False
                        except ToolError:
                            parent_view = False
                    if parent_view:
                        # For private threads, additionally require manage_threads
                        # or thread membership
                        if thread_info.type == discord.ChannelType.private_thread.value:
                            manage = manage_threads_by_channel_id.get(parent_id, False)
                            if not manage and not member.guild_permissions.administrator:
                                # Try thread membership
                                try:
                                    raw_thread = await _resolve_channel(c, ch_id)
                                    if isinstance(raw_thread, discord.Thread):
                                        await raw_thread.fetch_member(int(user_id))
                                        can_view = True
                                    else:
                                        can_view = False
                                except (discord.NotFound, ToolError):
                                    can_view = False
                            else:
                                can_view = True
                        else:
                            can_view = True
            else:
                # Unknown channel id — resolve it (never default-invisible)
                try:
                    raw_ch = await _resolve_channel(c, ch_id)
                    if isinstance(raw_ch, discord.Thread):
                        try:
                            await _check_thread_view(c, raw_ch, member, user_id)
                            can_view = True
                        except ToolError:
                            can_view = False
                    elif isinstance(raw_ch, discord.DMChannel):
                        can_view = False
                    else:
                        _check_view_permission(raw_ch, member)
                        can_view = True
                except ToolError:
                    can_view = False

            view_cache[ch_id] = can_view
            if can_view:
                visible_hits.append(hit)

        # 10. Map visible hits → MessageRow
        rows: list[MessageRow] = []
        for hit in visible_hits:
            rows.append(
                MessageRow(
                    id=hit.id,
                    channel_id=hit.channel_id,
                    author_id=hit.author.id,
                    author_username=hit.author.username,
                    role="assistant" if hit.author.bot else "user",
                    content=hit.content,
                    timestamp=hit.timestamp,
                    attachments=[
                        AttachmentRow(
                            id=str(a.id),
                            filename=a.filename,
                            url=a.url,
                            size=a.size,
                        )
                        for a in hit.attachments
                    ],
                )
            )

        # 11. Envelope
        showing = len(rows)
        consumed = len(parsed.messages)
        # When channel_ids is not provided, total_results is the guild-wide
        # count from the bot's perspective — suppress it to avoid leaking the
        # existence/volume of messages in channels the caller cannot view.
        effective_total = parsed.total_results if channel_ids else showing
        hint: str | None = None
        if clamped_offset + consumed < parsed.total_results:
            if channel_ids:
                hint = (
                    f"More results available. Use offset={clamped_offset + consumed} to continue."
                )
            elif showing > 0:
                hint = (
                    f"More results may be available. "
                    f"Use offset={clamped_offset + consumed} to continue."
                )
            # Unscoped page with zero visible rows: stay silent. Any hint here
            # would reveal that hidden matches exist — the same count oracle
            # the suppressed total closes.

        return SearchResult(
            total_results=effective_total,
            showing=showing,
            offset=clamped_offset,
            rows=rows,
            hint=hint,
        )
