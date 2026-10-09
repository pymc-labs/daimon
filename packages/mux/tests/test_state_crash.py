"""Crash injection on the restartable memory store (C04)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.state.journal import JournalEntry
from mux.state.lease import Lease, Slot, StaleFence
from mux.state.memory import MemoryStateStore, SimulatedCrash
from mux.state.operations import recovery

NOW = datetime(2026, 10, 9, tzinfo=UTC)
TTL = timedelta(minutes=5)
THREAD = ThreadRef(
    channel=ChannelRef(tenant_id="t", platform="slack", channel_id="c"), thread_id="th"
)
SLOT = Slot(thread=THREAD, account_id="a")
SCOPE = Scope(tenant_id="t", account_id="a", principal_id="u", authorization_id="z")
SESSION = ResourceRef(id="s1", kind="session", provider="anthropic", account_scope_id="ws")


def _entry(source: str) -> JournalEntry:
    event = Event(
        id=f"ev_{source}",
        session_id=SESSION.id,
        sequence=0,
        type="agent.message",
        observed_at=NOW,
        authority="record",
        payload={"item_id": source, "content": []},
        native=NativeProvenance(provider="anthropic", api_revision="x", event_id=source),
    )
    return JournalEntry(source_key=source, event=event)


async def _start(store: MemoryStateStore) -> Lease:
    binding = ProviderBinding(
        id="b1",
        thread=THREAD,
        provider="anthropic",
        profile="anthropic.managed_agents",
        native_refs={"session": SESSION.id},
        generation=1,
        config_revision=1,
        legacy_account_id="a",
    )
    await store.put_binding(binding, expected_generation=0)
    fence = await store.acquire_lease(SLOT, holder="w1", turn_id="t", now=NOW, ttl=TTL)
    await store.begin_operation(
        SCOPE, key="k", request_digest="d", operation_id="o", now=NOW, slot=SLOT
    )
    return fence


async def test_crash_before_the_send_claim_commits_is_safe_to_send() -> None:
    store = MemoryStateStore(crash={"claim_send": "before_commit"})
    fence = await _start(store)
    with pytest.raises(SimulatedCrash):
        await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    successor = store.restart()
    record = await successor.get_operation(SCOPE, "k")
    assert record is not None
    assert recovery(record.operation) == "send"
    await successor.claim_send(SCOPE, "k", now=NOW, fence=fence)


async def test_crash_after_the_send_claim_must_reconcile() -> None:
    store = MemoryStateStore(crash={"claim_send": "after_commit"})
    fence = await _start(store)
    with pytest.raises(SimulatedCrash):
        await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    record = await store.restart().get_operation(SCOPE, "k")
    assert record is not None
    assert recovery(record.operation) == "reconcile"


async def test_crash_after_accept_observes_instead_of_resending() -> None:
    store = MemoryStateStore()
    fence = await _start(store)
    await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    crashing = store.restart(crash={"advance_operation": "after_commit"})
    with pytest.raises(SimulatedCrash):
        await crashing.advance_operation(SCOPE, "k", "accepted", now=NOW, fence=fence)
    successor = crashing.restart()
    record = await successor.get_operation(SCOPE, "k")
    assert record is not None and recovery(record.operation) == "observe"
    again = await successor.begin_operation(
        SCOPE, key="k", request_digest="d", operation_id="o2", now=NOW, slot=SLOT
    )
    assert not again.fresh


async def test_crash_mid_append_commits_nothing() -> None:
    store = MemoryStateStore(crash={"append_events": "before_commit"})
    fence = await _start(store)
    with pytest.raises(SimulatedCrash):
        await store.append_events(
            SESSION, [_entry("e1"), _entry("e2")], fence=fence, cursor="c2", now=NOW
        )
    restarted = store.restart()
    assert await restarted.read_events(SESSION.id) == []
    assert await restarted.projection(SESSION.id) is None


async def test_crash_after_journal_commit_replays_without_duplicates() -> None:
    store = MemoryStateStore(crash={"append_events": "after_commit"})
    fence = await _start(store)
    with pytest.raises(SimulatedCrash):
        await store.append_events(
            SESSION, [_entry("e1"), _entry("e2")], fence=fence, cursor="c2", now=NOW
        )
    restarted = store.restart()
    projection = await restarted.projection(SESSION.id)
    assert projection is not None and projection.cursor == "c2"
    # The successor re-reads from an older cursor; the overlap is dropped.
    result = await restarted.append_events(
        SESSION, [_entry("e1"), _entry("e2"), _entry("e3")], fence=fence, cursor="c3", now=NOW
    )
    assert result.duplicates == 2
    assert [e.id for e in await restarted.read_events(SESSION.id)] == ["ev_e1", "ev_e2", "ev_e3"]


async def test_stale_worker_after_restart_cannot_commit() -> None:
    store = MemoryStateStore()
    old = await _start(store)
    successor = store.restart()
    later = NOW + TTL
    new = await successor.acquire_lease(SLOT, holder="w2", turn_id="t", now=later, ttl=TTL)
    assert new.took_over and new.fence > old.fence
    with pytest.raises(StaleFence):
        await store.claim_send(SCOPE, "k", now=later, fence=old)
    with pytest.raises(StaleFence):
        await store.append_events(SESSION, [_entry("e1")], fence=old, cursor="c1", now=later)
    # The successor took over: it reconciles nothing here (still pending), then claims.
    record = await successor.get_operation(SCOPE, "k")
    assert record is not None and recovery(record.operation) == "send"
    await successor.claim_send(SCOPE, "k", now=later, fence=new)
    await successor.append_events(SESSION, [_entry("e1")], fence=new, cursor="c1", now=later)
