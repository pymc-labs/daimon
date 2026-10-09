"""Thread leases: one active root turn per thread, fenced.

Every acquisition gets a fence one higher than any before it on that thread.
A write that carries a lease is refused unless that lease is still the
active, unexpired one, so a worker that lost its lease cannot commit. A
lease taken over from an expired holder is marked `took_over`: the
predecessor may have sent something, so the successor reconciles before it
resends.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field

from mux.contracts._base import Contract
from mux.contracts.ids import ThreadRef
from mux.errors import MuxError


class LeaseBusy(MuxError):
    """Another holder has an unexpired lease on the thread."""

    def __init__(self, thread_id: str, holder: str) -> None:
        super().__init__(f"thread {thread_id} is leased by {holder}")
        self.thread_id = thread_id
        self.holder = holder


class StaleFence(MuxError):
    """A write carried a lease that is no longer the thread's active one."""

    def __init__(self, thread_id: str, fence: int) -> None:
        super().__init__(f"fence {fence} on thread {thread_id} is stale")
        self.thread_id = thread_id
        self.fence = fence


class Lease(Contract):
    thread: ThreadRef
    holder: str
    turn_id: str
    fence: int = Field(ge=1)
    acquired_at: datetime
    expires_at: datetime
    took_over: bool = False


class LeaseState(Contract):
    """A thread's lease record: the highest fence issued and the active lease."""

    thread: ThreadRef
    last_fence: int = Field(default=0, ge=0)
    active: Lease | None = None


def acquire(
    state: LeaseState | None,
    thread: ThreadRef,
    *,
    holder: str,
    turn_id: str,
    now: datetime,
    ttl: timedelta,
) -> tuple[LeaseState, Lease]:
    """Take the thread's lease. The same holder and turn get their lease back."""
    state = state or LeaseState(thread=thread)
    active = state.active
    if active is not None and active.expires_at > now:
        if active.holder == holder and active.turn_id == turn_id:
            return state, active
        raise LeaseBusy(thread.thread_id, active.holder)
    lease = Lease(
        thread=thread,
        holder=holder,
        turn_id=turn_id,
        fence=state.last_fence + 1,
        acquired_at=now,
        expires_at=now + ttl,
        took_over=active is not None,
    )
    return LeaseState(thread=thread, last_fence=lease.fence, active=lease), lease


def check(state: LeaseState | None, lease: Lease, now: datetime) -> None:
    """Raise `StaleFence` unless `lease` is the thread's active, unexpired lease."""
    active = state.active if state else None
    if active is None or active.fence != lease.fence or active.expires_at <= now:
        raise StaleFence(lease.thread.thread_id, lease.fence)


def renew(
    state: LeaseState | None, lease: Lease, *, now: datetime, ttl: timedelta
) -> tuple[LeaseState, Lease]:
    check(state, lease, now)
    renewed = lease.model_copy(update={"expires_at": now + ttl})
    return LeaseState(thread=lease.thread, last_fence=lease.fence, active=renewed), renewed


def release(state: LeaseState | None, lease: Lease) -> LeaseState:
    """End the lease. Releasing an already released lease is a no-op; a
    stale one raises, because a newer holder owns the thread."""
    if state is None or state.last_fence < lease.fence:
        raise StaleFence(lease.thread.thread_id, lease.fence)
    if state.active is None and state.last_fence == lease.fence:
        return state
    if state.active is None or state.active.fence != lease.fence:
        raise StaleFence(lease.thread.thread_id, lease.fence)
    return state.model_copy(update={"active": None})
