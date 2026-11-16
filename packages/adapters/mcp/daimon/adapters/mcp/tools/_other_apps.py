"""The `platform` a tool call passes to `scrub_other_platforms`."""

from __future__ import annotations

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.turn.other_apps import place_scrub_platform


async def scrub_platform(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str | None = None
) -> str | None:
    """`auth.platform`, or None (names kept) when `other_app_names` is on for the call's
    workspace and channel. Read once per call; a failed read keeps the scrub."""
    if auth.platform is None:
        return None
    place = FeaturePlace.of(auth.tenant_id, auth.platform, channel_id)
    return await place_scrub_platform(runtime.session_factory, runtime.settings, place)
