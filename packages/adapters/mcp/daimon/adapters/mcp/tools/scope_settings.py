"""The Settings group from chat: `get_settings` (anyone) and `change_setting` (carded).

The place is always the verified turn origin, never an argument. A change waits for the
requester's Approve card (`CARDED_TOOLS`) and is written by the core `change_setting`,
which re-checks the key's rule after the press (approvals: a native admin, live).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.confirmed_call import card_satisfied
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import NEEDS_CARD, require_card
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import StaleChannelValue
from daimon.core.features.registry import Tier
from daimon.core.features.service import (
    FeatureActor,
    change_feature,
    change_refusal,
    feature_refusal,
    read_features,
)
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.features.surface import change_note, listed, view_json
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

TOOL_NAME: Final = "change_setting"
NO_PLACE: Final = (
    "Settings are read and changed from a chat conversation with a person. Nothing changed."
)


@dataclass(frozen=True)
class SettingsResult:
    settings: list[dict[str, Any]]


@dataclass(frozen=True)
class SettingChanged:
    setting: dict[str, Any]
    note: str


def settings_actor(auth: AuthIdentity) -> FeatureActor:
    """The token's own person, never an argument."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return auth.admin_for(action)

    return FeatureActor(
        auth.tenant_id,
        auth.account_id,
        "chat",
        auth.platform_user_id,
        admin_for,
        via_agent_key=auth.is_external_agent,
    )


async def _place(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None, *, change: bool
) -> FeaturePlace:
    """The verified origin's place. A change also needs a person at an attended chat with a
    card to press: agent keys, routines, executions, holds and clients are refused first."""
    if change and (
        auth.agent_id is not None
        or auth.is_external_agent
        or auth.routine_id is not None
        or auth.execution_id is not None
        or auth.hold is not None
        or auth.client
        or (auth.no_confirmation_surface and not card_satisfied(auth))
        or auth.platform_user_id is None
    ):
        raise ToolError(NEEDS_CARD)
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if origin is None or (change and not origin.attended):
        raise ToolError(NEEDS_CARD if change else NO_PLACE)
    return FeaturePlace.of(
        auth.tenant_id,
        origin.platform,
        origin.parent_channel_id,
        origin.thread_id,
        agent_id=origin.responder_ma_agent_id or None,
    )


async def _get_settings_impl(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> SettingsResult:
    place = await _place(runtime, auth, origin_context_id, change=False)
    views = await read_features(runtime.session_factory, runtime.settings, place)
    return SettingsResult([view_json(view, place.platform) for view in listed(views)])


async def _change_setting_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
    tier: Tier,
    value: str,
    origin_context_id: str | None,
    locked: bool = False,
) -> SettingChanged:
    """Every check before the card, in order; the writer re-checks the key's rule after it."""
    place = await _place(runtime, auth, origin_context_id, change=True)
    actor = settings_actor(auth)
    refusal = feature_refusal(
        key, tier, value, locked=locked, place=place, settings=runtime.settings
    ) or await change_refusal(
        runtime.session_factory,
        runtime.settings,
        actor,
        key=key,
        tier=tier,
        place=place,
        locked=locked,
        value=value,
    )
    if refusal is not None:
        raise ToolError(str(refusal))
    await require_card(runtime, auth, tool_name=TOOL_NAME, origin_context_id=origin_context_id)
    try:
        change = await change_feature(
            runtime.session_factory,
            runtime.settings,
            actor,
            key=key,
            tier=tier,
            value=value,
            place=place,
            locked=locked,
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        raise ToolError(str(exc)) from exc
    return SettingChanged(
        view_json(change.view, place.platform), change_note(change, place.platform)
    )


def register_scope_settings_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def get_settings(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str
    ) -> SettingsResult:
        """Report this conversation's settings (features set up here or with a saved value).

        Each lists its own value at the workspace, agent, channel and thread levels of where
        you are, what is in effect, which level decided it (``source``: ``default``,
        ``deployment`` for everywhere, ``workspace``, ``agent``, ``channel`` or ``thread``)
        and whether it is locked. Anyone may read them; read each ``summary`` back. People
        change them with ``/settings``, the web app's Settings section or ``change_setting``.
        """
        return await _get_settings_impl(runtime, await _auth(ctx), origin_context_id)

    @mcp.tool(name=TOOL_NAME, tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def change_setting_tool(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        key: str,
        tier: Literal["workspace", "agent", "channel", "thread"],
        value: str,
        locked: bool = False,
    ) -> SettingChanged:
        """Change one setting where you are, after the person presses Approve.

        ``key`` and ``value`` are one of ``get_settings``' keys and its values, or
        ``default`` to clear the level's own value. ``tier`` is the ``workspace``,
        ``agent``, ``channel`` or ``thread`` level of this conversation; other places cannot be
        named. ``locked`` (an admin's, not at the thread tier) makes the value final
        below. Approvals need a live admin of the chat platform; a thread's participation
        or progress plans are anyone's; the rest an admin's. Read the returned ``note``
        back.
        """
        return await _change_setting_impl(
            runtime,
            await _auth(ctx),
            key=key,
            tier=tier,
            value=value,
            origin_context_id=origin_context_id,
            locked=locked,
        )
