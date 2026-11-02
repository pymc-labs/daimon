"""Move one caller's thread to the provider a confirmed switch selected.

A thread keeps its provider through its slot binding, so a switch normally
applies to new threads only. A confirmed switch from a thread, of the agent
answering it, records a release request for the requester's own slot of that
thread. A hand-off never does: it is not confirmed, and moving a thread drops
its history. The next turn there selects as if released and, once admitted,
applies it:

- only while the slot is still at the generation the confirmation saw, so a
  binding that moved since (another turn, another change) is never released;
- through the binding compare-and-swap, as a sessionless successor that reads
  as unbound (`mux.state.store.released_successor`), never an in-place edit;
- and it fresh-starts the caller's live Managed Agents session there, so no
  earlier native session is reused by the next provider.

The turn that confirms the switch keeps its own binding to its end: the
release waits for the next turn. Shared slots are never released here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from daimon.core.stores import thread_provider_releases
from daimon.core.stores.thread_session_lineage import request_fresh_start
from daimon.core.stores.thread_sessions import get_live_thread_session
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import BindingConflict
from mux.profiles import MANAGED_AGENTS
from mux.state.lease import Slot
from mux.state.store import RELEASED_REF, StateStore, is_released, released_successor
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


def caller_slot(
    *, tenant_id: uuid.UUID, platform: str, channel_id: str, thread_id: str, account_id: uuid.UUID
) -> Slot:
    channel = ChannelRef(tenant_id=str(tenant_id), platform=platform, channel_id=channel_id)
    return Slot(thread=ThreadRef(channel=channel, thread_id=thread_id), account_id=str(account_id))


async def request_thread_release(
    session: AsyncSession, slot: Slot, *, generation: int, now: datetime
) -> None:
    """Record that `slot`'s next turn selects its provider afresh.

    `generation` is the slot's binding generation the confirmation saw (0 for
    unbound), read in the caller's own transaction; a later binding change makes
    the request stale. Idempotent: a newer request for the slot replaces it.
    """
    if slot.account_id is None:
        raise ValueError("only a caller's own slot of a thread is released")
    await thread_provider_releases.put_request(session, slot, generation=generation, now=now)


async def apply_thread_release(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: StateStore,
    slot: Slot,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Apply `slot`'s pending release, if any and still current. True when released.

    Against the Postgres store the request, the binding and the fresh start are one
    transaction that takes the account's erasure fence first, so an account erasure
    waits for it whole (or finds it done) and never meets the request row half-held.
    Another store keeps the request and the binding write in separate transactions.
    """
    from daimon.core.stores import mux_erasure, mux_state

    if slot.account_id is None:
        return False
    in_one_transaction = isinstance(store, mux_state.PostgresStateStore) and store.owns_sessions(
        sessionmaker
    )
    async with sessionmaker.begin() as session:
        if in_one_transaction:
            await mux_erasure.fence_writers(session, slot.account_id)
        requested = await thread_provider_releases.take_request(session, slot)
        if requested is None:
            return False
        if in_one_transaction:
            current = await mux_state.get_binding(session, slot, include_released=True)
        else:
            current = await store.get_binding(slot, include_released=True)
        generation = current.generation if current is not None else 0
        # A binding written by a separate transaction may have committed before this one
        # failed: the request is still here with the slot one generation on. Finish it,
        # don't redo it.
        finishing = is_released(current) and generation == requested + 1
        if generation != requested and not finishing:
            log.info("thread_release_stale", requested=requested, current=generation)
            return False
        if (
            not finishing
            and not is_released(current)
            and not await _write_release(
                store, slot, current, session if in_one_transaction else None
            )
        ):
            return False
        await _fresh_start(session, slot, tenant_id=tenant_id, account_id=account_id, now=now)
    return True


async def release_now(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: StateStore,
    slot: Slot,
    *,
    generation: int,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    now: datetime,
) -> bool:
    """Release `slot` now if it is still at `generation`: a conversation following its
    agent's switch (M03), once the message that moves it is admitted.

    No request row: nothing is left behind if that message is refused, and a card's
    pending request is never touched. A slot that moved meanwhile (another message,
    a card, a later switch) is left alone. True when released.
    """
    if slot.account_id is None:
        return False
    current = await store.get_binding(slot, include_released=True)
    if is_released(current) or (current.generation if current is not None else 0) != generation:
        log.info("thread_follow_stale", requested=generation)
        return False
    if not await _write_release(store, slot, current):
        return False
    async with sessionmaker.begin() as session:
        await _fresh_start(session, slot, tenant_id=tenant_id, account_id=account_id, now=now)
    return True


async def _write_release(
    store: StateStore,
    slot: Slot,
    current: ProviderBinding | None,
    session: AsyncSession | None = None,
) -> bool:
    """Mark `slot` released at its next generation; False when another write won.

    With a `session`, the write joins that transaction (Postgres store only).
    """
    if current is None:
        # Pinned only by an older Managed Agents session: a released first generation.
        released = ProviderBinding(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"daimon.turn.binding:{slot.model_dump_json()}")),
            thread=slot.thread,
            provider=MANAGED_AGENTS.provider,
            profile=MANAGED_AGENTS.profile_id,
            native_refs={RELEASED_REF: "1"},
            generation=1,
            config_revision=0,
            legacy_account_id=slot.account_id,
        )
        expected = 0
    else:
        released, expected = released_successor(current), current.generation
    try:
        if session is not None:
            from daimon.core.stores import mux_state

            await mux_state.put_binding(session, released, expected_generation=expected)
        else:
            await store.put_binding(released, expected_generation=expected)
    except BindingConflict:
        log.info("thread_release_lost_race", generation=expected)
        return False
    return True


async def _fresh_start(
    session: AsyncSession,
    slot: Slot,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    now: datetime,
) -> None:
    """The slot's live Managed Agents session (if any) starts afresh on its next turn."""
    live = await get_live_thread_session(
        session,
        tenant_id=tenant_id,
        platform=slot.thread.channel.platform,
        thread_id=slot.thread.thread_id,
        account_id=account_id,
    )
    if live is not None:
        await request_fresh_start(session, id=live.id, at=now)


async def release_due(
    sessionmaker: async_sessionmaker[AsyncSession], store: StateStore, slot: Slot
) -> bool:
    """Whether `slot` is released or has a current pending release (read only).

    Admission selects as if released, and applies the release only once the
    turn is admitted, so a refused turn leaves the request for the next one.
    """
    current = await store.get_binding(slot, include_released=True)
    if is_released(current):
        return True
    async with sessionmaker() as session:
        requested = await thread_provider_releases.requested_generation(session, slot)
    return requested is not None and requested == (current.generation if current is not None else 0)


async def release_pending(session: AsyncSession, slot: Slot) -> bool:
    """Whether `slot` is released, or its next turn will release it (read only)."""
    from daimon.core.stores import mux_state

    current = await mux_state.get_binding(session, slot, include_released=True)
    if is_released(current):
        return True
    if slot.account_id is None:
        return False
    requested = await thread_provider_releases.requested_generation(session, slot)
    return requested is not None and requested == (current.generation if current is not None else 0)


async def slot_is_released(store: StateStore, slot: Slot) -> bool:
    """Whether `slot` is released: its turn ignores the thread's older sessions."""
    return is_released(await store.get_binding(slot, include_released=True))


async def pending_releases(
    session: AsyncSession, *, tenant_id: uuid.UUID
) -> list[tuple[str, str, str, int]]:
    """The tenant's pending requests as (channel, thread, account, generation)."""
    return await thread_provider_releases.list_requests(session, tenant_id=tenant_id)
