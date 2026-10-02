"""This channel's environment, picked on Who answers where.

Server admins and this channel's admins get a select that runs the channel in
one of the server's environments or hands it back to the default. What the
panel rendered is a hint: the select re-checks the caller live through
`authorize` (which keeps an unrestricted network out of a channel admin's
reach in a sealed channel), and the pick is checked against the environments
that still exist before anything is written.
"""

from __future__ import annotations

import uuid
from typing import Final

import anthropic
import structlog
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.checks import channel_admin_caller
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.authz import Subject
from daimon.core.channel_admins import load_live_subject
from daimon.core.channel_environments import (
    ENVIRONMENT_OPTION_INHERIT,
    NOT_OFFERED_NOTE,
    EnvironmentPicker,
    authorize_environment_pick,
    build_clear_environment_note,
    build_missing_environment_note,
    build_sealed_network_refusal,
    build_set_environment_note,
    environment_option_value,
    list_environment_names,
    may_pick_environment_in,
    parse_environment_option,
    plan_environment_picker,
    save_scope_environment,
)
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import PanelOutcome, record_panel_write

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


def panel_tenant_id(state: PanelState) -> uuid.UUID:
    """The tenant the panel's guild maps to."""
    return derive_tenant_uuid(platform="discord", workspace_id=str(state.guild_id))


async def load_picker_subject(
    interaction: discord.Interaction, *, runtime: DiscordRuntime, state: PanelState, live: bool
) -> Subject:
    """The caller as `authorize` sees them on this panel.

    ``live`` reads Manage Server off the member instead of the panel state;
    clicks pass it, rendering does not. Anyone else costs one grant read.
    """
    caller = channel_admin_caller(interaction.user)
    if not live:
        caller = caller.model_copy(update={"is_server_admin": state.is_admin})
    async with runtime.sessionmaker() as session:
        return await load_live_subject(
            session, tenant_id=panel_tenant_id(state), platform="discord", caller=caller
        )


async def may_pick_environment(
    subject: Subject, *, runtime: DiscordRuntime, state: PanelState
) -> bool:
    """Server admin, or an admin of the panel's channel."""
    async with runtime.sessionmaker() as session:
        return await may_pick_environment_in(
            session,
            tenant_id=panel_tenant_id(state),
            subject=subject,
            channel_id=str(state.channel_id),
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
    subject = await load_picker_subject(interaction, runtime=runtime, state=state, live=False)
    if not await may_pick_environment(subject, runtime=runtime, state=state):
        return None
    try:
        names = await list_environment_names(runtime.anthropic, tenant_id=panel_tenant_id(state))
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


async def audit_environment_pick(
    *, runtime: DiscordRuntime, state: PanelState, user_id: str, outcome: PanelOutcome, reason: str
) -> None:
    """One audit row for an environment pick on the panel's channel."""
    await record_panel_write(
        runtime.sessionmaker,
        tenant_id=panel_tenant_id(state),
        platform="discord",
        platform_user_id=user_id,
        op="environment",
        outcome=outcome,
        reason=reason,
    )


async def save_environment_choice(
    *, runtime: DiscordRuntime, state: PanelState, subject: Subject, user_id: str, value: str
) -> str:
    """Write the pick for the panel's channel and return what to tell the reader.

    `subject` is the caller as just re-checked live. A value no picker offers,
    an environment that no longer exists, or a pick `authorize` refuses writes
    nothing.
    """
    tenant_id = panel_tenant_id(state)
    channel_id = str(state.channel_id)
    try:
        name = parse_environment_option(value)
    except ValueError:
        return NOT_OFFERED_NOTE
    async with runtime.sessionmaker() as session:
        pick = await authorize_environment_pick(
            session,
            runtime.anthropic,
            tenant_id=tenant_id,
            subject=subject,
            channel_id=channel_id,
            environment_name=name,
            default=runtime.deployment_default,
        )
    if not pick.decision:
        await audit_environment_pick(
            runtime=runtime,
            state=state,
            user_id=user_id,
            outcome="denied",
            reason=f"authz:{pick.decision.reason}",
        )
        if pick.decision.reason == "sealed":
            return build_sealed_network_refusal(environment_name=name)
        return REFUSED_MESSAGE
    if name is not None and pick.missing:
        return build_missing_environment_note(name)
    # The environment the network rule judged, not a second lookup by name.
    name = pick.environment_name or name
    async with runtime.sessionmaker.begin() as session:
        previous = await save_scope_environment(
            session,
            tenant_id=tenant_id,
            channel_id=channel_id,
            environment_name=name,
            actor_account_id=state.account_id,
        )
    await audit_environment_pick(
        runtime=runtime, state=state, user_id=user_id, outcome="allowed", reason="completed"
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
