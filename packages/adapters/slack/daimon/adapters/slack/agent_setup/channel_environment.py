"""This channel's environment, picked on Who answers where.

Workspace admins and this channel's admins get a select there. A pick
re-checks both live, and checks that the environment still exists before
anything is written. Slack has no roles, so a channel admin is a member the
channel's grant names.
"""

from __future__ import annotations

import uuid
from typing import Final

import anthropic
import structlog
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.answering_map import AnsweringMap
from daimon.core.channel_admins import ChannelAdminCaller, is_channel_admin
from daimon.core.channel_environments import (
    EnvironmentPicker,
    build_clear_environment_note,
    build_set_environment_note,
    list_environment_names,
    parse_environment_option,
    plan_environment_picker,
    save_scope_environment,
)
from daimon.core.defaults.ma_index import find_environment_by_daimon_tag
from daimon.core.stores.channel_admins import get_channel_admins
from daimon.core.stores.identity import get_or_create_platform_principal

log = structlog.get_logger()

MAX_ENVIRONMENT_OPTIONS: Final = 99
"""A static select holds 100 options; the first is always Use the default."""
_MAX_OPTION_VALUE: Final = 150
"""Slack's limit on an option's value."""
ENVIRONMENT_NEED_ADMIN_MESSAGE: Final = (
    "Picking this channel's environment needs a workspace admin or an admin of this channel."
)


async def may_pick_environment(
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, channel_id: str, user_id: str, is_admin: bool
) -> bool:
    """Workspace admin, or a member this channel's grant names. `is_admin` is resolved live."""
    if is_admin:
        return True
    async with runtime.sessionmaker() as session:
        grant = await get_channel_admins(
            session, tenant_id=tenant_id, platform="slack", channel_id=channel_id
        )
    return is_channel_admin(ChannelAdminCaller(platform_user_id=user_id), grant=grant)


async def load_environment_picker(
    runtime: SlackRuntime,
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
            runtime, tenant_id=tenant_id, channel_id=channel_id, user_id=user_id, is_admin=False
        )
    ):
        return None
    try:
        names = await list_environment_names(runtime.anthropic, tenant_id=tenant_id)
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
    runtime: SlackRuntime, *, tenant_id: uuid.UUID, channel_id: str, user_id: str, value: str
) -> str:
    """Write the pick for `channel_id` and return what to tell the reader.

    The caller has re-checked the caller live. A value no picker offers, or an
    environment that no longer exists, writes nothing.
    """
    try:
        name = parse_environment_option(value)
    except ValueError:
        return "That is not an environment this panel offers. Nothing changed."
    if name is not None and (
        await find_environment_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=name)
        is None
    ):
        return f"The {name} environment no longer exists. Nothing changed."
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
    log.info("slack.agent_setup.channel_environment.saved", cleared=name is None)
    if name is None:
        return build_clear_environment_note(
            channel=f"<#{channel_id}>", cleared=previous is not None
        )
    return build_set_environment_note(environment_name=name, channel=f"<#{channel_id}>")
