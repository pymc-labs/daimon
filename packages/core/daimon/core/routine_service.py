"""Saving, changing and removing routines: the checks every surface shares.

The `create_routine` chat tool, the Slack and Teams panels and the web app all go
through here, so a routine saved from any of them passes the same gates: the
schedule is valid, the destination could be posted to, the tenant's rules allow the
agent there, and only the owner or an admin changes it. A surface describes who is
acting (`RoutineCaller`) and brings what only it can do (`RoutineEnv`): looking a
destination up on its chat platform, and naming the agent a channel answers with.
Refusals raise `RoutineRefused` with plain text; the surface words them its own way.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from anthropic import AsyncAnthropic
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.agent_pins import agent_aliases
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize, build_agent_ref
from daimon.core.cron import InvalidScheduleError, validated_next_slot
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.errors import UserFacingError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.permissions import any_agent_rules, any_own_readers, home_of
from daimon.core.routine_delivery import destination_shape_error
from daimon.core.rule_views import RuleViewer, routine_destination_channel
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores import routines as routines_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import CatchUpPolicy, RoutineDestinationKind, RoutineRow
from daimon.core.turn_hold import TurnHold
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOT_FOUND = "routine not found"
"""What an unknown id and someone else's routine both read as, so a probe learns nothing."""

_TEAMS_THREAD = ";messageid="


class RoutineRefused(UserFacingError):
    """A routine change was refused; the message is plain text for the person."""


class InvalidSchedule(RoutineRefused):
    """The cron expression or time zone cannot be scheduled."""


class UnknownAgent(RoutineRefused):
    """No agent of that name is here for the caller."""


class RoutineNotFound(RoutineRefused):
    """No such routine for the caller."""

    def __init__(self) -> None:
        super().__init__(NOT_FOUND)


class RoutineNotOwned(RoutineNotFound):
    """Someone else's routine. Reads as `RoutineNotFound` to anyone probing; a panel that
    already lists the routine to the caller may word it differently."""


@dataclass(frozen=True)
class Placement:
    """Where a checked destination posts."""

    channel_id: str
    """The place the post is authorised at: the channel, or a thread's own id."""
    parent_channel_id: str | None = None
    """The channel a thread sits in, when the platform says so."""
    category_id: str | None = None
    category_unresolved: bool = False
    """The surface could not look the category up; a rule on categories then refuses."""

    @property
    def budget_channel_id(self) -> str:
        """The channel the routine's spend counts against."""
        return self.parent_channel_id or self.channel_id


class DestinationResolver(Protocol):
    """Looks a destination up on its platform and checks the caller may post there.

    Raises when the channel is missing, unseen by the caller, or closed to them.
    """

    async def __call__(self, kind: RoutineDestinationKind, destination_id: str) -> Placement: ...


Happened = Literal["created", "changed", "paused", "resumed", "deleted"]

OnChange = Callable[[AsyncSession, Happened, RoutineRow], Awaitable[None]]
"""Runs inside the transaction that made the change, so a record of it commits with it."""

ChannelAgentName = Callable[[str], Awaitable[str | None]]
"""The name of the agent a channel answers with by default."""


@dataclass(frozen=True)
class RoutineCaller:
    """Who changes routines, and what their surface knows about the call."""

    tenant_id: uuid.UUID
    subject: Subject
    """The caller as `authorize` sees it (its `is_admin` is the surface's trusted signal)."""
    platform: str
    is_admin: bool = False
    """Whether the caller may change any routine, not only their own."""
    hold: TurnHold | None = None
    own_agent_ids: frozenset[uuid.UUID] = frozenset()
    """The agents (as `derive_agent_uuid`) the caller is talking to or may chat with; a
    member may schedule these."""
    admin_noun: str = "an admin"

    @property
    def user_id(self) -> str | None:
        return self.subject.platform_user_id


@dataclass(frozen=True)
class CallView:
    """What the caller may see, from where the call is made."""

    viewer: RuleViewer
    origin: Place | None = None
    """The channel a chat call is made in, when verified."""


@dataclass(frozen=True)
class RoutineEnv:
    """What the service needs beyond the caller: stores, agents and the platform."""

    sessionmaker: async_sessionmaker[AsyncSession]
    anthropic: AsyncAnthropic
    resolve_destination: DestinationResolver | None = None
    """None where the surface sets no destinations; naming one is then refused."""
    channel_agent_name: ChannelAgentName | None = None
    load_view: Callable[[], Awaitable[CallView]] | None = None
    """Loaded when needed, so a surface pays for it only after the cheap checks pass."""
    origin_budget_channel: Callable[[], Awaitable[str | None]] | None = None
    """The channel a destination-less routine's spend counts against."""
    require_destination: bool = False
    on_change: OnChange | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class NewRoutine:
    agent_name: str
    cron_expr: str
    timezone: str
    trigger_message: str
    enabled: bool = True
    catch_up_policy: CatchUpPolicy = "skip"
    destination_kind: RoutineDestinationKind | None = None
    destination_id: str | None = None


@dataclass(frozen=True)
class RoutineChange:
    """Only the fields given are changed."""

    agent_name: str | None = None
    cron_expr: str | None = None
    timezone: str | None = None
    trigger_message: str | None = None
    enabled: bool | None = None
    catch_up_policy: CatchUpPolicy | None = None
    destination_kind: RoutineDestinationKind | None = None
    destination_id: str | None = None
    clear_destination: bool = False


def destination_channel_of(
    platform: str, kind: RoutineDestinationKind | None, destination_id: str | None
) -> str | None:
    """The channel a destination posts straight into, where its id says so.

    A Discord thread's channel needs the platform to look it up, so it reads as unknown.
    """
    if kind is None or destination_id is None:
        return None
    if kind == "channel":
        return destination_id
    if platform == "slack":
        return destination_id.partition(":")[0]
    if platform == "teams":
        return destination_id.partition(_TEAMS_THREAD)[0]
    return None


def may_read(caller: RoutineCaller, row: RoutineRow) -> bool:
    """The routine's owner, or an admin.

    A routine's trigger and the tail of its last result are what its agent was asked
    and answered, which for a client project agent is that client's work.
    """
    if caller.is_admin:
        return True
    return caller.user_id is not None and caller.user_id == row.created_by_user_id


def sees_routine(viewer: RuleViewer, row: RoutineRow) -> bool:
    """Routines of a channel kept to its own agents, and of those agents, show only inside it."""
    place = home_of(viewer.policy, row.channel_id, None)
    return viewer.sees(row.agent_name) and place in (None, viewer.inside_channel_id)


async def _record(
    env: RoutineEnv, session: AsyncSession, happened: Happened, row: RoutineRow
) -> None:
    if env.on_change is not None:
        await env.on_change(session, happened, row)


def isolation_view_loader(
    sessionmaker: async_sessionmaker[AsyncSession],
    anthropic: AsyncAnthropic,
    tenant_id: uuid.UUID,
    *,
    location_channel_id: str | None = None,
) -> Callable[[], Awaitable[CallView]]:
    """A `RoutineEnv.load_view` for surfaces with no turn of their own (a panel, the web app).

    Channels kept to their own agents hide their agents' routines from anywhere else;
    with none, everything shows.
    """

    async def load() -> CallView:
        async with sessionmaker() as session:
            policy = await _load_policy(session, tenant_id)
        if not any_own_readers(policy):
            return CallView(viewer=RuleViewer(policy=policy))
        agents = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
        return CallView(
            viewer=RuleViewer(
                policy,
                home_of(policy, location_channel_id),
                agent_aliases(agents),
                location_channel_id=location_channel_id,
            )
        )

    return load


async def _view(env: RoutineEnv) -> CallView:
    if env.load_view is None:
        return CallView(viewer=RuleViewer(policy=OPEN_ACCESS_POLICY))
    return await env.load_view()


def _require_allowed(caller: RoutineCaller) -> None:
    """A held credential or a client saves, changes and reads no routine.

    Decided before any policy lookup, so no rule or empty policy relaxes it.
    """
    decision = authorize(
        OPEN_ACCESS_POLICY,
        subject=caller.subject,
        action=Action.SAVE_ROUTINE,
        surface=Surface.ROUTINE,
        hold=caller.hold,
    )
    if not decision and decision.reason in ("outside_hold", "client_requester"):
        record_authz_denial(Action.SAVE_ROUTINE, decision.reason)
        raise RoutineRefused(
            "this conversation is held to the channel it runs in, so it can't use routines. "
            "Tell the caller. Do not retry."
        )


def _require_owner(caller: RoutineCaller, row: RoutineRow) -> None:
    """Owner-or-admin gate for a change; an ownerless routine is an admin's alone.

    Raises the text an unknown id produces, so a non-owner cannot tell forbidden from
    missing. Both sides are nullable and neither null identifies an owner, so a caller
    with no user id owns nothing and `None == None` never lets one through.
    """
    if caller.is_admin:
        return
    if caller.user_id is None or row.created_by_user_id is None:
        raise RoutineNotOwned()
    if caller.user_id != row.created_by_user_id:
        raise RoutineNotOwned()


def next_fire(env: RoutineEnv, cron_expr: str, timezone: str) -> datetime:
    """The next time a schedule fires; a bad time zone or cron is refused in plain text."""
    try:
        return validated_next_slot(cron_expr, timezone, env.now())
    except InvalidScheduleError as err:
        raise InvalidSchedule(str(err)) from err


async def _load_policy(session: AsyncSession, tenant_id: uuid.UUID) -> TenantAccessPolicy:
    try:
        return await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable as err:
        raise RoutineRefused(
            "the access settings here could not be read; nothing was saved"
        ) from err


async def check_destination(
    env: RoutineEnv,
    caller: RoutineCaller,
    *,
    kind: RoutineDestinationKind | None,
    destination_id: str | None,
) -> str | None:
    """Refuse a destination that could never be posted to, before it is saved.

    Returns the channel the routine's spend is budgeted against (a thread's parent), or
    None without a destination. The pair must come together; the id must have the
    platform's shape; the surface's resolver confirms the channel exists with the
    caller able to post; and the tenant's access policy must not protect it. The
    adapter re-checks at post time, so a channel protected or removed later still holds.
    """
    if (kind is None) != (destination_id is None):
        raise RoutineRefused("destination_kind and destination_id must be given together")
    if kind is None or destination_id is None:
        return None
    shape_error = destination_shape_error(caller.platform, kind, destination_id)
    if shape_error is not None:
        raise RoutineRefused(f"invalid destination_id: {shape_error}. Nothing was saved.")
    if env.resolve_destination is None:
        raise RoutineRefused("A destination can't be set here. Nothing was saved.")
    placement = await env.resolve_destination(kind, destination_id)
    async with env.sessionmaker() as session:
        policy = await _load_policy(session, caller.tenant_id)
    if not authorize(
        policy,
        subject=caller.subject,
        action=Action.POST,
        surface=Surface.ROUTINE,
        hold=caller.hold,
        place=Place(
            channel_id=placement.channel_id,
            parent_channel_id=placement.parent_channel_id,
            category_id=placement.category_id,
            category_unresolved=placement.category_unresolved,
        ),
    ):
        raise RoutineRefused(
            f"{placement.channel_id}'s rule lets nobody write there, so a routine "
            "cannot deliver to it. Pick another channel. Nothing was saved."
        )
    return placement.budget_channel_id


def _check_agent_pin(
    policy: TenantAccessPolicy,
    caller: RoutineCaller,
    *,
    agent_name: str,
    agent: AgentRef,
    kind: RoutineDestinationKind | None,
    destination_id: str | None,
    destination_channel_id: str | None,
    origin: Place | None = None,
) -> None:
    """Refuse a routine that would run a pinned agent outside its channels, or another
    agent into an isolated channel (`authorize(SAVE_ROUTINE)`). One saved from a verified
    `origin` inside an isolated channel stays in it.

    A pinned agent's routine must post straight into one of its pinned channels: the
    scheduler can't resolve a Discord thread's parent at fire time, so a thread
    destination is refused here rather than skipped later. The scheduler re-checks at
    every fire, so a pin added later still holds.
    """
    decision = authorize(
        policy,
        subject=caller.subject,
        action=Action.SAVE_ROUTINE,
        surface=Surface.ROUTINE,
        agent=agent,
        place=Place(
            channel_id=destination_channel_of(caller.platform, kind, destination_id),
            parent_channel_id=destination_channel_id,
        ),
        origin=origin,
        hold=caller.hold,
    )
    if not decision:
        record_authz_denial(Action.SAVE_ROUTINE, decision.reason)
    if decision.reason == "own_agents_only":
        if origin is not None and home_of(policy, origin.channel_id, origin.parent_channel_id):
            raise RoutineRefused(
                "This conversation is in a channel kept to its own agents, so its routines "
                "post only into that channel. Nothing was saved."
            )
        raise RoutineRefused(
            "That channel is kept to its own agents, so only they post there. Nothing was saved."
        )
    if not decision:
        raise RoutineRefused(
            f"{agent_name}'s rule runs it only in certain channels, so its routines "
            "must post straight into one of them (a channel destination, not a thread "
            "or none). Nothing was saved."
        )


async def _require_agent_in_scope(
    env: RoutineEnv,
    caller: RoutineCaller,
    *,
    agent_name: str,
    ma_agent_id: str,
    destination_channel_id: str | None,
) -> None:
    """Refuse a non-admin scheduling an agent that isn't theirs to drive here.

    A fired routine runs as its agent, with that agent's repo, keys, connectors and
    memory, so naming any agent in the workspace would let a member of one client's
    channel drive another client's agent. A member may schedule the agent they are
    talking to, or the agent the destination channel itself answers with. Anything else
    is an admin's call.
    """
    if caller.is_admin:
        return
    if (
        derive_agent_uuid(tenant_id=caller.tenant_id, ma_agent_id=ma_agent_id)
        in caller.own_agent_ids
    ):
        return
    if (
        destination_channel_id is not None
        and env.channel_agent_name is not None
        and await env.channel_agent_name(destination_channel_id) == agent_name
    ):
        return
    raise RoutineRefused(
        f"{agent_name} is neither the agent you are talking to nor the one the destination "
        "channel answers with, and a routine runs with its agent's repository, keys and "
        f"connectors, so only {caller.admin_noun} can schedule it. Tell the caller "
        "who can. Nothing was saved."
    )


async def create_routine(env: RoutineEnv, caller: RoutineCaller, new: NewRoutine) -> RoutineRow:
    """Save a routine for the caller, after every check."""
    _require_allowed(caller)
    if caller.user_id is None:
        raise RoutineRefused("creating a routine requires a platform user identity")
    next_fire_at = next_fire(env, new.cron_expr, new.timezone)
    destination_channel_id = await check_destination(
        env, caller, kind=new.destination_kind, destination_id=new.destination_id
    )
    if env.require_destination and new.destination_kind is None:
        raise RoutineRefused(
            "No destination was supplied. This routine won't post, so nothing was created. "
            "Where should results be delivered?"
        )
    async with env.sessionmaker() as session:
        policy = await _load_policy(session, caller.tenant_id)
    budget_channel_id = destination_channel_id
    if new.destination_kind is None and env.origin_budget_channel is not None:
        budget_channel_id = await env.origin_budget_channel()
    view = await _view(env)
    match = await find_agent_by_daimon_tag(
        env.anthropic, tenant_id=caller.tenant_id, name=new.agent_name
    )
    if match is None or not view.viewer.sees_agent(match):
        raise UnknownAgent(f"no agent named {new.agent_name!r} found here")
    _check_agent_pin(
        policy,
        caller,
        agent_name=new.agent_name,
        agent=build_agent_ref(match.name, match.metadata, new.agent_name),
        kind=new.destination_kind,
        destination_id=new.destination_id,
        destination_channel_id=destination_channel_id,
        origin=view.origin,
    )
    await _require_agent_in_scope(
        env,
        caller,
        agent_name=new.agent_name,
        ma_agent_id=match.id,
        destination_channel_id=destination_channel_id,
    )
    async with env.sessionmaker() as session, session.begin():
        created = await routines_store.create_routine(
            session,
            tenant_id=caller.tenant_id,
            created_by_user_id=caller.user_id,
            agent_id=match.id,
            agent_name=new.agent_name,
            cron_expr=new.cron_expr,
            timezone_=new.timezone,
            trigger_message=new.trigger_message,
            enabled=new.enabled,
            catch_up_policy=new.catch_up_policy,
            next_fire_at=next_fire_at,
            destination_kind=new.destination_kind,
            destination_id=new.destination_id,
            channel_id=budget_channel_id,
        )
        await _record(env, session, "created", created)
        return created


async def list_routines(env: RoutineEnv, caller: RoutineCaller) -> list[RoutineRow]:
    """The routines the caller may see: their own, or every one for an admin."""
    _require_allowed(caller)
    viewer = (await _view(env)).viewer
    async with env.sessionmaker() as session:
        rows = await routines_store.list_routines_for_tenant(session, tenant_id=caller.tenant_id)
    return [row for row in rows if may_read(caller, row) and sees_routine(viewer, row)]


async def get_routine(env: RoutineEnv, caller: RoutineCaller, routine_id: uuid.UUID) -> RoutineRow:
    _require_allowed(caller)
    async with env.sessionmaker() as session:
        row = await routines_store.get_routine(session, routine_id, tenant_id=caller.tenant_id)
    if row is None or not may_read(caller, row):
        raise RoutineNotFound()
    if not sees_routine((await _view(env)).viewer, row):
        raise RoutineNotFound()
    return row


async def update_routine(
    env: RoutineEnv, caller: RoutineCaller, routine_id: uuid.UUID, change: RoutineChange
) -> RoutineRow:
    """Change the fields given on a routine the caller owns (any, for an admin)."""
    _require_allowed(caller)
    tenant_id = caller.tenant_id
    if change.clear_destination and (
        change.destination_kind is not None or change.destination_id is not None
    ):
        raise RoutineRefused("clear_destination cannot be combined with a new destination")
    new_destination_channel_id = await check_destination(
        env, caller, kind=change.destination_kind, destination_id=change.destination_id
    )
    viewer = (await _view(env)).viewer
    async with env.sessionmaker() as session, session.begin():
        row = await routines_store.get_routine(session, routine_id, tenant_id=tenant_id)
        if row is None or not sees_routine(viewer, row):
            raise RoutineNotFound()
        _require_owner(caller, row)
        if change.clear_destination:
            destination_channel_id = None
        elif change.destination_id is not None:
            destination_channel_id = new_destination_channel_id
        else:
            destination_channel_id = routine_destination_channel(row)
        wanted = change.agent_name or row.agent_name
        if not viewer.sees(wanted):
            raise UnknownAgent(f"no agent named {wanted!r} found here")
        if change.clear_destination:
            effective_kind, effective_id = None, None
        elif change.destination_kind is not None:
            effective_kind, effective_id = change.destination_kind, change.destination_id
        else:
            effective_kind, effective_id = row.destination_kind, row.destination_id
        policy = await _load_policy(session, tenant_id)

        # Recompute next_fire_at only when cron or timezone is being changed.
        next_fire_at: datetime | None = None
        if change.cron_expr is not None or change.timezone is not None:
            next_fire_at = next_fire(
                env,
                change.cron_expr if change.cron_expr is not None else row.cron_expr,
                change.timezone if change.timezone is not None else row.timezone,
            )

        # A new name is resolved to the live agent id; an unknown name is refused.
        new_agent_id: str | None = None
        if change.agent_name is not None and change.agent_name != row.agent_name:
            match = await find_agent_by_daimon_tag(
                env.anthropic, tenant_id=tenant_id, name=change.agent_name
            )
            if match is None:
                raise UnknownAgent(f"no agent named {change.agent_name!r} found here")
            new_agent_id = match.id
        if any_agent_rules(policy) or any_own_readers(policy):
            # Check the agent the routine will run, by all its names.
            effective_ma_id = new_agent_id or row.agent_id
            effective_agent = next(
                (
                    agent
                    for agent in await list_agents_by_tenant(env.anthropic, tenant_id=tenant_id)
                    if agent.id == effective_ma_id
                ),
                None,
            )
            if effective_agent is None:
                raise RoutineRefused("the routine's agent no longer exists. Nothing was saved.")
            effective_name = change.agent_name if new_agent_id is not None else row.agent_name
            _check_agent_pin(
                policy,
                caller,
                agent_name=effective_name or effective_agent.name,
                agent=build_agent_ref(
                    effective_agent.name, effective_agent.metadata, effective_name
                ),
                kind=effective_kind,
                destination_id=effective_id,
                destination_channel_id=destination_channel_id,
            )
        if change.clear_destination:
            effective_channel_id = None
        elif change.destination_kind is not None:
            effective_channel_id = new_destination_channel_id
        else:
            effective_channel_id = destination_channel_of(
                caller.platform, row.destination_kind, row.destination_id
            )
        await _require_agent_in_scope(
            env,
            caller,
            agent_name=change.agent_name
            if change.agent_name is not None and new_agent_id
            else row.agent_name,
            ma_agent_id=new_agent_id if new_agent_id is not None else row.agent_id,
            destination_channel_id=effective_channel_id,
        )
        updated = await routines_store.update_routine(
            session,
            routine_id,
            tenant_id=tenant_id,
            cron_expr=change.cron_expr,
            timezone_=change.timezone,
            trigger_message=change.trigger_message,
            enabled=change.enabled,
            catch_up_policy=change.catch_up_policy,
            agent_id=new_agent_id,
            agent_name=change.agent_name if new_agent_id is not None else None,
            next_fire_at=next_fire_at,
            destination_kind=change.destination_kind,
            destination_id=change.destination_id,
            clear_destination=change.clear_destination,
            channel_id=new_destination_channel_id,
        )
        if updated is None:
            raise RoutineNotFound()
        await _record(env, session, "changed", updated)
        return updated


async def set_enabled(
    env: RoutineEnv, caller: RoutineCaller, routine_id: uuid.UUID, *, enabled: bool
) -> RoutineRow:
    """Pause a routine or resume it from its next slot."""
    _require_allowed(caller)
    viewer = (await _view(env)).viewer
    async with env.sessionmaker() as session, session.begin():
        row = await routines_store.get_routine(session, routine_id, tenant_id=caller.tenant_id)
        if row is None or not sees_routine(viewer, row):
            raise RoutineNotFound()
        _require_owner(caller, row)
        changed = (
            await routines_store.resume_routine(
                session, routine_id, tenant_id=caller.tenant_id, now=env.now()
            )
            if enabled
            else await routines_store.pause_routine(session, routine_id, tenant_id=caller.tenant_id)
        )
        if changed is None:
            raise RoutineNotFound()
        await _record(env, session, "resumed" if enabled else "paused", changed)
        return changed


async def delete_routine(env: RoutineEnv, caller: RoutineCaller, routine_id: uuid.UUID) -> None:
    _require_allowed(caller)
    viewer = (await _view(env)).viewer
    async with env.sessionmaker() as session, session.begin():
        row = await routines_store.get_routine(session, routine_id, tenant_id=caller.tenant_id)
        if row is None or not sees_routine(viewer, row):
            raise RoutineNotFound()
        _require_owner(caller, row)
        if not await routines_store.delete_routine(session, routine_id, tenant_id=caller.tenant_id):
            raise RoutineNotFound()
        await _record(env, session, "deleted", row)
