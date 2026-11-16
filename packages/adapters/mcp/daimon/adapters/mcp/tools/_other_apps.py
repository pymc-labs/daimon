"""The `platform` a tool call passes to `scrub_other_platforms`."""

from __future__ import annotations

import discord
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.thread_sessions import get_thread_parent_channel
from daimon.core.turn.other_apps import places_scrub_platform
from fastmcp.exceptions import ToolError

_TEAMS_THREAD = ";messageid="


async def _discord_parent(runtime: McpRuntime, ref: str) -> str | None:
    """A Discord thread's parent channel id; None for a plain channel. Raises when the
    channel can't be read, so the caller can't tell a thread from a channel."""
    try:
        async with rest_client(_require_bot_token(runtime)) as client:
            channel = await client.fetch_channel(int(ref))
    except (ToolError, discord.HTTPException, ValueError) as exc:
        raise LookupError(ref) from exc
    return str(channel.parent_id) if isinstance(channel, discord.Thread) else None


async def _destination(
    runtime: McpRuntime, auth: AuthIdentity, platform: str, ref: str, origin: TurnOriginRow | None
) -> FeaturePlace | None:
    """The place a tool posts to: a channel, or a thread in the forms the tools take (a
    Discord thread is a channel id, so its parent comes from the turn, the session or a
    channel lookup). None when a Discord ref can't be told from a thread: scrub."""
    tenant = auth.tenant_id
    if platform == "slack" and ":" in ref:
        return FeaturePlace.of(tenant, platform, None, ref)
    if platform == "teams" and _TEAMS_THREAD in ref:
        return FeaturePlace.of(tenant, platform, ref.split(_TEAMS_THREAD, 1)[0], ref)
    if origin is not None and ref == origin.thread_id:
        return FeaturePlace.of(tenant, platform, origin.parent_channel_id, ref)
    parent = None
    if platform == "discord":
        async with runtime.session_factory() as session:
            parent = await get_thread_parent_channel(
                session, tenant_id=tenant, platform=platform, thread_id=ref
            )
        if parent is None:
            try:
                parent = await _discord_parent(runtime, ref)
            except LookupError:
                return None
    # Unknown whether `ref` is a channel or a thread: ask as both, each tier reads its own.
    return FeaturePlace(tenant, platform, parent or ref, ref)


async def scrub_platform(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str | None = None,
    *,
    origin_context_id: str | None = None,
) -> str | None:
    """`auth.platform`, or None (names kept) when `other_app_names` is on both where the
    caller runs and where the call posts. The caller's place is the verified turn origin
    (its agent, channel and thread); a chat agent that names none keeps the scrub. Read
    once per call; a failed read keeps the scrub."""
    platform = auth.platform
    if platform is None:
        return None
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if origin is None and (auth.agent_id or auth.chat_agent_id) is not None:
        return platform
    caller = (
        FeaturePlace.of(
            auth.tenant_id,
            platform,
            origin.parent_channel_id,
            origin.thread_id,
            agent_id=origin.responder_ma_agent_id,
        )
        if origin is not None
        else FeaturePlace.of(auth.tenant_id, platform, None)
    )
    places = [caller]
    if channel_id:
        destination = await _destination(runtime, auth, platform, channel_id, origin)
        if destination is None:
            return platform
        places.append(destination)
    return await places_scrub_platform(runtime.session_factory, runtime.settings, *places)
