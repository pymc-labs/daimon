"""Who may read a lead: one rule for every reader, on every platform path.

A record's readers are fixed when it is captured, by the destination stored on
it (tenant, guild, sales channel). Every read also needs live native read
permission there: View Channel and Read Message History on the parent channel,
and the same on the dossier thread once one exists or may exist, plus the
tenant's channel rules for the executing agent and where it runs. Changing an
intake setting never widens who can read a past record.

An unclassified mailbox discovery stores no message content (an encrypted
message id, a digest and timestamps), so it is read against the current
destination; submitting it snapshots that destination on the new record.

Decision logic only: each adapter supplies native permission facts through
`NativeLeadPermissions`, so no adapter imports another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.lead_intake import IntakeConfig, IntakeRecord, authorize_intake_actor
from daimon.core.membership import Membership
from daimon.core.permissions import any_own_readers


class LeadReadDenied(PermissionError):
    """A lead read was refused; `str()` is a stable, content-free reason."""


@dataclass(frozen=True)
class LeadReadTarget:
    """What a read would disclose, and where its readers are fixed."""

    tenant_id: UUID
    guild_id: str
    channel_id: str
    # The actual dossier thread, or the starter id while its creation is uncertain.
    thread_id: str | None = None
    # A thread effect exists but neither a thread nor a starter id does.
    thread_unresolvable: bool = False


@dataclass(frozen=True)
class LeadReader:
    """The live person, and the execution reading on their behalf.

    `config` is the current intake configuration, used only for admission
    (agent, routine, resolvers); it never names the target. `agent` is the
    executing agent (`AgentRef.none()` for a person's own ephemeral reply);
    `origin` is where the execution runs, which holds an agent kept to its
    own channel there.
    """

    tenant_id: UUID
    platform_user_id: str
    facts: Membership
    policy: TenantAccessPolicy
    config: IntakeConfig
    agent_id: UUID | None
    agent: AgentRef = field(default_factory=AgentRef.none)
    origin: Place | None = None
    origin_channel_ids: frozenset[str] = frozenset()
    routine_id: UUID | None = None
    execution_active: bool = False
    resolve: bool = False


@dataclass(frozen=True)
class NativeChannel:
    """A platform's live answer about one channel or thread, for one member."""

    id: str
    guild_id: str
    parent_id: str | None
    category_id: str | None
    is_text: bool
    is_thread: bool
    private_thread: bool
    everyone_can_view: bool
    can_view: bool
    can_read_history: bool
    # Member of a private thread, or allowed to manage threads.
    in_thread_or_manages: bool


class NativeLeadPermissions(Protocol):
    async def channel(self, channel_id: str) -> NativeChannel | None:
        """The channel's facts for the reader; None when missing or forbidden."""
        ...


def actual_thread_id(record: IntakeRecord) -> str | None:
    """The dossier thread a record's effects created, once its id is known."""
    return record.progress.discord_thread_id or next(
        (e.remote_id for e in record.effects if e.step == "thread" and e.remote_id), None
    )


def read_target(record: IntakeRecord) -> LeadReadTarget:
    """A record's readers: its capture-time destination and its dossier thread."""
    if record.destination is None:
        raise LeadReadDenied("lead_destination_missing")
    thread_id = actual_thread_id(record)
    unresolvable = False
    if thread_id is None and any(e.step == "thread" for e in record.effects):
        # Discord creates a message thread at its starter's id. Until an
        # uncertain creation is reconciled, reads check that possible thread.
        thread_id = record.progress.discord_message_id or next(
            (e.remote_id for e in record.effects if e.step == "starter" and e.remote_id), None
        )
        unresolvable = thread_id is None
    return LeadReadTarget(
        tenant_id=record.tenant_id,
        guild_id=record.destination.guild_id,
        channel_id=record.destination.channel_id,
        thread_id=thread_id,
        thread_unresolvable=unresolvable,
    )


def discovery_target(config: IntakeConfig, tenant_id: UUID) -> LeadReadTarget:
    """An unclassified discovery holds no message content: the current destination reads it."""
    return LeadReadTarget(
        tenant_id=tenant_id, guild_id=config.guild_id, channel_id=config.channel_id
    )


def _require_rule(reader: LeadReader, place: Place) -> None:
    decision = authorize(
        reader.policy,
        subject=Subject(),
        action=Action.READ_CHANNEL,
        agent=reader.agent,
        place=place,
        origin_channel_ids=reader.origin_channel_ids,
        origin=reader.origin,
    )
    if not decision:
        raise LeadReadDenied(f"lead_read_{decision.reason or 'denied'}")


async def authorize_lead_read(
    target: LeadReadTarget, reader: LeadReader, native: NativeLeadPermissions
) -> None:
    """Allow the whole read or raise `LeadReadDenied`; there is no admin bypass."""
    if not target.guild_id or not target.channel_id:
        raise LeadReadDenied("lead_destination_missing")
    if target.tenant_id != reader.tenant_id or target.guild_id != reader.config.guild_id:
        raise LeadReadDenied("lead_tenant_mismatch")
    if target.thread_unresolvable:
        raise LeadReadDenied("dossier_thread_reconciliation_required")
    try:
        authorize_intake_actor(
            reader.config,
            reader.policy,
            user_id=reader.platform_user_id,
            facts=reader.facts,
            agent_id=reader.agent_id,
            routine_id=reader.routine_id,
            execution_active=reader.execution_active,
            resolve=reader.resolve,
        )
    except PermissionError as exc:
        raise LeadReadDenied(str(exc)) from None
    if any_own_readers(reader.policy) and reader.agent.present and not reader.agent.resolved:
        raise LeadReadDenied("lead_read_agent_unresolved")
    parent = await native.channel(target.channel_id)
    if parent is None or not parent.is_text or parent.guild_id != target.guild_id:
        raise LeadReadDenied("sales_channel_required")
    if parent.everyone_can_view:
        raise LeadReadDenied("restricted_sales_channel_required")
    if not (parent.can_view and parent.can_read_history):
        raise LeadReadDenied("sales_channel_history_required")
    _require_rule(reader, Place(channel_id=parent.id, category_id=parent.category_id))
    if target.thread_id is None:
        return
    thread = await native.channel(target.thread_id)
    if (
        thread is None
        or not thread.is_thread
        or thread.guild_id != target.guild_id
        or thread.parent_id != target.channel_id
    ):
        raise LeadReadDenied("dossier_thread_mismatch")
    if not (thread.can_view and thread.can_read_history):
        raise LeadReadDenied("dossier_thread_history_required")
    if thread.private_thread and not thread.in_thread_or_manages:
        raise LeadReadDenied("dossier_thread_membership_required")
    _require_rule(
        reader,
        Place(
            channel_id=thread.id,
            parent_channel_id=target.channel_id,
            category_id=parent.category_id,
        ),
    )


__all__ = [
    "LeadReadDenied",
    "LeadReadTarget",
    "LeadReader",
    "NativeChannel",
    "NativeLeadPermissions",
    "actual_thread_id",
    "authorize_lead_read",
    "discovery_target",
    "read_target",
]
