"""Agent reach: every place an agent answers, and whether it stays inside some channels.

An agent answers through the config cascade (a channel default, the tenant
default, the deployment fall-through; see `daimon.core.scope.answering_places`)
and through threads bound to it, under every name it carries (its own name
and its routing name) and by its stable id. It also runs in other people's
live sessions with it and in their routines, each in its own channel, which is
everything `is_agent_shared_for_key_changes` counts. It is *local to channels S*
for a caller when it is not a tenant-wide or anyone's personal default, it
answers or runs somewhere and every such channel lies in S (a thread counts as
its parent channel; a session or routine whose channel is unknown lies in
none), and no unattended run of it is owed to someone else with rights beyond
the caller's: a server admin, or a channel admin of a channel outside S.
Unattended runs are routines and queued wakes (timers, handoffs, applied
private input); each fires with its requester's rights, so a stronger
requester's would run what the caller writes with those rights. A plain
member's carries only that member's own reach, as their chat does. Rights are
those stored at the requester's last chat turn, read when the caller edits: a
requester promoted later runs earlier edits with the new rights. An agent that
answers nowhere, or has no name at all, is local to nobody: locality only ever
narrows the sharing read toward refusal, never past it.

A private conversation counts as the channel `/dm` ran in: its DM channel's
row and its `dm:` scope answer only while it is the tenant's live conversation
there, and then as that source channel.

Locality alone never makes an agent a channel admin's: it must also be theirs
(`daimon.core.authz.channel_admin_holds`), which this module reads the facts
for (its creation channel, its pins, the defaults server admins set to it).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence
from typing import Final, NamedTuple

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    AgentStanding,
    Place,
    agent_held_in,
    authorize,
    build_subject,
    channel_admin_holds,
)
from daimon.core.authz import AgentReach as ReachFacts
from daimon.core.channel_admins import (
    ChannelAdminCaller,
    administered_channel_ids,
    load_administered_channel_ids,
)
from daimon.core.operation_policy import OperationKind, TargetFacts, needs_reachability_read
from daimon.core.permissions import confidential_channels
from daimon.core.scope import (
    AnsweringPlace,
    ChannelConfigRow,
    DeploymentDefault,
    TenantConfigRow,
    active_agent_channels,
    answering_places,
    is_agent_reachable,
)
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.agent_creation_channels import (
    get_creation_channel,
    record_creation_channel,
)
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.direct_messages import DmOrigin, list_dm_origins
from daimon.core.stores.domain import ChannelAdminsRow, UnattendedRequester
from daimon.core.stores.routines import list_routine_channel_ids, list_routine_creators
from daimon.core.stores.scoped_config_read import (
    has_personal_default,
    is_agent_shared_for_key_changes,
    list_propagations_for_tenant,
)
from daimon.core.stores.task_continuations import list_waiting_requesters
from daimon.core.stores.thread_agent_bindings import (
    list_bound_parent_channel_ids,
    list_dm_bindings,
)
from daimon.core.stores.thread_sessions import list_live_session_channel_ids
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

WIDE_SHARING_OPERATIONS: Final[frozenset[OperationKind]] = frozenset(
    {
        "agent_spec_edit",
        "key_replace",
        "key_remove",
        "mcp_replace",
        "mcp_remove",
        "repo_bind",
        "skill_repo_connect",
        "skill_add",
        "skill_remove",
    }
)
"""Read as shared by `is_agent_shared_for_key_changes`: every one of these writes also
reaches routines, bound threads and live sessions, so an agent only those point at is
still someone else's (an admin's routine would run a member's edited prompt)."""


class UnattendedRights(BaseModel):
    """The rights an unattended run fires with: its requester's stored role and grants."""

    model_config = ConfigDict(frozen=True)

    platform_user_id: str
    is_server_admin: bool = False
    administered_channel_ids: frozenset[str] = frozenset()

    def exceeds(self, channel_ids: Collection[str]) -> bool:
        """Whether these rights reach beyond a channel admin of `channel_ids`."""
        return self.is_server_admin or not self.administered_channel_ids <= frozenset(channel_ids)


class AgentReach(BaseModel):
    """Where one agent answers (cascade places, bound-thread parents) and who runs it."""

    model_config = ConfigDict(frozen=True)

    agent_names: tuple[str, ...]
    places: tuple[AnsweringPlace, ...] = ()
    thread_parent_channel_ids: frozenset[str] = frozenset()
    # Channels of other people's live sessions and routines of the agent.
    run_channel_ids: frozenset[str] = frozenset()
    # One of those runs in a channel nobody recorded, so it could be anywhere.
    has_unplaced_run: bool = False
    unattended_runs: tuple[UnattendedRights, ...] = ()
    is_personal_default: bool = False
    # Channels whose default a server admin set to it (`AgentStanding`).
    admin_default_channel_ids: frozenset[str] = frozenset()

    @property
    def is_tenant_wide(self) -> bool:
        """True when the agent is the tenant default or the deployment fall-through."""
        return any(place.tier != "channel" for place in self.places)

    @property
    def channel_ids(self) -> frozenset[str]:
        """Channels the agent answers or runs in: channel defaults, bound threads'
        parents, and other people's sessions and routines."""
        defaults = {place.channel_id for place in self.places if place.channel_id is not None}
        return frozenset(defaults) | self.thread_parent_channel_ids | self.run_channel_ids

    def stays_inside(self, channel_ids: Collection[str]) -> bool:
        """Whether every place the agent answers or runs lies in `channel_ids`.

        True for an agent that answers nowhere. A personal default answers its
        member everywhere, and a run with no known channel could be anywhere,
        so neither ever does.
        """
        return (
            bool(self.agent_names)
            and not self.is_tenant_wide
            and not self.is_personal_default
            and not self.has_unplaced_run
            and self.channel_ids <= frozenset(channel_ids)
        )

    def is_held_back_by_unplaced_runs(self, channel_ids: Collection[str]) -> bool:
        """Whether runs in no recorded channel are all that keep it outside `channel_ids`."""
        return self.has_unplaced_run and self.model_copy(
            update={"has_unplaced_run": False}
        ).stays_inside(channel_ids)

    def runs_unattended_beyond(
        self, channel_ids: Collection[str], *, platform_user_id: str | None
    ) -> bool:
        """Whether someone else with rights beyond `channel_ids` has an unattended run of it."""
        return any(
            run.platform_user_id != platform_user_id and run.exceeds(channel_ids)
            for run in self.unattended_runs
        )

    def may_move_into(self, channel_ids: Collection[str], *, platform_user_id: str | None) -> bool:
        """Whether a channel admin of `channel_ids` may bind it there: it answers
        and runs only inside them, or nowhere yet, and no stronger requester runs it."""
        return self.stays_inside(channel_ids) and not self.runs_unattended_beyond(
            channel_ids, platform_user_id=platform_user_id
        )

    def is_local_to(self, channel_ids: Collection[str], *, platform_user_id: str | None) -> bool:
        """Whether a channel admin of `channel_ids` holds it. Never one answering nowhere:
        its keys may still reach sessions and routines this reach cannot place."""
        return bool(self.channel_ids) and self.may_move_into(
            channel_ids, platform_user_id=platform_user_id
        )


def build_agent_reach(
    agent_names: tuple[str, ...],
    *,
    tenant: TenantConfigRow | None,
    channels: Sequence[ChannelConfigRow],
    default: DeploymentDefault,
    ma_agent_id: str | None = None,
    thread_parent_channel_ids: Iterable[str] = (),
    run_channel_ids: Iterable[str | None] = (),
    unattended_requesters: Iterable[UnattendedRequester] = (),
    grants: Sequence[ChannelAdminsRow] = (),
    dm_origins: Sequence[DmOrigin] = (),
    dm_bindings: Iterable[tuple[str, str, str, str]] = (),
    is_personal_default: bool = False,
) -> AgentReach:
    """`grants` are the tenant's channel admin rows, read for requesters' rights.

    `run_channel_ids` are the channels of other people's live sessions and
    routines of the agent, None for one whose channel is unknown.

    `dm_bindings` are the tenant's `(dm_channel_id, scope_id, responder_name,
    responder_ma_agent_id)` rows; one counts under any of `agent_names` or
    `ma_agent_id`.

    A place in a DM channel, or a `dm:` scope bound to the agent, counts as the
    live conversation's source channel, and not at all without one.
    """
    names = tuple(dict.fromkeys(name for name in agent_names if name))
    dm_bindings = tuple(dm_bindings)
    by_channel = {dm.channel_id: dm.origin for dm in dm_origins}
    by_scope = {dm.scope_id: dm.origin for dm in dm_origins}
    dm_channels = by_channel.keys() | {binding[0] for binding in dm_bindings}
    admin_defaults = frozenset(
        row.channel_id
        for row in channels
        if row.mode == "agent"
        and row.agent_name in names
        and row.agent_name_set_by_admin
        and row.channel_id not in dm_channels
    )
    places: dict[AnsweringPlace, None] = {}
    for name in names:
        for place in answering_places(name, tenant=tenant, channels=channels, default=default):
            if place.channel_id in dm_channels:
                origin = by_channel.get(place.channel_id)
                if origin is None:
                    continue
                place = AnsweringPlace(tier="channel", channel_id=origin)
            places[place] = None
    runs = set(run_channel_ids)
    dm_parents = {
        by_scope[scope]
        for _, scope, responder, responder_id in dm_bindings
        if (responder in names or (ma_agent_id is not None and responder_id == ma_agent_id))
        and scope in by_scope
    }
    return AgentReach(
        agent_names=names,
        places=tuple(places),
        thread_parent_channel_ids=frozenset(thread_parent_channel_ids) | dm_parents,
        run_channel_ids=frozenset(channel for channel in runs if channel is not None),
        has_unplaced_run=None in runs,
        unattended_runs=tuple(
            UnattendedRights(
                platform_user_id=requester.platform_user_id,
                is_server_admin=requester.is_admin,
                administered_channel_ids=administered_channel_ids(
                    ChannelAdminCaller(
                        platform_user_id=requester.platform_user_id,
                        role_ids=frozenset(requester.role_ids),
                    ),
                    grants,
                ),
            )
            for requester in dict.fromkeys(unattended_requesters)
        ),
        is_personal_default=is_personal_default,
        admin_default_channel_ids=admin_defaults,
    )


async def load_agent_reach(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller_account_id: uuid.UUID | None = None,
    caller_platform_user_id: str | None = None,
) -> AgentReach:
    """Shell half of `build_agent_reach`: read the cascade, bindings, DMs and every run.

    The caller ids leave the caller's own live sessions and routines out of
    the runs; None counts them all. Live sessions are found by `ma_agent_id`
    alone, so none count without it.
    """
    names = tuple(dict.fromkeys(name for name in agent_names if name))
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    channels = active_agent_channels(channels, default, confidential_channels(policy))
    parents = await list_bound_parent_channel_ids(
        session, tenant_id=tenant_id, responder_names=names, responder_ma_agent_id=ma_agent_id
    )
    sessions = (
        await list_live_session_channel_ids(
            session,
            tenant_id=tenant_id,
            ma_agent_id=ma_agent_id,
            caller_account_id=caller_account_id,
        )
        if ma_agent_id is not None
        else []
    )
    routines = await list_routine_channel_ids(
        session,
        tenant_id=tenant_id,
        agent_names=names,
        agent_id=ma_agent_id,
        caller_platform_user_id=caller_platform_user_id,
    )
    return build_agent_reach(
        names,
        tenant=tenant,
        channels=channels,
        default=default,
        ma_agent_id=ma_agent_id,
        thread_parent_channel_ids=parents,
        run_channel_ids=[*sessions, *routines],
        unattended_requesters=[
            *await list_routine_creators(
                session,
                tenant_id=tenant_id,
                platform=platform,
                agent_names=names,
                agent_id=ma_agent_id,
            ),
            *await list_waiting_requesters(
                session, tenant_id=tenant_id, target_names=names, target_ma_agent_id=ma_agent_id
            ),
        ],
        grants=await list_channel_admins(session, tenant_id=tenant_id, platform=platform),
        dm_origins=await list_dm_origins(session, tenant_id=tenant_id),
        dm_bindings=await list_dm_bindings(session, tenant_id=tenant_id),
        is_personal_default=await has_personal_default(
            session, tenant_id=tenant_id, agent_names=names
        ),
    )


class _Locality(NamedTuple):
    is_local: bool
    # And the agent is the caller's (`channel_admin_holds`); read only when local.
    is_held: bool = False
    # Why a reachable agent is not local, when that is the reason; both False when local.
    held_back_by_unattended_run: bool = False
    held_back_by_unplaced_run: bool = False


async def _caller_locality(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    caller_account_id: uuid.UUID | None,
    caller_platform_user_id: str | None,
) -> _Locality:
    """All False without a grant, reading no reach."""
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    if not administered:
        return _Locality(is_local=False)
    reach = await load_agent_reach(
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_names=agent_names,
        ma_agent_id=ma_agent_id,
        default=default,
        caller_account_id=caller_account_id,
        caller_platform_user_id=caller_platform_user_id,
    )
    if reach.is_local_to(administered, platform_user_id=caller.platform_user_id):
        return _Locality(
            is_local=True,
            is_held=await _caller_holds(
                session,
                tenant_id=tenant_id,
                platform=platform,
                agent_names=agent_names,
                ma_agent_id=ma_agent_id,
                caller=caller,
                administered=administered,
                reach=reach,
            ),
        )
    return _Locality(
        is_local=False,
        held_back_by_unattended_run=reach.runs_unattended_beyond(
            administered, platform_user_id=caller.platform_user_id
        ),
        held_back_by_unplaced_run=reach.is_held_back_by_unplaced_runs(administered),
    )


async def _caller_holds(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    caller: ChannelAdminCaller,
    administered: frozenset[str],
    reach: AgentReach,
) -> bool:
    """Shell half of `channel_admin_holds`. An unreadable policy counts no pin."""
    try:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable:
        policy = TenantAccessPolicy()
    created_for = (
        await get_creation_channel(
            session, tenant_id=tenant_id, ma_agent_id=ma_agent_id, platform=platform
        )
        if ma_agent_id is not None
        else None
    )
    return channel_admin_holds(
        policy,
        subject=build_subject(
            is_admin=False,
            platform_user_id=caller.platform_user_id,
            administered_channel_ids=administered,
        ),
        agent=AgentRef.of(*agent_names),
        standing=AgentStanding(
            created_for_channel_id=created_for,
            admin_default_channel_ids=reach.admin_default_channel_ids,
        ),
    )


async def record_created_for_channel(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    ma_agent_id: str,
    channel_id: str | None,
    caller: ChannelAdminCaller,
) -> bool:
    """Record that a channel admin created the agent for `channel_id`, from there.

    Only when the caller administers that channel (a thread's parent) and is
    no server admin, whose agents reach a channel by being made its default.
    True when recorded.
    """
    if caller.is_server_admin or channel_id is None:
        return False
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    if channel_id not in administered:
        return False
    await record_creation_channel(
        session,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        platform=platform,
        channel_id=channel_id,
    )
    return True


async def may_bind_as_channel_default(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    is_daimon_managed: bool,
    caller_account_id: uuid.UUID | None = None,
) -> bool:
    """Whether `caller` may make the agent the default of `channel_id`, which they administer.

    Server admins bind anything. A channel admin binds by the handoff rule,
    `authorize(BIND_CHANNEL_DEFAULT)` with `load_binding_reach`: never another
    channel's own agent, or one a member made, which would lend its keys and
    memory to this channel. `caller_account_id` leaves the caller's own live
    sessions out; None counts them. An unreadable policy counts no pin.
    """
    if caller.is_server_admin:
        return True
    try:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable:
        policy = TenantAccessPolicy()
    administered = await load_administered_channel_ids(
        session, tenant_id=tenant_id, platform=platform, caller=caller
    )
    agent = AgentRef.of(*agent_names)
    reach = await load_binding_reach(
        session,
        tenant_id=tenant_id,
        platform=platform,
        policy=policy,
        agent=agent,
        ma_agent_id=ma_agent_id,
        default=default,
        caller=caller,
        administered=administered,
        channel_id=channel_id,
        is_daimon_managed=is_daimon_managed,
        caller_account_id=caller_account_id,
    )
    return authorize(
        policy,
        subject=build_subject(
            is_admin=False,
            platform_user_id=caller.platform_user_id,
            administered_channel_ids=administered,
        ),
        action=Action.BIND_CHANNEL_DEFAULT,
        agent=agent,
        place=Place(channel_id=channel_id),
        reach=reach,
    ).allowed


def _grant_holders(grants: Sequence[ChannelAdminsRow]) -> dict[tuple[str, str], frozenset[str]]:
    """Each user and group a grant names, with the channels it names them for."""
    held: dict[tuple[str, str], set[str]] = {}
    for grant in grants:
        for user_id in grant.user_ids:
            held.setdefault(("user", user_id), set()).add(grant.channel_id)
        for role_id in grant.role_ids:
            held.setdefault(("role", role_id), set()).add(grant.channel_id)
    return {holder: frozenset(channels) for holder, channels in held.items()}


def _held_by_other_admin(
    policy: TenantAccessPolicy,
    *,
    reach: AgentReach,
    agent: AgentRef,
    standing: AgentStanding,
    grants: Sequence[ChannelAdminsRow],
    caller_platform_user_id: str | None,
    channel_id: str,
) -> bool:
    """Whether a channel admin other than the caller holds the agent local to
    channels without `channel_id`, which a binding there would take away.

    Read over the union of the channels of every other user and group a grant
    names without `channel_id`, since one person may hold it through several:
    both checks only grow with the channels, so it fails closed and may
    over-refuse.
    """
    channels = frozenset[str]().union(
        *(
            held
            for holder, held in _grant_holders(grants).items()
            if holder != ("user", caller_platform_user_id) and channel_id not in held
        )
    )
    return reach.is_local_to(channels, platform_user_id=None) and agent_held_in(
        policy, agent=agent, standing=standing, channel_ids=channels
    )


async def load_binding_reach(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    policy: TenantAccessPolicy,
    agent: AgentRef,
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    administered: frozenset[str],
    channel_id: str,
    is_daimon_managed: bool,
    caller_account_id: uuid.UUID | None,
) -> ReachFacts:
    """The facts `authorize` reads for a channel admin binding the agent into `channel_id`.

    As a channel's default, or a thread under it handed off (`HAND_OFF`).
    DB reads only: safe under the tenant policy lock.
    """
    if is_daimon_managed:
        return ReachFacts(managed=True)
    reach = await load_agent_reach(
        session,
        tenant_id=tenant_id,
        platform=platform,
        agent_names=tuple(name for name in agent.names if name),
        ma_agent_id=ma_agent_id,
        default=default,
        caller_account_id=caller_account_id,
        caller_platform_user_id=caller.platform_user_id,
    )
    if reach.is_tenant_wide:
        return ReachFacts(tenant_wide=True)
    standing = AgentStanding(
        created_for_channel_id=await get_creation_channel(
            session, tenant_id=tenant_id, ma_agent_id=ma_agent_id, platform=platform
        )
        if ma_agent_id is not None
        else None,
        admin_default_channel_ids=reach.admin_default_channel_ids,
    )
    return ReachFacts(
        local_to_caller=reach.may_move_into(administered, platform_user_id=caller.platform_user_id),
        held_by_caller=channel_admin_holds(
            policy,
            subject=build_subject(
                is_admin=False,
                platform_user_id=caller.platform_user_id,
                administered_channel_ids=administered,
            ),
            agent=agent,
            standing=standing,
            binding=True,
        ),
        held_by_other_admin=_held_by_other_admin(
            policy,
            reach=reach,
            agent=agent,
            standing=standing,
            grants=await list_channel_admins(session, tenant_id=tenant_id, platform=platform),
            caller_platform_user_id=caller.platform_user_id,
            channel_id=channel_id,
        ),
    )


async def _is_shared(
    session: AsyncSession,
    operation: OperationKind,
    *,
    tenant_id: uuid.UUID,
    agent_names: tuple[str, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller_account_id: uuid.UUID | None,
    caller_platform_user_id: str | None,
) -> bool:
    """The reachability fact, read as wide as the operation needs. No name fails closed."""
    if not agent_names:
        return True
    if operation in WIDE_SHARING_OPERATIONS:
        # No stable id to look routines, sessions and bindings up by: fail closed.
        return ma_agent_id is None or await is_agent_shared_for_key_changes(
            session,
            tenant_id=tenant_id,
            agent_names=agent_names,
            ma_agent_id=ma_agent_id,
            default=default,
            caller_account_id=caller_account_id,
            caller_platform_user_id=caller_platform_user_id,
        )
    tenant, channels = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    channels = active_agent_channels(channels, default, confidential_channels(policy))
    return any(
        is_agent_reachable(name, tenant=tenant, channels=channels, default=default)
        for name in agent_names
    )


async def load_target_facts(
    session: AsyncSession,
    operation: OperationKind,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    agent_names: tuple[str | None, ...],
    ma_agent_id: str | None,
    default: DeploymentDefault,
    caller: ChannelAdminCaller,
    is_daimon_managed: bool,
    caller_account_id: uuid.UUID | None = None,
    caller_platform_user_id: str | None = None,
) -> TargetFacts:
    """The policy facts for one target, reading only what the decision depends on.

    A server admin, a posted-token write or a managed target reads nothing; an
    agent nobody reaches skips the channel admin read. `agent_names` is every
    name the agent carries (`daimon.core.agent_pins.agent_pin_names`). The
    caller ids leave the caller's own routines and sessions out of a wide
    sharing read (`WIDE_SHARING_OPERATIONS`) and of locality alike; None counts
    them all. Locality counts everything the sharing read does, so it can only
    narrow it: a wide read on an agent with no stable id is shared and local
    to nobody.
    """
    if not needs_reachability_read(
        operation, is_admin=caller.is_server_admin, is_daimon_managed=is_daimon_managed
    ):
        return TargetFacts(is_daimon_managed=is_daimon_managed, is_reachable_in_tenant=False)
    names = tuple(dict.fromkeys(name for name in agent_names if name))
    reachable = await _is_shared(
        session,
        operation,
        tenant_id=tenant_id,
        agent_names=names,
        ma_agent_id=ma_agent_id,
        default=default,
        caller_account_id=caller_account_id,
        caller_platform_user_id=caller_platform_user_id,
    )
    unplaceable = operation in WIDE_SHARING_OPERATIONS and ma_agent_id is None
    locality = (
        await _caller_locality(
            session,
            tenant_id=tenant_id,
            platform=platform,
            agent_names=names,
            ma_agent_id=ma_agent_id,
            default=default,
            caller=caller,
            caller_account_id=caller_account_id,
            caller_platform_user_id=caller_platform_user_id,
        )
        if reachable and not unplaceable
        else _Locality(is_local=False)
    )
    return TargetFacts(
        is_daimon_managed=is_daimon_managed,
        is_reachable_in_tenant=reachable,
        is_local_to_caller_channels=locality.is_local,
        is_held_by_caller=locality.is_held,
        runs_unattended_beyond_caller=locality.held_back_by_unattended_run,
        has_unplaced_run=locality.held_back_by_unplaced_run,
    )


__all__ = [
    "WIDE_SHARING_OPERATIONS",
    "AgentReach",
    "UnattendedRights",
    "build_agent_reach",
    "load_agent_reach",
    "load_binding_reach",
    "load_target_facts",
    "may_bind_as_channel_default",
    "record_created_for_channel",
]
