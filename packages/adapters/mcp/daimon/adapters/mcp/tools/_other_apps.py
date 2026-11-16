"""The `platform` a tool call passes to `scrub_other_platforms`."""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.thread_sessions import get_thread_parent_channel
from daimon.core.turn.other_apps import places_scrub_platform

_TEAMS_THREAD = ";messageid="


async def _destination(
    runtime: McpRuntime, auth: AuthIdentity, platform: str, ref: str, origin: TurnOriginRow | None
) -> FeaturePlace:
    """The place a tool posts to: a channel, or a thread in the forms the tools take (a
    Discord thread is a channel id, so its parent comes from the turn or the session)."""
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
        places.append(await _destination(runtime, auth, platform, channel_id, origin))
    return await places_scrub_platform(runtime.session_factory, runtime.settings, *places)
