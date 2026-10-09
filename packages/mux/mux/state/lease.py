"""Slot leases: one active root turn per binding slot, fenced.

A slot is a thread plus the caller account that owns it: a per-caller
thread (today's behaviour) has one slot per account, a thread opted into
sharing has one slot with no account. Every acquisition gets a fence one
higher than any before it on that slot. A write that needs a lease is
refused unless it carries the active, unexpired lease *of the record it
writes*, so neither a worker that lost its lease nor a lease from another
thread can commit. A lease taken over from an expired holder is marked
`took_over`: the predecessor may have sent something, so the successor
reconciles before it resends.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field

from mux.contracts._base import Contract
from mux.contracts.ids import ThreadRef
from mux.errors import MuxError, ScopeViolation


class Slot(Contract):
    """Where one provider binding lives. `account_id` is the owning caller
    account of a private binding, or None for a shared thread."""

    thread: ThreadRef
    account_id: str | None = None

    @property
    def tenant_id(self) -> str:
        return self.thread.channel.tenant_id

    def describe(self) -> str:
        owner = self.account_id or "shared"
        return f"{self.thread.channel.channel_id}/{self.thread.thread_id}/{owner}"


class LeaseBusy(MuxError):
    """Another holder has an unexpired lease on the slot."""

    def __init__(self, slot: str, holder: str) -> None:
        super().__init__(f"{slot} is leased by {holder}")
        self.slot = slot
        self.holder = holder


class StaleFence(MuxError):
    """A write carried a lease that is no longer the slot's active one."""

    def __init__(self, slot: str, fence: int) -> None:
        super().__init__(f"fence {fence} on {slot} is stale")
        self.slot = slot
        self.fence = fence


class Lease(Contract):
    slot: Slot
    holder: str
    turn_id: str
    fence: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime
    took_over: bool = False


class LeaseState(Contract):
    """A slot's lease record: the highest fence issued and the active lease."""

    slot: Slot
    last_fence: int = Field(default=0, ge=0)
    active: Lease | None = None


def acquire(
    state: LeaseState | None,
    slot: Slot,
    *,
    holder: str,
    turn_id: str,
    now: datetime,
    ttl: timedelta,
) -> tuple[LeaseState, Lease]:
    """Take the slot's lease. The same holder and turn get their lease back.

    Sharing a lease does not let two callers both send: claiming an
    operation for sending is a separate compare-and-swap.
    """
    state = state or LeaseState(slot=slot)
    active = state.active
    if active is not None and active.expires_at > now:
        if active.holder == holder and active.turn_id == turn_id:
            return state, active
        raise LeaseBusy(slot.describe(), active.holder)
    lease = Lease(
        slot=slot,
        holder=holder,
        turn_id=turn_id,
        fence=state.last_fence + 1,
        acquired_at=now,
        expires_at=now + ttl,
        took_over=active is not None,
    )
    return LeaseState(slot=slot, last_fence=lease.fence, active=lease), lease


def _is_active(active: Lease | None, lease: Lease) -> bool:
    return (
        active is not None
        and active.fence == lease.fence
        and active.holder == lease.holder
        and active.turn_id == lease.turn_id
    )


def check(state: LeaseState | None, lease: Lease, now: datetime) -> None:
    """Raise `StaleFence` unless `lease` is its slot's active, unexpired lease."""
    active = state.active if state else None
    if active is None or not _is_active(active, lease) or active.expires_at <= now:
        raise StaleFence(lease.slot.describe(), lease.fence)


def check_target(
    state: LeaseState | None, target: Slot | None, fence: Lease | None, now: datetime
) -> None:
    """Check the fence a write on a record bound to `target` carries.

    A record bound to a slot needs that slot's active lease; a record bound
    to none takes no fence. A missing or foreign lease is a `ScopeViolation`,
    a superseded one a `StaleFence`.
    """
    if target is None:
        if fence is not None:
            raise ScopeViolation(fence.slot.describe(), "this record is not bound to a slot")
        return
    if fence is None:
        raise ScopeViolation(target.describe(), "a write here needs the slot's lease")
    if fence.slot != target:
        raise ScopeViolation(target.describe(), f"lease belongs to {fence.slot.describe()}")
    check(state, fence, now)


def renew(
    state: LeaseState | None, lease: Lease, *, now: datetime, ttl: timedelta
) -> tuple[LeaseState, Lease]:
    check(state, lease, now)
    renewed = lease.model_copy(update={"expires_at": now + ttl})
    return LeaseState(slot=lease.slot, last_fence=lease.fence, active=renewed), renewed


def release(state: LeaseState | None, lease: Lease) -> LeaseState:
    """End the lease. Releasing an already released lease is a no-op; a
    stale one raises, because a newer holder owns the slot."""
    if state is None or state.last_fence < lease.fence:
        raise StaleFence(lease.slot.describe(), lease.fence)
    if state.active is None and state.last_fence == lease.fence:
        return state
    if not _is_active(state.active, lease):
        raise StaleFence(lease.slot.describe(), lease.fence)
    return state.model_copy(update={"active": None})
