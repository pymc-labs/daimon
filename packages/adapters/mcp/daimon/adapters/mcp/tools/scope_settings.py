"""The Settings group from chat: `get_settings` (anyone) and `change_setting` (carded).

Both read the place from the verified turn origin, never from a tool argument: the
model chooses only the key, the level (thread, channel or server) of where it is and
the value. A change waits for the requester's Approve card (`CARDED_TOOLS`) and is
written by `daimon.core.scope_settings.change_setting`, the writer `/settings` and the
web app use, which re-checks the rule for the key: approvals need a native platform
admin checked live, after the card was pressed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._card import NEEDS_CARD, require_card
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.admin_actions import DelegatedAction
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import StaleChannelValue
from daimon.core.scope_settings import (
    SettingKey,
    SettingLevel,
    SettingsActor,
    SettingsDefaults,
    SettingsPlace,
    SettingView,
    change_refusal,
    change_setting,
    read_settings,
    setting_refusal,
)
from daimon.core.stores.domain import TurnOriginRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

TOOL_NAME: Final = "change_setting"
NO_PLACE: Final = (
    "Settings are read and changed from a chat conversation with a person. Nothing changed."
)


@dataclass(frozen=True)
class LevelSetting:
    level: str
    """``server``, ``channel`` or ``thread``."""
    value: str
    """The level's own value; ``inherit`` when it sets none."""


@dataclass(frozen=True)
class Setting:
    key: str
    name: str
    levels: list[LevelSetting]
    effective: str
    """The value in effect here."""
    decided_by: str
    """The level in effect, or ``default``."""
    available: bool
    active: bool
    """False for approvals until the operator enables approval overrides."""
    note: str
    """One sentence to read back."""


@dataclass(frozen=True)
class SettingsResult:
    settings: list[Setting]


@dataclass(frozen=True)
class SettingChanged:
    key: str
    level: str
    before: str
    after: str
    effective: str
    note: str
    """The sentence to read back to the person."""


def _setting(view: SettingView) -> Setting:
    return Setting(
        key=view.key,
        name=view.name,
        levels=[LevelSetting(item.level, item.own) for item in view.levels],
        effective=view.effective,
        decided_by=view.decided_by,
        available=view.available,
        active=view.active,
        note=view.note,
    )


def _place(origin: TurnOriginRow) -> SettingsPlace:
    return SettingsPlace.of(origin.platform, origin.parent_channel_id, origin.thread_id)


def settings_actor(auth: AuthIdentity) -> SettingsActor:
    """The caller as the settings writer sees it: the token's own person, never an argument."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return auth.admin_for(action)

    return SettingsActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        surface="chat",
        native_user_id=auth.platform_user_id,
        admin_for=admin_for,
        via_agent_key=auth.is_external_agent,
    )


def _refuse_non_person(auth: AuthIdentity) -> None:
    """Step one: only a person in an attended chat, with a card to press, may change one."""
    if (
        auth.agent_id is not None
        or auth.is_external_agent
        or auth.routine_id is not None
        or auth.execution_id is not None
        or auth.hold is not None
        or auth.client
        or auth.no_confirmation_surface
        or auth.platform_user_id is None
    ):
        raise ToolError(NEEDS_CARD)


async def _get_settings_impl(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> SettingsResult:
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if origin is None:
        raise ToolError(NO_PLACE)
    place = _place(origin)
    views = await read_settings(
        runtime.session_factory,
        tenant_id=auth.tenant_id,
        place=place,
        defaults=SettingsDefaults.from_settings(runtime.settings, place.platform),
    )
    return SettingsResult([_setting(view) for view in views])


async def _change_setting_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    key: str,
    level: str,
    value: str,
    origin_context_id: str | None,
) -> SettingChanged:
    """Every check before any write, in order; the writer re-checks the key's rule."""
    _refuse_non_person(auth)
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if origin is None or not origin.attended:
        raise ToolError(NEEDS_CARD)
    place = _place(origin)
    refusal = setting_refusal(key, level, value, place=place)
    if refusal is not None:
        raise ToolError(str(refusal))
    actor = settings_actor(auth)
    early = await change_refusal(
        runtime.session_factory,
        runtime.settings,
        actor,
        key=cast(SettingKey, key),
        level=cast(SettingLevel, level),
        platform=place.platform,
    )
    if early is not None:
        raise ToolError(str(early))
    await require_card(runtime, auth, tool_name=TOOL_NAME, origin_context_id=origin_context_id)
    try:
        change = await change_setting(
            runtime.session_factory,
            runtime.settings,
            actor,
            key=key,
            level=level,
            value=value,
            place=place,
        )
    except (ChannelChangeRefused, StaleChannelValue) as exc:
        raise ToolError(str(exc)) from exc
    return SettingChanged(
        key=change.key,
        level=change.level,
        before=change.before,
        after=change.after,
        effective=change.view.effective,
        note=change.note,
    )


def register_scope_settings_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def get_settings(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, origin_context_id: str
    ) -> SettingsResult:
        """Report this conversation's settings: approvals, thread participation, progress
        plans and the cost footer.

        Each setting lists its own value at the server, channel and thread level of where
        you are, and what is in effect. Anyone may read them. Read each ``note`` back.
        People change them with ``/settings``, the web app's Settings section or
        ``change_setting``.
        """
        return await _get_settings_impl(runtime, await _auth(ctx), origin_context_id)

    @mcp.tool(name=TOOL_NAME, tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def change_setting_tool(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        key: Literal["approvals", "participation", "progress_plans", "cost_footer"],
        level: Literal["server", "channel", "thread"],
        value: Literal["on", "off", "disabled", "inherit", "shown", "hidden"],
    ) -> SettingChanged:
        """Change one setting where you are, after the person presses Approve.

        ``key`` and its values: ``approvals`` is ``on`` (Ask before third-party writes),
        ``off`` (Allow writes) or ``inherit``, and only a server admin of the platform
        itself may change it; ``participation`` (reply in threads without being
        addressed) and ``progress_plans`` are ``on``, ``off``, ``disabled`` (a channel or
        server lock) or ``inherit``, a thread's for anyone and a channel's or the
        server's for an admin; ``cost_footer`` is ``shown`` or ``hidden``, server level
        only, for an admin. ``level`` is the ``server``, ``channel`` or ``thread`` of
        this conversation; other places cannot be named. Read the returned ``note`` back.
        """
        return await _change_setting_impl(
            runtime,
            await _auth(ctx),
            key=key,
            level=level,
            value=value,
            origin_context_id=origin_context_id,
        )


__all__ = ["register_scope_settings_tools", "settings_actor"]
