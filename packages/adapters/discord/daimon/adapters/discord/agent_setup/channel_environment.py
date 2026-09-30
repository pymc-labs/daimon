"""This channel's environment, picked on Who answers where.

Server admins and this channel's admins get a select that runs the channel in
one of the server's environments or hands it back to the default. What the
panel rendered is a hint: the select re-checks the caller live, and the pick is
checked against the environments that still exist before anything is written.
"""

from __future__ import annotations

import uuid
from typing import Final

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import channel_admin_caller, is_guild_admin
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.channel_admins import is_channel_admin
from daimon.core.channel_environments import (
    ENVIRONMENT_OPTION_INHERIT,
    NOT_OFFERED_NOTE,
    EnvironmentPicker,
    build_clear_environment_note,
    build_missing_environment_note,
    build_set_environment_note,
    environment_option_value,
    list_environment_names,
    parse_environment_option,
    plan_environment_picker,
    save_scope_environment,
)
from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.channel_admins import get_channel_admins

import discord

log = structlog.get_logger()

MAX_ENVIRONMENT_OPTIONS: Final = 24
"""A select holds 25 options; the first is always Use the default."""
_MAX_OPTION_VALUE: Final = 100
"""Discord's limit on a select option's value."""
REFUSED_MESSAGE: Final = (
    "Picking this channel's environment needs Manage Server or an admin of this channel."
)


def build_environment_select(
    picker: EnvironmentPicker, *, channel_name: str | None
) -> discord.ui.Select[discord.ui.LayoutView]:
    """The select for ``picker``, current choice pre-selected. Pure — no I/O."""
    default_label = "Use the default" + (f" ({picker.inherited})" if picker.inherited else "")
    options = [
        discord.SelectOption(
            label=default_label[:100], value=ENVIRONMENT_OPTION_INHERIT, default=picker.own is None
        )
    ]
    options += [
        discord.SelectOption(
            label=name[:100], value=environment_option_value(name), default=name == picker.own
        )
        for name in picker.names
    ]
    return discord.ui.Select(
        placeholder=f"Environment for #{channel_name or 'this channel'}"[:150], options=options
    )


def _tenant_id(state: PanelState) -> uuid.UUID:
    return derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))


async def may_pick_environment(
    interaction: discord.Interaction, *, runtime: DiscordRuntime, state: PanelState, live: bool
) -> bool:
    """Server admin, or an admin of the panel's channel.

    ``live`` reads Manage Server off the interaction instead of the panel state;
    clicks pass it, rendering does not. Anyone else costs one grant read.
    """
    if is_guild_admin(interaction) if live else state.is_admin:  # pyright: ignore[reportArgumentType]  # only reads user/guild
        return True
    async with runtime.sessionmaker() as session:
        grant = await get_channel_admins(
            session,
            tenant_id=_tenant_id(state),
            platform="discord",
            channel_id=str(state.channel_id),
        )
    return grant is not None and is_channel_admin(
        channel_admin_caller(interaction.user), grant=grant
    )


async def load_environment_picker(
    interaction: discord.Interaction, *, runtime: DiscordRuntime, state: PanelState
) -> EnvironmentPicker | None:
    """The picker for the panel's channel, or None for a reader who may not change it.

    A failed environment listing hides the picker rather than the whole screen.
    """
    answering_map = state.answering_map
    if not state.channel_id or answering_map is None:
        return None
    if not await may_pick_environment(interaction, runtime=runtime, state=state, live=False):
        return None
    try:
        names = await list_environment_names(runtime.anthropic, tenant_id=_tenant_id(state))
    except anthropic.APIError:
        log.warning("agent_setup.environment_picker.list_failed", exc_info=True)
        return None
    return plan_environment_picker(
        answering_map,
        channel_id=str(state.channel_id),
        names=names,
        limit=MAX_ENVIRONMENT_OPTIONS,
        max_value_length=_MAX_OPTION_VALUE,
    )


async def save_environment_choice(*, runtime: DiscordRuntime, state: PanelState, value: str) -> str:
    """Write the pick for the panel's channel and return what to tell the reader.

    The caller has re-checked the caller live. A value no picker offers, or an
    environment that no longer exists, writes nothing.
    """
    tenant_id = _tenant_id(state)
    channel_id = str(state.channel_id)
    try:
        name = parse_environment_option(value)
    except ValueError:
        return NOT_OFFERED_NOTE
    if name is not None and (
        await find_environment_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
        is None
    ):
        return build_missing_environment_note(name)
    async with runtime.sessionmaker.begin() as session:
        previous = await save_scope_environment(
            session,
            tenant_id=tenant_id,
            channel_id=channel_id,
            environment_name=name,
            actor_account_id=state.account_id,
        )
    log.info(
        "agent_setup.channel_environment.saved",
        tenant_id=str(tenant_id),
        channel_id=channel_id,
        actor_account_id=str(state.account_id),
        environment_name=name,
        previous_environment_name=previous,
    )
    if name is None:
        return build_clear_environment_note(
            channel=f"<#{channel_id}>", cleared=previous is not None
        )
    return build_set_environment_note(environment_name=name, channel=f"<#{channel_id}>")
