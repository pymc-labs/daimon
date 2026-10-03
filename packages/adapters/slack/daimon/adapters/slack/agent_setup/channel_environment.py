"""This channel's environment, picked on Who answers where.

Workspace admins and this channel's admins get a select there. A pick
re-checks both live through `authorize` (which keeps an unrestricted network
out of a channel admin's reach in a sealed channel), and checks that the
environment still exists before anything is written. A channel admin is a
member the channel's grant names, directly or through a user group.
"""

from __future__ import annotations

import functools
import uuid
from typing import Final

import anthropic
import structlog
from daimon.adapters.slack.channel_admin_groups import channel_admin_caller
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.answering_map import AnsweringMap
from daimon.core.authz import Subject
from daimon.core.channel_admins import load_live_subject
from daimon.core.channel_environments import (
    NOT_OFFERED_NOTE,
    EnvironmentPicker,
    authorize_environment_pick,
    build_clear_environment_note,
    build_limited_network_confirm,
    build_limited_network_refusal,
    build_missing_environment_note,
    build_set_environment_note,
    list_environment_names,
    load_panel_hidden_environment_names,
    may_pick_environment_in,
    parse_environment_option,
    plan_environment_picker,
    save_scope_environment,
)
from daimon.core.panel_audit import record_panel_write
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

MAX_ENVIRONMENT_OPTIONS: Final = 99
"""A static select holds 100 options; the first is always Use the default."""
_MAX_OPTION_VALUE: Final = 150
"""Slack's limit on an option's value."""
ENVIRONMENT_NEED_ADMIN_MESSAGE: Final = (
    "Picking this channel's environment needs a workspace admin or an admin of this channel."
)


async def load_picker_subject(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    is_admin: bool,
) -> Subject:
    """The caller as `authorize` sees them. `is_admin` is resolved live."""
    caller = await channel_admin_caller(
        runtime, client, tenant_id=tenant_id, user_id=user_id, is_admin=is_admin
    )
    async with runtime.sessionmaker() as session:
        return await load_live_subject(
            session, tenant_id=tenant_id, platform="slack", caller=caller
        )


async def may_pick_environment(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, channel_id: str, subject: Subject
) -> bool:
    """Workspace admin, or a member this channel's grant names."""
    async with runtime.sessionmaker() as session:
        return await may_pick_environment_in(
            session, tenant_id=tenant_id, subject=subject, channel_id=channel_id
        )


async def load_environment_picker(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    answering_map: AnsweringMap,
    channel_id: str,
    user_id: str | None,
    is_admin: bool,
) -> EnvironmentPicker | None:
    """The picker for `channel_id`, or None for a reader who may not change it.

    A failed environment listing hides the picker rather than the whole view.
    """
    if not channel_id:
        return None
    if not is_admin and (
        user_id is None
        or not await may_pick_environment(
            runtime,
            tenant_id=tenant_id,
            channel_id=channel_id,
            subject=await load_picker_subject(
                runtime, client, tenant_id=tenant_id, user_id=user_id, is_admin=False
            ),
        )
    ):
        return None
    try:
        async with runtime.sessionmaker() as session:
            hidden = await load_panel_hidden_environment_names(
                session,
                runtime.anthropic,
                tenant_id=tenant_id,
                channel_id=channel_id,
                is_admin=is_admin,
                default=runtime.deployment_default,
            )
        names = await list_environment_names(runtime.anthropic, tenant_id=tenant_id, hidden=hidden)
    except anthropic.APIError:
        log.warning("slack.agent_setup.environment_picker.list_failed", exc_info=True)
        return None
    return plan_environment_picker(
        answering_map,
        channel_id=channel_id,
        names=names,
        limit=MAX_ENVIRONMENT_OPTIONS,
        max_value_length=_MAX_OPTION_VALUE,
    )


async def save_environment_choice(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    user_id: str,
    subject: Subject,
    value: str,
) -> str:
    """Write the pick for `channel_id` and return what to tell the reader.

    `subject` is the caller as just re-checked live. A value no picker offers,
    an environment that no longer exists, or a pick `authorize` refuses writes
    nothing.
    """
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
    audit = functools.partial(
        record_panel_write,
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="environment",
    )
    if not pick.decision:
        await audit(outcome="denied", reason=f"authz:{pick.decision.reason}")
        if pick.decision.reason == "not_a_reader":
            return build_limited_network_refusal(environment_name=name)
        return ENVIRONMENT_NEED_ADMIN_MESSAGE
    if name is not None and pick.missing:
        return build_missing_environment_note(name)
    if pick.needs_confirm:
        await audit(outcome="denied", reason="needs_confirm")
        return build_limited_network_confirm(environment_name=name, panel=True)
    # The environment the network rule judged, not a second lookup by name.
    name = pick.environment_name or name
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
        previous = await save_scope_environment(
            session,
            tenant_id=tenant_id,
            channel_id=channel_id,
            environment_name=name,
            actor_account_id=actor.account_id,
        )
    await audit(outcome="allowed", reason="completed")
    log.info(
        "slack.agent_setup.channel_environment.saved",
        tenant_id=str(tenant_id),
        channel_id=channel_id,
        actor_account_id=str(actor.account_id),
        environment_name=name,
        previous_environment_name=previous,
    )
    if name is None:
        return build_clear_environment_note(
            channel=f"<#{channel_id}>", cleared=previous is not None
        )
    return build_set_environment_note(environment_name=name, channel=f"<#{channel_id}>")
