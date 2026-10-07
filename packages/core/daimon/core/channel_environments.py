"""Channel environments: which environment a channel's turns run in.

The environment resolves over the same tiers as the agent (channel, then the
tenant default, then the deployment default; see `daimon.core.scope`) but on
its own, so a channel can keep the agent it has and run it with the packages
one team needs. Server admins set any channel's environment or the tenant
default; a channel admin sets the channels they run, except an environment
with unrestricted networking in a sealed channel (`authorize`'s
SET_CHANNEL_ENVIRONMENT): any network beyond package managers and the
agent's MCP servers (`has_open_network`). A server admin confirms any change
that leaves a sealed channel on one: a pick there, a workspace default it
follows, an environment edit or archive (`update_needs_confirm`,
`archive_needs_confirm`). A scope with no environment of its own falls
through, so nothing changes until one is set.

The sentences and the setup panels' picker live here so the chat tools and
both panels say and offer the same. Only the select's length differs by
platform (`tests/parity/test_environment_select_caps.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaEnvironment
from anthropic.types.beta.beta_cloud_config_params import BetaCloudConfigParams
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.answering_map import AnsweringMap
from daimon.core.authz import Action, Decision, Place, Subject, authorize, holds_limited_readers
from daimon.core.defaults.ma_index import (
    find_environment_by_daimon_tag,
    list_environments_by_tenant,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.permissions import home_of, limited_ids
from daimon.core.rule_views import RuleViewer, load_rule_viewer
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    ConfigTier,
    DeploymentDefault,
    ScopeContext,
    TenantConfigRow,
    TenantScopeRef,
)
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.scoped_config_read import (
    get_scope,
    list_propagations_for_tenant,
    resolve,
)
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
from daimon.core.stores.thread_sessions import recorded_thread_parents
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


def build_limited_network_refusal(*, environment_name: str | None) -> str:
    """Why a channel admin's pick in a channel with limited readers was refused; None is a clear."""
    what = (
        f"the {environment_name} environment has"
        if environment_name is not None
        else "the default it would fall back to has"
    )
    return (
        f"Only turns inside this channel read it, and {what} unrestricted network access, "
        "so only a server admin can make that change here. Nothing changed. Pick an "
        "environment with limited networking and no allowed hosts, or ask a server admin."
    )


def build_limited_network_confirm(*, environment_name: str | None, panel: bool = False) -> str:
    """Why a server admin's pick in a channel with limited readers waits for a confirmation.

    A panel has no confirm step, so it points at chat, whose tool asks for one.
    """
    what = (
        f"the {environment_name} environment has"
        if environment_name is not None
        else "the default it would fall back to has"
    )
    note = (
        f"Only turns inside this channel read it, and {what} unrestricted network access, "
        "so its content could leave through it. Nothing changed."
    )
    if panel:
        note += " To use it anyway, ask daimon to make the change in chat and confirm there."
    return note


def build_limited_channels_confirm(*, environment_name: str | None) -> str:
    """Why a change beyond one channel waits for a confirmation: it leaves channels
    with limited readers on an open network (a workspace default, an environment
    edit or archive)."""
    what = (
        f"the {environment_name} environment"
        if environment_name is not None
        else "the environment they would fall back to"
    )
    return (
        f"Channels only turns inside them read would run in {what}, which has unrestricted "
        "network access, so their content could leave through it. Nothing changed."
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


async def list_environment_names(
    client: AsyncAnthropic, *, tenant_id: uuid.UUID, hidden: frozenset[str] = frozenset()
) -> list[str]:
    """The names a scope may pick in this tenant, de-duplicated and sorted case-insensitively.

    The name is the resolver's key (`daimon_name`), the same one a turn and a
    save look up; an environment without one cannot be picked, so is left out,
    as is one in `hidden` (`load_hidden_environment_names`).
    """
    environments = await list_environments_by_tenant(client, tenant_id=tenant_id)
    names = {name for env in environments if (name := env.metadata.get(MA_METADATA_KEY_NAME))}
    return sorted(names - hidden, key=str.casefold)


def hidden_environment_names(
    viewer: RuleViewer,
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
) -> frozenset[str]:
    """Names only isolated channels the viewer stands outside pick. Pure.

    A name could name the client a channel serves. One a default or any other
    channel uses is shared, so stays seen.
    """
    hidden: set[str] = set()
    shown = {tenant.environment_name if tenant is not None else None, default.environment_name}
    for row in channels:
        if row.environment_name:
            owner = home_of(viewer.policy, row.channel_id)
            across = owner not in (None, viewer.inside_channel_id)
            (hidden if across else shown).add(row.environment_name)
    return frozenset(hidden - shown)


async def load_hidden_environment_names(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    viewer: RuleViewer | None,
    default: DeploymentDefault,
) -> frozenset[str]:
    """`hidden_environment_names` for `viewer`; None, or nothing isolated, hides none."""
    if viewer is None or not viewer.is_active:
        return frozenset()
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    return hidden_environment_names(viewer, tenant=tenant, channels=channels, default=default)


async def load_panel_hidden_environment_names(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    is_admin: bool,
    default: DeploymentDefault,
) -> frozenset[str]:
    """What a panel reader at `channel_id` doesn't see; a server admin sees every name."""
    viewer = await load_rule_viewer(
        session, client, tenant_id=tenant_id, channel_id=channel_id, is_admin=is_admin
    )
    return await load_hidden_environment_names(
        session, tenant_id=tenant_id, viewer=viewer, default=default
    )


def has_open_network(environment: BetaEnvironment) -> bool:
    """Any network beyond package managers and the agent's MCP servers.

    Only a cloud environment on limited networking with no allowed hosts is
    closed: an allowed host is somewhere a sealed channel's content could go.
    A self-hosted environment's network is its host's, unknown here, so it
    counts as open.
    """
    config = environment.config
    if config.type != "cloud" or config.networking.type != "limited":
        return True
    return bool(config.networking.allowed_hosts)


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


@dataclass(frozen=True)
class EnvironmentPick:
    """A decided pick, with the environment it sets looked up once.

    The network rule and the write both use `environment`, so an environment
    created or replaced under the same name in between can't slip past the rule.
    """

    decision: Decision
    environment: BetaEnvironment | None = None
    """The environment being set; None on a clear or a refusal."""
    missing: bool = False
    """A set naming no environment of the tenant: nothing to write."""
    needs_confirm: bool = False
    """An allowed pick leaves a sealed channel on an open network: written only
    once the server admin confirms it."""

    @property
    def environment_name(self) -> str | None:
        """The name to store: the looked-up environment's own; None on a clear."""
        if self.environment is None:
            return None
        return self.environment.metadata.get(MA_METADATA_KEY_NAME)


async def authorize_environment_pick(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    subject: Subject,
    channel_id: str | None,
    environment_name: str | None,
    default: DeploymentDefault,
    thread_id: str | None = None,
) -> EnvironmentPick:
    """Whether `subject` may set `channel_id`'s environment to `environment_name`.

    None clears it, leaving the channel on the workspace or deployment
    default, which then decides the network rule; a missing default counts
    as open. A set looks its environment up once, after the admin check, and
    one that doesn't exist is `missing`. `thread_id` is a thread under
    `channel_id` the pick names, so a seal on that thread counts; without one, a
    sealed thread a session ran in under `channel_id` counts. A pick only a server
    admin may make, an open network in a sealed channel, `needs_confirm`; so is a
    workspace default that moves a sealed channel or thread onto one.
    """
    policy = await load_access_policy(session, tenant_id=tenant_id)
    if (
        channel_id is not None
        and thread_id is None
        and not holds_limited_readers(policy, channel_id)
    ):
        thread_id = await _sealed_thread_under(
            session, policy, tenant_id=tenant_id, channel_id=channel_id
        )
    place = Place(
        channel_id=thread_id or channel_id,
        parent_channel_id=channel_id if thread_id is not None else None,
    )

    def decide(*, open_network: bool) -> Decision:
        return authorize(
            policy,
            subject=subject,
            action=Action.SET_CHANNEL_ENVIRONMENT,
            place=place,
            open_network=open_network,
        )

    gate = decide(open_network=False)
    if not gate:
        return EnvironmentPick(decision=gate)
    sealed = channel_id is not None and holds_limited_readers(policy, channel_id, place)
    if environment_name is not None:
        environment = await find_environment_by_daimon_tag(
            client, tenant_id=tenant_id, name=environment_name
        )
        if environment is None:
            return EnvironmentPick(decision=gate, missing=True)
        open_network = has_open_network(environment)
        decision = decide(open_network=open_network)
        if channel_id is None and decision and open_network:
            sealed = bool(
                await _sealed_moves(
                    session,
                    policy,
                    tenant_id=tenant_id,
                    default=default,
                    change=_workspace(environment_name),
                )
            )
        return EnvironmentPick(
            decision=decision,
            environment=environment if decision else None,
            needs_confirm=bool(decision) and sealed and open_network,
        )
    if channel_id is None:
        moves = await _sealed_moves(
            session, policy, tenant_id=tenant_id, default=default, change=_workspace(None)
        )
        open_network = await _any_open(client, tenant_id=tenant_id, names=moves)
        return EnvironmentPick(decision=gate, needs_confirm=open_network)
    if not sealed:
        return EnvironmentPick(decision=gate)
    fallback = await resolve(
        session, context=ScopeContext(tenant_id=tenant_id, channel_id=None), default=default
    )
    environment = await find_environment_by_daimon_tag(
        client, tenant_id=tenant_id, name=fallback.environment_name or "default"
    )
    open_network = environment is None or has_open_network(environment)
    decision = decide(open_network=open_network)
    return EnvironmentPick(decision=decision, needs_confirm=bool(decision) and open_network)


@dataclass(frozen=True)
class _Picks:
    """Each channel's own environment, and the workspace default."""

    channels: dict[str, str]
    workspace: str | None

    def runs(self, seal_id: str, parent: str | None, default: DeploymentDefault) -> str:
        """The environment a turn under `seal_id`, in `parent` if a thread, runs in."""
        return (
            self.channels.get(seal_id)
            or (self.channels.get(parent) if parent is not None else None)
            or self.workspace
            or default.environment_name
            or "default"
        )

    def without(self, environment_name: str) -> _Picks:
        """The picks once every pick of `environment_name` is cleared."""
        return _Picks(
            channels={c: n for c, n in self.channels.items() if n != environment_name},
            workspace=None if self.workspace == environment_name else self.workspace,
        )


def _workspace(environment_name: str | None) -> Callable[[_Picks], _Picks]:
    return lambda picks: replace(picks, workspace=environment_name)


def _thread_of(seal: str, channels: Iterable[str]) -> str | None:
    """The channel a Slack thread (``channel:ts``) names; a Teams thread keeps no rule."""
    return next((c for c in channels if seal.startswith(f"{c}:")), None)


async def _seal_parents(
    session: AsyncSession, *, tenant_id: uuid.UUID, seals: Iterable[str], channels: Iterable[str]
) -> dict[str, tuple[str | None, ...]]:
    """Each sealed id with the channels it runs under (None: none, or itself one).

    A Discord thread is placed by the sessions run in it; until one has, it counts
    as following the workspace default.
    """
    seals, channels = tuple(seals), tuple(channels)
    parents = await recorded_thread_parents(
        session, tenant_id=tenant_id, thread_ids=[s for s in seals if s.isdigit()]
    )
    placed: dict[str, tuple[str | None, ...]] = {}
    for seal in seals:
        named = _thread_of(seal, channels)
        placed[seal] = (named,) if named else tuple(sorted(parents.get(seal, ()))) or (None,)
    return placed


async def _sealed_thread_under(
    session: AsyncSession, policy: TenantAccessPolicy, *, tenant_id: uuid.UUID, channel_id: str
) -> str | None:
    """A sealed Discord thread a session ran in under `channel_id`, if any."""
    seals = limited_ids(policy)
    places = await _seal_parents(session, tenant_id=tenant_id, seals=seals, channels=(channel_id,))
    return next((seal for seal in sorted(places) if channel_id in places[seal]), None)


async def _load_picks(session: AsyncSession, *, tenant_id: uuid.UUID) -> _Picks:
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    return _Picks(
        channels={row.channel_id: row.environment_name for row in channels if row.environment_name},
        workspace=tenant.environment_name if tenant is not None else None,
    )


async def _sealed_moves(
    session: AsyncSession,
    policy: TenantAccessPolicy,
    *,
    tenant_id: uuid.UUID,
    default: DeploymentDefault,
    change: Callable[[_Picks], _Picks],
) -> frozenset[str]:
    """The environments `change` moves sealed channels or threads onto."""
    seals = limited_ids(policy)
    if not seals:
        return frozenset()
    before = await _load_picks(session, tenant_id=tenant_id)
    after = change(before)
    places = await _seal_parents(
        session, tenant_id=tenant_id, seals=seals, channels={*before.channels, *after.channels}
    )
    return frozenset(
        after.runs(seal, parent, default)
        for seal, parents in places.items()
        for parent in parents
        if after.runs(seal, parent, default) != before.runs(seal, parent, default)
    )


async def _any_open(client: AsyncAnthropic, *, tenant_id: uuid.UUID, names: Iterable[str]) -> bool:
    """Whether any of `names` has an open network; a missing one counts as open."""
    for name in names:
        environment = await find_environment_by_daimon_tag(client, tenant_id=tenant_id, name=name)
        if environment is None or has_open_network(environment):
            return True
    return False


def opens_network(environment: BetaEnvironment, config: BetaCloudConfigParams) -> bool:
    """Whether patching `environment` with `config` takes it from a closed network
    to an open one (`has_open_network`). Omitted fields keep their value, as the
    update does."""
    if has_open_network(environment):
        return False
    networking = config.get("networking")
    if networking is None:
        return False
    if networking["type"] != "limited":
        return True
    return bool(networking.get("allowed_hosts"))


async def update_needs_confirm(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    environment: BetaEnvironment,
    config: BetaCloudConfigParams | None,
    default: DeploymentDefault,
) -> bool:
    """Whether patching `environment` with `config` opens its network while a sealed
    channel runs in it, so a server admin confirms it first."""
    name = environment.metadata.get(MA_METADATA_KEY_NAME)
    if config is None or name is None or not opens_network(environment, config):
        return False
    seals = limited_ids(await load_access_policy(session, tenant_id=tenant_id))
    picks = await _load_picks(session, tenant_id=tenant_id)
    places = await _seal_parents(session, tenant_id=tenant_id, seals=seals, channels=picks.channels)
    return any(
        picks.runs(seal, parent, default) == name
        for seal, parents in places.items()
        for parent in parents
    )


async def archive_needs_confirm(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    environment_name: str,
    default: DeploymentDefault,
) -> bool:
    """Whether archiving `environment_name` drops a sealed channel that runs in it
    onto an environment with an open network, or none, so a server admin confirms
    it first."""
    policy = await load_access_policy(session, tenant_id=tenant_id)
    moves = await _sealed_moves(
        session,
        policy,
        tenant_id=tenant_id,
        default=default,
        change=lambda picks: picks.without(environment_name),
    )
    return await _any_open(client, tenant_id=tenant_id, names=moves)


LIMITED_OPEN_NETWORK_WARNING: Final = (
    "This channel runs an environment with open network; a server admin should confirm "
    "or change it."
)
"""Sealing a channel whose own pick may predate the seal's network rule."""

LIMITED_NETWORK_UNCHECKED_WARNING: Final = (
    "This channel's environment could not be checked for an open network; a server "
    "admin should confirm it."
)
"""Sealing a channel whose own pick could not be looked up."""


async def limited_readers_network_warning(
    session: AsyncSession,
    client: AsyncAnthropic,
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    default: DeploymentDefault,
) -> str | None:
    """The warning for sealing `channel_id` while its own environment has an open network.

    A pick made before the seal skipped the network rule, and who made it is
    not recorded, so a channel's own open pick is unconfirmed. The workspace
    and deployment defaults are a server admin's or operator's. One that no
    longer exists can't run, so isn't warned of.
    """
    resolved = await resolve(
        session, context=ScopeContext(tenant_id=tenant_id, channel_id=channel_id), default=default
    )
    if resolved.environment_name_tier != "channel" or resolved.environment_name is None:
        return None
    try:
        environment = await find_environment_by_daimon_tag(
            client, tenant_id=tenant_id, name=resolved.environment_name
        )
    except anthropic.APIError:  # the seal is saved already; a lookup must not undo its reply
        return LIMITED_NETWORK_UNCHECKED_WARNING
    if environment is None or not has_open_network(environment):
        return None
    return LIMITED_OPEN_NETWORK_WARNING


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
    "LIMITED_NETWORK_UNCHECKED_WARNING",
    "LIMITED_OPEN_NETWORK_WARNING",
    "EnvironmentPick",
    "EnvironmentPicker",
    "archive_needs_confirm",
    "authorize_environment_pick",
    "build_archive_environment_note",
    "build_clear_environment_note",
    "build_environment_resolution_note",
    "build_missing_environment_note",
    "build_limited_channels_confirm",
    "build_limited_network_confirm",
    "build_limited_network_refusal",
    "build_set_environment_note",
    "environment_choices",
    "environment_option_value",
    "has_open_network",
    "hidden_environment_names",
    "list_environment_names",
    "load_hidden_environment_names",
    "load_panel_hidden_environment_names",
    "may_pick_environment_in",
    "opens_network",
    "parse_environment_option",
    "plan_environment_picker",
    "save_scope_environment",
    "limited_readers_network_warning",
    "update_needs_confirm",
]
