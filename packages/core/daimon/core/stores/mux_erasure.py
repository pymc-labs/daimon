"""Account erasure of the provider-neutral (mux) state, and the fence that keeps it erased.

The mux tables (`stores.mux_state`) carry the owning account as plain text, with no
foreign key to `accounts`, so deleting an account removes none of them. This module is
the one inventory of what belongs to an account; `privacy.collect_purge_preview` counts
it and `purge.purge_account` erases it, both through the predicates below.

What belongs to an account
--------------------------
* Its private slots: `provider_binding_slot.account_id` is the account's id, in any
  tenant. A slot owns its binding generations, its journal sessions and their events
  (and projections), all of them erased with it.
* Its `thread_lease` and `thread_provider_release` rows, keyed by the same text column
  and erased even when no slot is left to own them.
* Its operations: `operation.account_id` is the account, or `operation.principal_id` is
  one of its principals. An operation whose recorded slot is a shared one is the one
  thing kept: the shared conversation's policy is not decided here, so it is counted as
  retained and left untouched.

What is deliberately not touched
--------------------------------
* Shared slots (`account_id` NULL) and everything under them.
* Other accounts' rows, byte for byte.
* `usage_observation` and `accounting_outbox`, the tenant's settlement evidence. They are
  detached from the slot (`AccountingBindingContext` keeps what settling them reads) and
  settle and replay exactly as before.
* Synthetic billing operations (`selfhosted-billing:<binding id>`): no account's id or
  principal matches them.

A private slot's `account_id` is never cleared or rewritten: NULL means shared.

The fence
---------
Every writer of an account's private mux state calls `fence_writers` first. It takes the
account's advisory lock *shared* (`lock_account` is the exclusive side, held by purge for
its whole transaction), then refuses an account found in `erased_account`; an operation's
principal is fenced the same way against `erased_principal`. A write that began before
the erasure therefore finishes first and is erased with the rest; one that begins after
it is refused. Lock order, in every transaction that uses it:

1. the account advisory lock (shared for writers, exclusive for purge and the
   self-hosted resource writers);
2. the principal advisory locks, in id order;
3. tenant-level locks (self-hosted resources);
4. row locks (slot, lease, operation, journal head, observation advisory lock).

Purge takes (1) and (2) first and deletes children before parents, so it never waits on a
row lock held by a writer that has not yet taken them. A caller therefore takes the
fence before it locks any row it later hands to a store, and does its reads and writes
in the one transaction that holds it (see `thread_release.apply_thread_release`):
a second transaction asking for the fence queues behind a waiting purge.

Writers that predate the fence are held to the same rule by database triggers
(`daimon.core._erasure_guards`, installed by migration 0142).

A shared lease, shared slot or tenant-owned operation names no account, so it is never
fenced; neither is an account id that is not a UUID (synthetic and operator identities).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, fields
from typing import Any, cast

from daimon.core._models import (
    AccountingBindingContext,
    AccountingOutbox,
    ErasedAccount,
    ErasedPrincipal,
    Journal,
    JournalSession,
    MuxOperation,
    ProviderBinding,
    ProviderBindingSlot,
    ThreadLease,
    ThreadProviderRelease,
    UsageObservation,
)
from daimon.core.stores.selfhosted_usage import context_account
from mux.errors import ScopeViolation
from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Delete,
    Select,
    and_,
    delete,
    distinct,
    exists,
    func,
    not_,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

JSON_OWNERSHIP_FIELDS: dict[str, str] = {
    "provider_binding.binding['legacy_account_id']": (
        "mirrors provider_binding_slot.account_id; erased with the slot's generations"
    ),
    "thread_lease.active (holder, turn_id)": "names no account; erased with the lease row",
    "operation.record['account_id'], ['principal_id']": (
        "duplicate the operation's account_id and principal_id columns, which select it"
    ),
    "operation.record['slot']['account_id']": (
        "NULL means the operation ran against a shared slot, which is kept"
    ),
    "journal_session.projection['session']['account_id']": "erased with the conversation head",
    "journal.event": "no participant field; erased with the head of a private conversation",
    "usage_observation.applied['observation']['session']['account_id']": (
        "financial evidence, retained; settlement compares whole resource refs"
    ),
    "accounting_outbox.row['observation']['session']['account_id']": (
        "financial evidence, retained; settlement compares whole resource refs"
    ),
}
"""Every JSON path that can name an account, and why none of them is searched.

Selection uses relational columns only; these paths are erased or kept with their row."""

TABLE_DISPOSITIONS: dict[str, str] = {
    "provider_binding_slot": "erased when account_id is the account; shared (NULL) kept",
    "provider_binding": "erased with a private slot (every generation)",
    "journal_session": "erased with a private slot, with its projection",
    "journal": "erased with the heads of a private slot's conversations",
    "thread_lease": "erased by account_id",
    "thread_provider_release": "erased by account_id",
    "operation": (
        "erased by account_id or principal_id unless it ran against a shared slot (kept, "
        "counted); synthetic billing operations are not matched"
    ),
    "usage_observation": "retained financial evidence, detached from the slot",
    "accounting_outbox": "retained financial evidence, detached from the slot",
    "accounting_binding_context": "the profile and channel settlement reads; no personal data",
    "erased_account": "the fence; a bare account id",
    "erased_principal": "the fence for operations keyed by principal; bare ids",
    "channel_config_revision": "tenant-owned channel configuration, not a person's data",
}
"""Disposition of every mux table, checked against the schema by the privacy tests."""


@dataclass(frozen=True)
class MuxErased:
    """What account erasure removes from the mux tables: previewed, then deleted."""

    slots: int = 0
    binding_generations: int = 0
    journal_sessions: int = 0
    journal_events: int = 0
    leases: int = 0
    release_requests: int = 0
    operations: int = 0

    @property
    def total(self) -> int:
        return sum(getattr(self, field.name) for field in fields(self))


@dataclass(frozen=True)
class MuxRetained:
    """What stays after erasure, for the preview to disclose."""

    shared_conversations: int = 0
    shared_operations: int = 0
    accounting_records: int = 0

    @property
    def total(self) -> int:
        return self.shared_conversations + self.shared_operations + self.accounting_records


@dataclass(frozen=True)
class MuxInventory:
    erased: MuxErased
    retained: MuxRetained


def _account_key(account_id: uuid.UUID) -> str:
    return str(account_id)


def _private_slot_ids(account_key: str) -> Select[tuple[str]]:
    return select(ProviderBindingSlot.binding_id).where(
        ProviderBindingSlot.account_id == account_key
    )


def _private_session_ids(account_key: str) -> Select[tuple[str]]:
    return select(JournalSession.session_id).where(
        JournalSession.binding_id.in_(_private_slot_ids(account_key))
    )


def _on_shared_slot() -> ColumnElement[bool]:
    """The operation recorded a slot, and that slot names no account."""
    slot = MuxOperation.record["slot"]
    # A record without a slot (or with a JSON null) ran against none: not shared.
    recorded = func.coalesce(func.jsonb_typeof(slot), "null") == "object"
    return and_(recorded, slot["account_id"].astext.is_(None))


def _account_operations(account_key: str, principal_keys: Collection[str]) -> ColumnElement[bool]:
    owner = [MuxOperation.account_id == account_key]
    if principal_keys:
        owner.append(MuxOperation.principal_id.in_(sorted(principal_keys)))
    synthetic = MuxOperation.account_id.startswith(context_account(""), autoescape=True)
    return and_(or_(*owner), not_(synthetic))


async def _count(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    return int(await session.scalar(statement) or 0)


async def inventory(
    session: AsyncSession, *, account_id: uuid.UUID, principal_ids: Iterable[uuid.UUID]
) -> MuxInventory:
    """Count what `erase` would remove and what it would leave, without changing anything."""
    key = _account_key(account_id)
    principals = {str(principal) for principal in principal_ids}
    owned = _account_operations(key, principals)
    shared_ops = and_(owned, _on_shared_slot())
    slot = MuxOperation.record["slot"]["thread"]
    shared_conversations = (
        select(
            MuxOperation.tenant_id,
            slot["channel"]["platform"].astext,
            slot["channel"]["channel_id"].astext,
            slot["thread_id"].astext,
        )
        .where(shared_ops)
        .distinct()
        .subquery()
    )
    erased = MuxErased(
        slots=await _count(
            session,
            select(func.count())
            .select_from(ProviderBindingSlot)
            .where(ProviderBindingSlot.account_id == key),
        ),
        binding_generations=await _count(
            session,
            select(func.count())
            .select_from(ProviderBinding)
            .where(ProviderBinding.binding_id.in_(_private_slot_ids(key))),
        ),
        journal_sessions=await _count(
            session,
            select(func.count())
            .select_from(JournalSession)
            .where(JournalSession.binding_id.in_(_private_slot_ids(key))),
        ),
        journal_events=await _count(
            session,
            select(func.count())
            .select_from(Journal)
            .where(Journal.session_id.in_(_private_session_ids(key))),
        ),
        leases=await _count(
            session,
            select(func.count()).select_from(ThreadLease).where(ThreadLease.account_id == key),
        ),
        release_requests=await _count(
            session,
            select(func.count())
            .select_from(ThreadProviderRelease)
            .where(ThreadProviderRelease.account_id == key),
        ),
        operations=await _count(
            session,
            select(func.count()).select_from(MuxOperation).where(owned, not_(_on_shared_slot())),
        ),
    )
    retained = MuxRetained(
        shared_conversations=await _count(
            session, select(func.count()).select_from(shared_conversations)
        ),
        shared_operations=await _count(
            session, select(func.count()).select_from(MuxOperation).where(shared_ops)
        ),
        accounting_records=await _count(
            session,
            select(func.count())
            .select_from(AccountingOutbox)
            .where(AccountingOutbox.binding_id.in_(_private_slot_ids(key))),
        ),
    )
    return MuxInventory(erased=erased, retained=retained)


async def _detach_accounting(session: AsyncSession, account_key: str) -> None:
    """Keep what settling a private slot's usage rows reads, before the slot goes."""
    generations = ProviderBinding.binding_id == ProviderBindingSlot.binding_id
    newest = (
        select(ProviderBinding.profile)
        .where(generations)
        .order_by(ProviderBinding.generation.desc())
        .limit(1)
        .correlate(ProviderBindingSlot)
        .scalar_subquery()
    )
    every = (
        select(func.array_agg(distinct(ProviderBinding.profile)))
        .where(generations)
        .correlate(ProviderBindingSlot)
        .scalar_subquery()
    )
    has_usage = or_(
        exists().where(AccountingOutbox.binding_id == ProviderBindingSlot.binding_id),
        exists().where(UsageObservation.binding_id == ProviderBindingSlot.binding_id),
    )
    await session.execute(
        insert(AccountingBindingContext)
        .from_select(
            ["binding_id", "tenant_id", "platform", "channel_id", "profile", "profiles"],
            select(
                ProviderBindingSlot.binding_id,
                ProviderBindingSlot.tenant_id,
                ProviderBindingSlot.platform,
                ProviderBindingSlot.channel_id,
                newest,
                every,
            ).where(ProviderBindingSlot.account_id == account_key, has_usage, newest.is_not(None)),
        )
        .on_conflict_do_nothing()
    )


async def _delete(session: AsyncSession, statement: Delete) -> int:
    return cast(CursorResult[Any], await session.execute(statement)).rowcount


async def erase(
    session: AsyncSession, *, account_id: uuid.UUID, principal_ids: Iterable[uuid.UUID]
) -> MuxErased:
    """Erase the account's private mux state in the caller's transaction.

    The caller holds the exclusive account lock (`privacy_session_deletes.lock_account`)
    and calls `mark_erased` once the account row itself is gone. Children go before
    parents, so no cascade does work the counts above did not see.
    """
    key = _account_key(account_id)
    principals = {str(principal) for principal in principal_ids}
    # Writers that act for a principal lock it after the account; so does purge.
    for principal in sorted(principals):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:principal, 0))"),
            {"principal": principal},
        )
    await _detach_accounting(session, key)
    events = await _delete(
        session, delete(Journal).where(Journal.session_id.in_(_private_session_ids(key)))
    )
    sessions = await _delete(
        session,
        delete(JournalSession).where(JournalSession.binding_id.in_(_private_slot_ids(key))),
    )
    generations = await _delete(
        session,
        delete(ProviderBinding).where(ProviderBinding.binding_id.in_(_private_slot_ids(key))),
    )
    leases = await _delete(session, delete(ThreadLease).where(ThreadLease.account_id == key))
    releases = await _delete(
        session, delete(ThreadProviderRelease).where(ThreadProviderRelease.account_id == key)
    )
    operations = await _delete(
        session,
        delete(MuxOperation).where(_account_operations(key, principals), not_(_on_shared_slot())),
    )
    slots = await _delete(
        session, delete(ProviderBindingSlot).where(ProviderBindingSlot.account_id == key)
    )
    return MuxErased(
        slots=slots,
        binding_generations=generations,
        journal_sessions=sessions,
        journal_events=events,
        leases=leases,
        release_requests=releases,
        operations=operations,
    )


async def mark_erased(
    session: AsyncSession, *, account_id: uuid.UUID, principal_ids: Iterable[uuid.UUID]
) -> None:
    """Record the account and its principals as erased, so a stale writer is refused."""
    await session.execute(
        insert(ErasedAccount).values(account_id=account_id).on_conflict_do_nothing()
    )
    rows = [{"principal_id": principal, "account_id": account_id} for principal in principal_ids]
    if rows:
        await session.execute(insert(ErasedPrincipal).values(rows).on_conflict_do_nothing())


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _lock_shared(session: AsyncSession, subject: uuid.UUID) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:subject, 0))"),
        {"subject": str(subject)},
    )


async def fence_writers(
    session: AsyncSession,
    *account_ids: str | None,
    principal_ids: Iterable[str | None] = (),
) -> None:
    """Refuse a write for an erased account or principal; else hold off erasure until commit.

    Call it before the write takes any other lock. Pass every account the write acts
    for (the owner of a private slot, the scope's caller) and, for an operation, its
    principal. Ids that are not UUIDs (the operator, `selfhosted-billing:` identities,
    `host:` principals) and None (a shared slot) are skipped. Accounts are locked before
    principals, each in id order, which is the order purge takes them.
    """
    accounts = sorted({found for value in account_ids if (found := _uuid_or_none(value))})
    for account in accounts:
        await _lock_shared(session, account)
        if await session.scalar(
            select(ErasedAccount.account_id).where(ErasedAccount.account_id == account)
        ):
            raise ScopeViolation(str(account), "the account was erased")
    principals = sorted({found for value in principal_ids if (found := _uuid_or_none(value))})
    for principal in principals:
        await _lock_shared(session, principal)
        if await session.scalar(
            select(ErasedPrincipal.principal_id).where(ErasedPrincipal.principal_id == principal)
        ):
            raise ScopeViolation(str(principal), "the account was erased")
