"""Hand a thread's conversation to another agent, decided under the policy lock.

Every way a person moves a thread to another agent ends here: the
`hand_off_task` tool the current agent calls when asked, and the button on the
notice a thread shows when its channel now answers with an agent the thread's
session does not belong to. The switch only writes the thread's binding. The
caller's next message in the thread builds the new agent's session, carries
the old one's work across (`daimon.core.session_preparation`) and closes the
old one.

Lock order, held to the end of the caller's transaction:

1. the tenant policy lock (`lock_access_policy`, FOR NO KEY UPDATE on the
   tenant row);
2. the requester's account row, FOR KEY SHARE;
3. the thread's binding row, FOR UPDATE (`lock_binding`);
4. whatever the caller writes after `hand_over_thread` returns (its own
   thread session row, a queued continuation).

Session creation, seal publication and replacement closure take the same tenant
lock in short DB transactions. Session seals are monotone, and NULL legacy
facts fail closed until collected. The decision reads the live set and these
facts under step 1, without MA I/O. Preparation's advisory lock precedes the
session mutation advisory fence; sends take only the mutation fence and read
policy without locking it. Neither handoff nor policy writers, purge, audit,
form consume or support acquire either advisory lock, so there is no reverse
edge to those locks. Recovery creates MA before locking or updating DB rows.

The policy is read after 1, so an edit committed first refuses the switch and
one arriving later waits for the commit. Why the order can't deadlock with the
other lock holders: the policy writers (CLI, isolation setup), a private form's
consume and a support escalation take the tenant lock and then only rows of
their own (policy, scoped config, form, ledger), never an account, binding or
thread session row. `security_audit.append_event` takes KEY SHARE on the
tenant and account rows, which neither lock here blocks. Tenant teardown
(`delete_tenant`) takes FOR UPDATE on the tenant row before any child row, so
it queues at step 1. An account purge takes FOR UPDATE on the account row
first and only then reaches its bindings and thread sessions, so it queues at
step 2, or this switch waits there holding only the tenant lock, which the
purge never asks for. Binding writers that hold no tenant lock (setup
conversations, lifecycle updates) never take one afterwards.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import anthropic as anthropic_pkg
import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_reach import load_binding_reach
from daimon.core.authz import (
    Action,
    AgentRef,
    Place,
    SessionFacts,
    Subject,
    Surface,
    authorize,
    build_agent_ref,
    build_subject,
)
from daimon.core.channel_admins import ChannelAdminCaller, load_administered_channel_ids
from daimon.core.continuity.handoff import (
    HandoffRefused,
    HandoffRefusedInSetupThread,
    decide_handoff,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.errors import DaimonError
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.scope import DeploymentDefault, ScopeContext
from daimon.core.session_ports_compat import retrieve_session_record, session_scope
from daimon.core.session_seal import session_facts
from daimon.core.setup_conversations import get_setup_agent
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
    lock_access_policy,
)
from daimon.core.stores.accounts import key_share_lock_account
from daimon.core.stores.domain import ThreadAgentBindingRow
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant, resolve
from daimon.core.stores.thread_agent_bindings import lock_binding, upsert_responder_binding
from daimon.core.stores.thread_sessions import list_live_thread_sessions, record_session_seals
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

__all__ = [
    "HandoffCaller",
    "HandoffDestination",
    "RecordedSession",
    "SwitchOutcome",
    "ThreadHandoffRefused",
    "destination_may_read",
    "hand_over_thread",
    "may_carry_work",
    "recorded_thread_sessions",
    "render_handoff_refused",
    "switch_thread_on_request",
]


@dataclass(frozen=True)
class HandoffDestination:
    """The agent the thread moves to, as resolved by exact MA id."""

    ma_agent_id: str
    name: str
    """Its configuration name: what the cascade and the binding record."""
    agent: AgentRef
    """Every name a pin may be keyed by (`build_agent_ref`)."""
    is_daimon_managed: bool = False


@dataclass(frozen=True)
class HandoffCaller:
    """Who asks for the switch.

    `channel_admin.is_server_admin` is the caller's trusted admin signal (the
    live platform role for a click, the turn's role for the tool). Channel
    admin grants are read inside the lock from `platform_user_id` and
    `role_ids`.
    """

    account_id: uuid.UUID | None
    channel_admin: ChannelAdminCaller
    via_agent_key: bool = False


log = structlog.get_logger(__name__)

# Stands in for a seal that couldn't be read: a session MA wouldn't return,
# or sessions nobody looked up. Inside no place, so it reads as sealed.
# This sentinel is in-memory only; it must never be persisted as a seal.
_UNREAD_SEAL = "\x00unread-session-seal"


@dataclass(frozen=True)
class RecordedSession:
    """A live session in the thread, as its own metadata places and seals it."""

    account_id: uuid.UUID | None
    facts: SessionFacts


async def recorded_thread_sessions(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
) -> tuple[RecordedSession, ...]:
    """Every live session in the thread with the seal it recorded.

    Read before `hand_over_thread` takes its locks, so no MA call runs under
    them. A session that can't be read counts as sealed.
    """
    async with sessionmaker() as session:
        rows = await list_live_thread_sessions(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
    recorded: list[RecordedSession] = []
    for row in rows:
        try:
            ma_session = await retrieve_session_record(
                anthropic,
                row.ma_session_id,
                scope=session_scope(
                    tenant_id=tenant_id,
                    account_id=row.account_id,
                    call_site="thread_handoff:recorded_thread_sessions",
                ),
            )
        except anthropic_pkg.APIError as error:
            log.warning(
                "thread_handoff.session_unreadable",
                session_id=row.ma_session_id,
                error=type(error).__name__,
            )
            facts = SessionFacts(seal_ids=frozenset({_UNREAD_SEAL}))
        else:
            facts = session_facts(ma_session.metadata, owned=True)
            if any(seal.startswith("\x00") for seal in facts.seal_ids):
                # A sentinel (a legacy seal with no channel) can't be stored in
                # JSONB; leaving the row unread keeps the locked read sealed.
                recorded.append(RecordedSession(account_id=row.account_id, facts=facts))
                continue
            async with sessionmaker.begin() as db:
                await record_session_seals(
                    db,
                    tenant_id=tenant_id,
                    ma_session_id=row.ma_session_id,
                    seals=facts.seal_ids,
                    channel_id=facts.channel,
                )
        recorded.append(RecordedSession(account_id=row.account_id, facts=facts))
    return tuple(recorded)


async def locked_thread_sessions(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> tuple[RecordedSession, ...]:
    """Read under the tenant lock, shared by mapping and seal publishers."""
    rows = await list_live_thread_sessions(
        session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
    )
    return tuple(
        RecordedSession(
            account_id=row.account_id,
            facts=SessionFacts(
                channel=row.channel_id,
                thread=row.thread_id,
                owned=True,
                seal_ids=frozenset({_UNREAD_SEAL})
                if row.seal_ids is None
                else frozenset(row.seal_ids),
            ),
        )
        for row in rows
    )


def may_carry_work(
    policy: TenantAccessPolicy,
    *,
    destination: HandoffDestination,
    parent_channel_id: str,
    thread_id: str,
    recorded: Sequence[RecordedSession],
    account_id: uuid.UUID,
) -> bool:
    """Whether `account_id`'s work in the thread may reach the destination.

    The carry rule the next bind applies to the transcript
    (`destination_may_read`), applied here to anything else the switch
    passes on, such as the queued continuation text.
    """
    return all(
        destination_may_read(
            policy,
            agent=destination.agent,
            channel_id=parent_channel_id,
            thread_id=thread_id,
            previous=item.facts,
        )
        for item in recorded
        if item.account_id == account_id
    )


class ThreadHandoffRefused(DaimonError):
    """The switch was refused; nothing was written."""

    def __init__(self, refusal: HandoffRefused) -> None:
        super().__init__(f"handoff refused: {refusal.reason}")
        self.refusal = refusal


async def _subject(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, caller: HandoffCaller
) -> Subject:
    who = caller.channel_admin
    if who.is_server_admin:
        return build_subject(
            is_admin=True, platform_user_id=who.platform_user_id, via_agent_key=caller.via_agent_key
        )
    return build_subject(
        is_admin=False,
        platform_user_id=who.platform_user_id,
        via_agent_key=caller.via_agent_key,
        administered_channel_ids=await load_administered_channel_ids(
            session, tenant_id=tenant_id, platform=platform, caller=who
        ),
    )


async def hand_over_thread(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    caller: HandoffCaller,
    destination: HandoffDestination,
    current_responder_ma_agent_id: str | None,
    default: DeploymentDefault,
    now: datetime,
    recorded: Sequence[RecordedSession] | None = None,
) -> ThreadAgentBindingRow:
    """Decide the switch on the policy as it is now, then bind the thread to `destination`.

    Runs in the caller's transaction and takes the locks in the module
    docstring's order. Raises `ThreadHandoffRefused` before writing anything;
    the caller's transaction then has nothing to roll back but its reads.
    Anything else the caller writes for this switch belongs in the same
    transaction, after this returns, so the decision covers it too.

    `recorded` is the thread's live sessions (`recorded_thread_sessions`).
    Its facts are a conservative floor; the authoritative live set and seals
    are read again under the tenant lock. None (not read) counts as sealed.
    """
    await lock_access_policy(session, tenant_id=tenant_id)
    if caller.account_id is not None:
        await key_share_lock_account(session, account_id=caller.account_id)
    policy = await load_access_policy(session, tenant_id=tenant_id)
    binding = await lock_binding(
        session,
        tenant_id=tenant_id,
        platform=platform,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
    )
    # Session publication and seal stamping take the tenant lock too. No MA
    # request runs here: the live set and monotone facts are authoritative DB reads.
    snapshot_seals = (
        frozenset({_UNREAD_SEAL})
        if recorded is None
        else frozenset(seal for item in recorded for seal in item.facts.seal_ids)
    )
    recorded = await locked_thread_sessions(
        session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
    )
    subject = await _subject(session, tenant_id=tenant_id, platform=platform, caller=caller)
    reachable = await is_agent_reachable_in_tenant(
        session, tenant_id=tenant_id, agent_name=destination.name, default=default
    )
    # Who the channel answers with, ignoring any thread binding.
    channel_config = await resolve(
        session,
        context=ScopeContext(tenant_id=tenant_id, channel_id=parent_channel_id),
        default=default,
    )
    answers_here = channel_config.agent_name == destination.name
    # A channel admin's standing over the destination, read only when it decides.
    reach = (
        await load_binding_reach(
            session,
            tenant_id=tenant_id,
            platform=platform,
            policy=policy,
            agent=destination.agent,
            ma_agent_id=destination.ma_agent_id,
            default=default,
            caller=caller.channel_admin,
            administered=subject.administered_channel_ids,
            channel_id=parent_channel_id,
            is_daimon_managed=destination.is_daimon_managed,
            caller_account_id=caller.account_id,
        )
        if not subject.is_admin
        and not answers_here
        and parent_channel_id in subject.administered_channel_ids
        else None
    )
    access = authorize(
        policy,
        subject=subject,
        action=Action.HAND_OFF,
        surface=Surface.HANDOFF,
        agent=destination.agent,
        # A DM scope runs in no channel, so it is outside every pin.
        place=Place.from_origin(parent_channel_id=parent_channel_id, thread_id=thread_id),
        answers_here=answers_here,
        reach=reach,
        recorded_seal_ids=snapshot_seals
        | frozenset(seal for item in recorded for seal in item.facts.seal_ids),
    )
    decision = decide_handoff(
        destination_ma_agent_id=destination.ma_agent_id,
        destination_name=destination.name,
        destination_reachable=reachable,
        existing_binding_kind=binding.kind if binding is not None else None,
        origin_responder_ma_agent_id=current_responder_ma_agent_id,
        access=access,
    )
    if isinstance(decision, HandoffRefused):
        raise ThreadHandoffRefused(decision)
    return await upsert_responder_binding(
        session,
        tenant_id=tenant_id,
        platform=platform,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
        responder_ma_agent_id=destination.ma_agent_id,
        responder_name=destination.name,
        created_by_account_id=caller.account_id,
        now=now,
    )


def destination_may_read(
    policy: TenantAccessPolicy,
    *,
    agent: AgentRef,
    channel_id: str | None,
    thread_id: str | None,
    previous: SessionFacts,
) -> bool:
    """Whether the new agent, running here, could read the old session's transcript itself.

    The same `authorize(READ_SESSION)` the transcript tools ask: every id
    that sealed the old session must lie in this thread, and a session that
    ran in an isolated channel is read only by that channel's own agents. A
    handoff carries the old work across only when this holds.
    """
    inside: set[str] = {i for i in (channel_id, thread_id) if i is not None}
    if channel_id is not None and thread_id is not None:
        # A Slack thread is sealed by its channel_id:thread_ts form.
        inside.add(f"{channel_id}:{thread_id}")
    return bool(
        authorize(
            policy,
            subject=Subject(),
            action=Action.READ_SESSION,
            agent=agent,
            origin_channel_ids=frozenset(inside),
            session=previous,
        )
    )


def render_handoff_refused(refusal: HandoffRefused, *, channel: str) -> str:
    """Person-facing copy for a refused switch. Nothing was changed in every case."""
    name = refusal.destination_name
    reasons: dict[str, str] = {
        "setup_thread": "This is a setup conversation, so it always answers as Daimon.",
        "writers_none": "Daimon doesn't post here, so no agent can take this over.",
        "invoker_not_allowed": "This workspace limits who can use Daimon, and you aren't on "
        "that list.",
        "runs_elsewhere": f"{name} only runs in other channels, so it can't answer here.",
        "own_agents_only": f"{channel} only runs its own agents, and {name} isn't one of them.",
        "unreachable": f"{name} doesn't answer anywhere in this workspace any more.",
        "same_agent": f"{name} already answers in this conversation.",
        "admin_required": f"{name} isn't an agent of {channel}. A server admin can hand this "
        f"conversation to it, or a channel admin of {channel} when {name} is one of their own "
        "agents.",
        "not_a_reader": f"Only turns inside {channel} read it and {name} isn't one of its "
        "agents, so only a server admin can hand this conversation to it.",
    }
    return f"{reasons[refusal.reason]} Nothing was changed."


@dataclass(frozen=True)
class SwitchOutcome:
    """What a click on the hand-over button did, and the copy to show for it."""

    switched: bool
    text: str
    destination_name: str | None = None


def render_switched(name: str) -> str:
    return (
        f"{name} answers in this conversation from the next message. The conversation "
        f"and its working files move across, and {name} uses its own keys, connections "
        "and memory."
    )


async def switch_thread_on_request(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    ma_agent_id: str,
    caller: ChannelAdminCaller,
    default: DeploymentDefault,
    channel: str,
    now: datetime,
) -> SwitchOutcome:
    """The hand-over button: switch this thread to `ma_agent_id` for the person who clicked.

    The agent id comes from the button, so it is only a request: the agent is
    retrieved by that exact id and must still be a live agent of this tenant,
    and `hand_over_thread` decides it under the lock for the clicker, with
    their live admin role (`caller.is_server_admin`) and their stored channel
    admin grants. `channel` is how the copy names the parent channel.
    """
    try:
        agent = await get_setup_agent(anthropic, tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    except DaimonError:
        return SwitchOutcome(
            switched=False,
            text="That agent is no longer available in this workspace. Nothing was changed.",
        )
    name = agent.metadata.get(MA_METADATA_KEY_NAME)
    if not name:
        # As `hand_off_task`: the cascade and the binding name agents by
        # their configuration name, so an agent without one can't answer here.
        return SwitchOutcome(
            switched=False,
            text="That agent has no configuration name, so nobody can reach it by name. "
            "Nothing was changed.",
        )
    # Only a channel admin's switch reads the sessions' seals; anyone else's
    # is decided without them. Grants are read again under the lock, and a
    # channel admin whose seals weren't read is refused there as sealed.
    recorded: tuple[RecordedSession, ...] | None = None
    channel_admin = False
    if not caller.is_server_admin:
        async with sessionmaker() as session:
            administered = await load_administered_channel_ids(
                session, tenant_id=tenant_id, platform=platform, caller=caller
            )
        channel_admin = parent_channel_id in administered
        if channel_admin:
            recorded = await recorded_thread_sessions(
                anthropic,
                sessionmaker,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
            )
    try:
        async with sessionmaker.begin() as session:
            principal = (
                None
                if caller.platform_user_id is None
                else await find_platform_principal(
                    session,
                    tenant_id=tenant_id,
                    platform=platform,
                    external_id=caller.platform_user_id,
                )
            )
            await hand_over_thread(
                session,
                tenant_id=tenant_id,
                platform=platform,
                parent_channel_id=parent_channel_id,
                thread_id=thread_id,
                caller=HandoffCaller(
                    account_id=principal.account_id if principal is not None else None,
                    channel_admin=caller,
                ),
                destination=HandoffDestination(
                    ma_agent_id=agent.id,
                    name=name,
                    agent=build_agent_ref(agent.name, agent.metadata, name),
                    is_daimon_managed=agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
                ),
                # The session here belongs to another agent; a second click
                # rewrites the same binding.
                current_responder_ma_agent_id=None,
                default=default,
                now=now,
                recorded=recorded,
            )
    except ThreadHandoffRefused as refused:
        if refused.refusal.authz_reason is not None:
            await _audit_click(
                sessionmaker,
                tenant_id=tenant_id,
                platform=platform,
                caller=caller,
                outcome="denied",
                reason=f"authz:{refused.refusal.authz_reason}",
            )
        return SwitchOutcome(
            switched=False, text=render_handoff_refused(refused.refusal, channel=channel)
        )
    except HandoffRefusedInSetupThread:
        return SwitchOutcome(
            switched=False,
            text="This is a setup conversation, so it always answers as Daimon. "
            "Nothing was changed.",
        )
    except AccessPolicyUnreadable as error:
        return SwitchOutcome(switched=False, text=str(error))
    if caller.is_server_admin or channel_admin:
        # As `hand_off_task`'s trail: an admin's click may rest on admin standing.
        await _audit_click(
            sessionmaker,
            tenant_id=tenant_id,
            platform=platform,
            caller=caller,
            outcome="allowed",
            reason="completed",
        )
    return SwitchOutcome(switched=True, text=render_switched(name), destination_name=name)


async def _audit_click(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    caller: ChannelAdminCaller,
    outcome: PanelOutcome,
    reason: str,
) -> None:
    if caller.platform_user_id is not None:
        await record_panel_write(
            sessionmaker,
            tenant_id=tenant_id,
            platform=platform,
            platform_user_id=caller.platform_user_id,
            op="handoff",
            outcome=outcome,
            reason=reason,
        )
