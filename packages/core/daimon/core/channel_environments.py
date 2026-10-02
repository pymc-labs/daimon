"""Channel environments: which environment a channel's turns run in.

The environment resolves over the same tiers as the agent (channel, then the
tenant default, then the deployment default; see `daimon.core.scope`) but on
its own, so a channel can keep the agent it has and run it with the packages
one team needs. Server admins set any channel's environment or the tenant
default; a channel admin sets the channels they run, except an environment
with unrestricted networking in a sealed channel (`authorize`'s
SET_CHANNEL_ENVIRONMENT). A scope with no environment of its own falls
through, so nothing changes until one is set.

The sentences and the setup panels' picker live here so the chat tools and
both panels say and offer the same. Only the select's length differs by
platform (`tests/parity/test_environment_select_caps.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment
from daimon.core.answering_map import AnsweringMap
from daimon.core.authz import Action, Decision, Place, Subject, authorize
from daimon.core.defaults.ma_index import (
    find_environment_by_daimon_tag,
    list_environments_by_tenant,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.scope import (
    ChannelScopeRef,
    ConfigTier,
    DeploymentDefault,
    ScopeContext,
    TenantScopeRef,
)
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.scoped_config_read import get_scope, resolve
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

ENVIRONMENT_OPTION_INHERIT: Final = "inherit"
"""The picker option that hands a channel back to the default."""
_ENVIRONMENT_OPTION_PREFIX: Final = "env:"
NOT_OFFERED_NOTE: Final = "That is not an environment this panel offers. Nothing changed."
"""A pick whose value no picker offered: forged or stale."""


def _scope_phrase(channel: str | None) -> str:
    return f"Channel {channel}" if channel is not None else "The workspace default"


def build_set_environment_note(*, environment_name: str, channel: str | None) -> str:
    """What changed after an environment is set, and when a conversation sees it.

    `channel` is the channel as the reader sees it (an id or a platform
    mention); None is the workspace default.
    """
    return (
        f"{_scope_phrase(channel)} now runs in the {environment_name} environment, from "
        "the next message in each conversation. The conversation and its saved files carry "
        "over; a running process does not."
    )


def build_missing_environment_note(environment_name: str) -> str:
    """A pick of an environment deleted since the picker was drawn."""
    return f"The {environment_name} environment no longer exists. Nothing changed."


def build_archive_environment_note(*, environment_name: str, cleared: int) -> str:
    """What archiving an environment did to the channels and workspace that picked it."""
    if cleared == 0:
        return f"Archived the {environment_name} environment. No channel or workspace picked it."
    picks = "1 pick" if cleared == 1 else f"{cleared} picks"
    return (
        f"Archived the {environment_name} environment and cleared {picks} of it; where it "
        "was picked, the next tier's environment applies from the next message."
    )


def build_clear_environment_note(*, channel: str | None, cleared: bool) -> str:
    """What changed after an environment is cleared; `cleared` False means nothing did."""
    if not cleared:
        return f"{_scope_phrase(channel)} had no environment of its own; nothing changed."
    return (
        f"{_scope_phrase(channel)} no longer picks an environment; from the next message "
        "it falls through to the next tier."
    )


def build_sealed_network_refusal(*, environment_name: str | None) -> str:
    """Why a channel admin's pick in a sealed channel was refused; None is a clear."""
    what = (
        f"the {environment_name} environment has"
        if environment_name is not None
        else "the default it would fall back to has"
    )
    return (
        f"This channel is sealed, and {what} unrestricted network access, so only a server "
        "admin can make that change here. Nothing changed. Pick an environment with limited "
        "networking, or ask a server admin."
    )


def build_environment_resolution_note(
    *, environment_name: str | None, tier: ConfigTier | None, channel_id: str
) -> str:
    """Which environment a turn in `channel_id` runs in, and the tier that chose it."""
    if environment_name is None:
        return (
            f"No environment resolves for channel {channel_id}, so a mention there cannot "
            "start. A server admin, or an admin of that channel, can pick one."
        )
    where = {
        "channel": "this channel's own setting",
        "tenant": "the workspace default, since this channel has none of its own",
        "deployment": "the deployment default, since neither this channel nor the workspace "
        "sets one",
    }.get(tier or "", "an unrecognised tier")
    return f"Turns in channel {channel_id} run in the {environment_name} environment, from {where}."


def environment_choices(
    names: Sequence[str], *, current: str | None, limit: int
) -> tuple[str, ...]:
    """At most `limit` names for a picker, keeping the current one when the list is cut."""
    listed = list(names[:limit])
    if current is not None and current in names and current not in listed:
        listed[-1] = current
    return tuple(listed)


def environment_option_value(name: str) -> str:
    """The picker option value that names environment `name`."""
    return f"{_ENVIRONMENT_OPTION_PREFIX}{name}"


def parse_environment_option(value: str) -> str | None:
    """The environment a picker option names, or None for the default.

    Raises ValueError for a value no picker offers.
    """
    if value == ENVIRONMENT_OPTION_INHERIT:
        return None
    if value.startswith(_ENVIRONMENT_OPTION_PREFIX) and len(value) > len(
        _ENVIRONMENT_OPTION_PREFIX
    ):
        return value.removeprefix(_ENVIRONMENT_OPTION_PREFIX)
    raise ValueError(f"not an environment option: {value!r}")


class EnvironmentPicker(BaseModel):
    """What a setup panel's environment select offers for one channel."""

    model_config = ConfigDict(frozen=True)

    channel_id: str
    own: str | None
    """The channel's own environment, or None while it uses the default."""
    inherited: str | None
    """What the channel runs in without one of its own."""
    names: tuple[str, ...]


def plan_environment_picker(
    answering_map: AnsweringMap,
    *,
    channel_id: str,
    names: Sequence[str],
    limit: int,
    max_value_length: int,
) -> EnvironmentPicker | None:
    """The picker for `channel_id`, or None when it would offer nothing. Pure.

    `limit` and `max_value_length` are the platform's select bounds; a name
    whose option value would not fit is left out rather than cut.
    """
    own, tier = answering_map.environment_in(channel_id)
    own = own if tier == "channel" else None
    fits = [name for name in names if len(environment_option_value(name)) <= max_value_length]
    if not fits and own is None:
        return None
    return EnvironmentPicker(
        channel_id=channel_id,
        own=own,
        inherited=answering_map.tenant_environment or answering_map.deployment_environment,
        names=environment_choices(fits, current=own, limit=limit),
    )


async def list_environment_names(client: AsyncAnthropic, *, tenant_id: uuid.UUID) -> list[str]:
    """The names a scope may pick in this tenant, de-duplicated and sorted case-insensitively.

    The name is the resolver's key (`daimon_name`), the same one a turn and a
    save look up; an environment without one cannot be picked, so is left out.
    """
    environments = await list_environments_by_tenant(client, tenant_id=tenant_id)
    names = {name for env in environments if (name := env.metadata.get(MA_METADATA_KEY_NAME))}
    return sorted(names, key=str.casefold)


def has_open_network(environment: BetaEnvironment) -> bool:
    """Anything but a cloud environment on limited networking.

    A self-hosted environment's network is its host's, unknown here, so it
    counts as open.
    """
    config = environment.config
    return config.type != "cloud" or config.networking.type != "limited"


async def may_pick_environment_in(
    session: AsyncSession, *, tenant_id: uuid.UUID, subject: Subject, channel_id: str | None
) -> bool:
    """Whether `subject` may pick `channel_id`'s environment at all (None: the workspace's).

    The network rule depends on the pick, so `authorize_environment_pick`
    decides it once one is made.
    """
    policy = await load_access_policy(session, tenant_id=tenant_id)
    return bool(
        authorize(
            policy,
            subject=subject,
            action=Action.SET_CHANNEL_ENVIRONMENT,
            place=Place(channel_id=channel_id),
        )
    )


async def authorize_environment_pick(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    channel_id: str | None,
    environment_name: str | None,
    default: DeploymentDefault,
) -> Decision:
    """Whether `subject` may set `channel_id`'s environment to `environment_name`.

    None clears it, leaving the channel on the workspace or deployment
    default, which then decides the network rule. Environments are looked up
    only when that rule is what decides: a channel admin in a sealed channel.
    One missing at set time is allowed here for the caller's own not-found
    refusal; a missing default counts as open.
    """
    policy = await load_access_policy(session, tenant_id=tenant_id)
    place = Place(channel_id=channel_id)

    def decide(*, open_network: bool) -> Decision:
        return authorize(
            policy,
            subject=subject,
            action=Action.SET_CHANNEL_ENVIRONMENT,
            place=place,
            open_network=open_network,
        )

    decision = decide(open_network=True)
    if decision or decision.reason != "sealed":
        return decision
    name = environment_name
    if name is None:
        fallback = await resolve(
            session, context=ScopeContext(tenant_id=tenant_id, channel_id=None), default=default
        )
        name = fallback.environment_name or "default"
    environment = await find_environment_by_daimon_tag(client, tenant_id=tenant_id, name=name)
    if environment is None:
        return decide(open_network=environment_name is None)
    return decide(open_network=has_open_network(environment))


async def save_scope_environment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    channel_id: str | None,
    environment_name: str | None,
    actor_account_id: uuid.UUID | None,
) -> str | None:
    """Set a channel's environment (the tenant default with no channel), or clear it with None.

    Returns the environment the scope named before. The caller has already
    checked that the environment exists and that the actor may change the scope.
    """
    scope: ChannelScopeRef | TenantScopeRef = (
        ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id)
        if channel_id is not None
        else TenantScopeRef(tenant_id=tenant_id)
    )
    prior = await get_scope(session, scope=scope)
    previous = prior.environment_name if prior is not None else None
    if environment_name is not None:
        await set_fields(
            session,
            scope=scope,
            tenant_id=tenant_id,
            environment_name=environment_name,
            actor_account_id=actor_account_id,
        )
    elif previous is not None:
        await unset_fields(
            session, scope=scope, fields=["environment_name"], actor_account_id=actor_account_id
        )
    return previous


__all__ = [
    "ENVIRONMENT_OPTION_INHERIT",
    "NOT_OFFERED_NOTE",
    "EnvironmentPicker",
    "authorize_environment_pick",
    "build_archive_environment_note",
    "build_clear_environment_note",
    "build_environment_resolution_note",
    "build_missing_environment_note",
    "build_sealed_network_refusal",
    "build_set_environment_note",
    "environment_choices",
    "environment_option_value",
    "has_open_network",
    "list_environment_names",
    "may_pick_environment_in",
    "parse_environment_option",
    "plan_environment_picker",
    "save_scope_environment",
]
