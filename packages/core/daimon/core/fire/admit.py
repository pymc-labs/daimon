"""Whether an unattended turn may run as a person at a place: one rule for routines and steps.

`admit_place` decides tenant, provider, access policy, pins, lapsed role, membership waits,
restricted source, direct posting, hold and origin place. Reads are injected; the caller
records the verdict (`fire.run.fire_turn`, the workflow lane turn).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import structlog
from daimon.core.access_policy import TenantAccessPolicy, agent_invoker_roles
from daimon.core.authz import Action, AgentRef, Place, Surface, authorize, build_subject
from daimon.core.authz import Subject as Invoker
from daimon.core.bot_access import BOT_ACCESS_UNAVAILABLE, bot_access_blocked, bot_access_wait_on
from daimon.core.config import Settings
from daimon.core.fire.subject import Subject
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.permissions import any_agent_rules, any_own_readers
from daimon.core.routine_delivery import (
    OWNER_LACKS_ROLE_ERROR,
    DirectPost,
    delivery_target,
    lapsed_role,
    placement_unknown_is_unsafe,
)
from daimon.core.routine_hold import routine_hold
from daimon.core.routine_provider import place_provider
from daimon.core.rule_views import (
    RoutineOrigin,
    is_routine_parent_unknown,
    keeps_routine_inside,
    routine_destination_place,
    routine_origin,
)
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import PlatformPrincipalRow, Role
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.tenants import get_tenant
from daimon.core.turn_hold import TurnHold
from mux.contracts.config import DEFAULT_BACKEND
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: A DM routine whose source is restricted: its result may not leave that channel.
RESTRICTED_SOURCE_ERROR = "restricted source cannot deliver by DM"
SELFHOSTED_ROUTINES_UNSUPPORTED = "selfhosted_routines_unsupported"
#: Waits that clear by themselves: retried, nobody told.
WAITS = frozenset({"membership_unavailable", BOT_ACCESS_UNAVAILABLE})


@dataclass(frozen=True)
class AdmitReads:
    """What admission reads with; `FireDeps.reads` builds one from a fire's runtime."""

    sm: async_sessionmaker[AsyncSession]
    settings: Settings
    membership_resolver: MembershipResolver | None
    get_or_create_platform_principal: Callable[..., Awaitable[PlatformPrincipalRow]] = (
        get_or_create_platform_principal
    )
    bot_access_wait_on: Callable[..., Awaitable[bool]] = bot_access_wait_on
    bot_access_blocked: Callable[..., Awaitable[bool]] = bot_access_blocked
    #: Whether a fire may DM its owner; None: a DM destination is closed.
    dm_open: Callable[..., Awaitable[bool]] | None = None
    #: Whether the agent's DM grant admits the owner; None: it does not.
    owner_granted: Callable[..., Awaitable[bool]] | None = None


@dataclass(frozen=True)
class Admitted:
    """May run: where, as whom, and under which hold."""

    platform: str
    account_id: uuid.UUID
    #: The non-Anthropic provider the place runs on, None on Managed Agents.
    native: str | None
    direct_post: DirectPost
    invoker: Invoker
    policy: TenantAccessPolicy
    channel_id: str | None
    hold: TurnHold | None
    origin_place: RoutineOrigin | None


@dataclass(frozen=True)
class Refused:
    """May not run. `lapsed`: the owner lost a role the agent needs."""

    reason: str
    error: str | None = None
    fault: bool = False
    tell: bool = True
    platform: str | None = None
    lapsed: bool = False


@dataclass(frozen=True)
class Waiting:
    """Not now: the platform or the bot's access could not say; retried, nobody told."""

    reason: str

    @property
    def fault(self) -> bool:
        return self.reason == "membership_unavailable"


Verdict = Admitted | Refused | Waiting


async def owner_dm_open(reads: AdmitReads, s: AsyncSession, facts: Subject, platform: str) -> bool:
    """Whether a fire may DM its owner: Discord, routine DMs on, owner allowed."""
    owner = facts.created_by_user_id
    return (
        owner is not None
        and reads.dm_open is not None
        and await reads.dm_open(
            s, reads.settings, tenant_id=facts.tenant_id, platform=platform, owner=owner
        )
    )


async def _owner_granted(reads: AdmitReads, facts: Subject, owner: str) -> bool:
    """The agent's DM grant admits the owner, read before the admitting transaction."""
    if reads.owner_granted is None:
        return False
    async with reads.sm() as s:
        tenant = await get_tenant(s, facts.tenant_id)
    return tenant is not None and await reads.owner_granted(
        reads.sm,
        reads.membership_resolver,
        tenant=tenant,
        agent_name=facts.agent_name,
        owner=owner,
    )


async def _bot_locked_out(reads: AdmitReads, facts: Subject) -> bool:
    """Whether the workspace waits for the bot's access and Discord says it has none here.

    Read before the admitting transaction, so the REST call holds no connection.
    """
    target = delivery_target(facts, platform="discord")
    if target is None or not target.channel_id.isdigit():
        return False
    async with reads.sm() as s:
        tenant = await get_tenant(s, facts.tenant_id)
    if tenant is None or tenant.platform != "discord":
        return False
    if not await reads.bot_access_wait_on(reads.sm, tenant_id=facts.tenant_id, platform="discord"):
        return False
    async with httpx.AsyncClient(timeout=10) as http:
        return await reads.bot_access_blocked(
            http, reads.settings, guild_id=tenant.external_id, destination_id=target.channel_id
        )


def _policy_verdict(facts: Subject, error: str, platform: str) -> Refused | Waiting:
    log.info(
        "routine.skipped.invoker_policy",
        subject_id=str(facts.id),
        tenant_id=str(facts.tenant_id),
        reason=error,
    )
    if error in WAITS:
        return Waiting(error)
    if error == OWNER_LACKS_ROLE_ERROR:
        return Refused(error, platform=platform, lapsed=True)
    reason = "restricted_source" if error == RESTRICTED_SOURCE_ERROR else error
    return Refused(reason, error=error)


async def admit_place(facts: Subject, reads: AdmitReads) -> Verdict:
    """The verdict on one unattended turn as `facts`' owner at its destination, now.

    Invoker policy at fire time: someone taken off the allowlist stops running headless
    turns as themselves. The creator facts come from live membership, never a saved admin
    role. Unreadable policy fails closed.
    """
    owner = facts.created_by_user_id
    if owner is None:
        return Refused("no_owner", error="This routine has no owner. Create it again.")
    granted = facts.destination_kind != "dm" or await _owner_granted(reads, facts, owner)
    bot_locked_out = await _bot_locked_out(reads, facts)
    async with reads.sm() as s, s.begin():
        tenant = await get_tenant(s, facts.tenant_id)
        if tenant is None:
            return Refused("tenant_not_found", error="The routine's team was not found", fault=True)
        if tenant.archived_at is not None:
            return Refused("tenant_archived", error="Daimon was removed from here.")
        platform = tenant.platform
        if facts.destination_kind == "dm" and not (
            granted and await owner_dm_open(reads, s, facts, platform)
        ):
            return Refused(
                "routine_dms_closed" if granted else "owner_not_allowed",
                error="routine DMs are disabled or the owner is not allowed",
            )
        # A thread, channel or agent on another provider is decided before anything is
        # resolved at Anthropic: one on OpenAI or Gemini runs through the chat path.
        if is_routine_parent_unknown(facts):
            return Refused("routine_destination_parent_unknown", fault=True)
        provider = await place_provider(s, facts, platform=platform, home=facts.home_channel_id)
        native = provider if provider != DEFAULT_BACKEND else None
        if native == "selfhosted":
            return Refused("routines_unsupported", error=SELFHOSTED_ROUTINES_UNSUPPORTED)
        principal = await reads.get_or_create_platform_principal(
            s, tenant_id=facts.tenant_id, platform=platform, external_id=owner
        )
        try:
            policy = await load_access_policy(s, tenant_id=facts.tenant_id)
        except AccessPolicyUnreadable:
            return _policy_verdict(facts, "access_policy_unreadable", platform)
        # A destination protected since it was saved is never offered to the agent as a
        # place to post; nor is one whose parent or category the policy may protect.
        target = delivery_target(facts, platform=platform)
        channel_id = target.channel_id if target is not None else None
        direct_post: DirectPost = "allowed"
        if channel_id is not None:
            if not authorize(
                policy,
                subject=Invoker(),
                action=Action.POST,
                surface=Surface.ROUTINE,
                place=Place(channel_id=channel_id),
            ):
                direct_post = "protected"
            elif placement_unknown_is_unsafe(
                policy, platform=platform, kind=facts.destination_kind
            ):
                direct_post = "unverified"
        account = await get_account(s, principal.account_id) if platform != "discord" else None
        is_admin = account is not None and account.role is Role.ADMIN
        invoker = build_subject(is_admin=is_admin, platform_user_id=owner)
        membership_required = platform == "discord" and (
            bool(policy.invoker_role_ids)
            or reads.membership_resolver is not None
            or facts.destination_kind == "dm"
        )
        if membership_required:
            invoker = await resolve_subject(
                reads.sm,
                reads.membership_resolver,
                tenant_id=facts.tenant_id,
                user_id=owner,
                require_fresh=True,
                session=s,
            )
        allowed = authorize(
            policy, subject=invoker, action=Action.ACT_FOR_CREATOR, surface=Surface.ROUTINE
        )
        error: str | None = (
            None
            if allowed and (not membership_required or invoker.verified_membership is True)
            else "invoker_not_allowed"
        )
        if (
            error is not None
            and membership_required
            and invoker.verified_membership is None
            and agent_invoker_roles(policy, (facts.agent_name,))
        ):
            # The platform could not say: retry next fire, tell nobody.
            error = "membership_unavailable"
        if facts.destination_kind == "dm" and keeps_routine_inside(policy, facts):
            error = RESTRICTED_SOURCE_ERROR
        # A pinned agent runs only straight into one of its pinned channels. Isolation
        # needs every name, so it waits for the resolved agent (`ran_agent_refusal`).
        if error is None:
            initial = authorize(
                policy,
                subject=invoker,
                action=Action.RUN_AGENT,
                surface=Surface.ROUTINE,
                agent=AgentRef.of(facts.agent_name),
                place=Place(channel_id=channel_id or facts.home_channel_id),
            )
            if initial.reason in ("runs_elsewhere", "invoker_not_allowed"):
                error = (
                    OWNER_LACKS_ROLE_ERROR
                    if initial.reason == "invoker_not_allowed"
                    and lapsed_role(policy, (facts.agent_name,), invoker)
                    else initial.reason
                )
        # The bot's own access comes after every owner check, so a revoked owner still
        # stops the turn.
        if error is None and bot_locked_out:
            error = BOT_ACCESS_UNAVAILABLE
    if error is not None:
        return _policy_verdict(facts, error, platform)
    # A destination in a channel shared with another organisation is read by people from
    # it, so the turn runs held to that channel. It never runs unheld.
    async with reads.sm() as hold_s:
        hold = await routine_hold(hold_s, facts, platform=platform)
    return Admitted(
        platform=platform,
        account_id=principal.account_id,
        native=native,
        direct_post=direct_post,
        invoker=invoker,
        policy=policy,
        channel_id=channel_id,
        hold=hold,
        # Stamped like a turn in the destination, so an isolated or sealed channel's
        # transcript reads only from inside it.
        origin_place=routine_origin(policy, facts, platform=platform),
    )


def checks_ran_agent(admitted: Admitted) -> bool:
    """Whether the agent that will run must be checked again by every name it has."""
    return any_agent_rules(admitted.policy) or any_own_readers(admitted.policy)


def ran_agent_refusal(admitted: Admitted, facts: Subject, ran: AgentRef) -> Refused | None:
    """Refused when the agent that will run, by every name a pin can be keyed by, may not
    run at the destination; `lapsed` when the owner lost the role it needs."""
    decision = authorize(
        admitted.policy,
        subject=admitted.invoker,
        action=Action.RUN_AGENT,
        surface=Surface.ROUTINE,
        agent=ran,
        # A thread sits under its saved parent channel; one saved without it can't be
        # placed and fails closed while anything is isolated.
        # A step with no place runs at its workflow's home.
        place=routine_destination_place(
            facts, channel_id=admitted.channel_id or facts.home_channel_id
        ),
    )
    if decision.reason is None:
        return None
    log.info(
        "routine.skipped.invoker_policy",
        subject_id=str(facts.id),
        tenant_id=str(facts.tenant_id),
        reason=decision.reason,
    )
    if decision.reason == "invoker_not_allowed" and lapsed_role(
        admitted.policy, ran.names, admitted.invoker
    ):
        return Refused(OWNER_LACKS_ROLE_ERROR, platform=admitted.platform, lapsed=True)
    return Refused(decision.reason)
