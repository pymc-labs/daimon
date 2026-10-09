"""StateStore behaviour (C02, C03, C05, C07, C13 and leases), on the memory store."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mux.contracts.config import ConfigRevision, resolve_default
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import BindingConflict, OperationConflict
from mux.state.journal import JournalEntry
from mux.state.lease import LeaseBusy, StaleFence
from mux.state.memory import MemoryStateStore
from mux.state.operations import InvalidTransition, recovery, request_digest
from mux.state.store import ConfigRevisionConflict, StateStore, bind_new_thread

NOW = datetime(2026, 10, 9, tzinfo=UTC)
TTL = timedelta(minutes=5)
CHANNEL = ChannelRef(tenant_id="t1", platform="discord", channel_id="c1")
THREAD = ThreadRef(channel=CHANNEL, thread_id="th1")
SCOPE = Scope(tenant_id="t1", account_id="a1", principal_id="u1", authorization_id="z1")
SESSION = ResourceRef(id="s1", kind="session", provider="anthropic", account_scope_id="ws")
PROV = NativeProvenance(provider="anthropic", api_revision="2026-07")


@pytest.fixture
def store() -> StateStore:
    return MemoryStateStore()


def _binding(generation: int = 1, session: str = "sesn_1", id_: str = "b1") -> ProviderBinding:
    return ProviderBinding(
        id=id_,
        thread=THREAD,
        provider="anthropic",
        profile="anthropic.managed_agents",
        native_refs={"session": session, "environment": "env_1"},
        generation=generation,
        config_revision=1,
    )


def _entry(
    source: str,
    type_: str,
    payload: dict[str, Any] | None = None,
    *,
    authority: str = "record",
    turn_id: str | None = "turn_1",
    revision: int = 1,
) -> JournalEntry:
    event = Event.model_validate(
        {
            "id": f"ev_{source}_{revision}",
            "session_id": SESSION.id,
            "sequence": 0,
            "type": type_,
            "turn_id": turn_id,
            "observed_at": NOW,
            "authority": authority,
            "payload": payload or {},
            "native": PROV,
        }
    )
    return JournalEntry(source_key=source, revision=revision, event=event)


def _usage(revision: int, output: int | None) -> UsageObservation:
    return UsageObservation(
        id="obs_1",
        revision=revision,
        session=SESSION,
        grain="model_request",
        basis="cumulative",
        output_tokens=output,
        completeness="measured" if output is not None else "unknown",
        observed_at=NOW,
    )


async def test_config_revisions_are_immutable(store: StateStore) -> None:
    rev = ConfigRevision.create(CHANNEL, 1, resolve_default(None))
    assert await store.put_config_revision(rev) == rev
    assert await store.put_config_revision(rev) == rev
    other = ConfigRevision.create(
        CHANNEL, 1, resolve_default(None).model_copy(update={"thread_mode": "shared"})
    )
    with pytest.raises(ConfigRevisionConflict):
        await store.put_config_revision(other)
    assert await store.latest_config_revision(CHANNEL) == rev


async def test_c13_parallel_new_binding_race_yields_one_binding(store: StateStore) -> None:
    candidates = [_binding(session=f"sesn_{i}", id_=f"b{i}") for i in range(8)]
    winners = await asyncio.gather(*(bind_new_thread(store, c) for c in candidates))
    assert len({w.id for w in winners}) == 1
    assert await store.get_binding(THREAD) == winners[0]


async def test_binding_cas_on_generation(store: StateStore) -> None:
    await store.put_binding(_binding(), expected_generation=0)
    with pytest.raises(BindingConflict) as raised:
        await store.put_binding(_binding(), expected_generation=0)
    assert (raised.value.expected_generation, raised.value.actual_generation) == (0, 1)
    with pytest.raises(ValueError, match="keeps binding id"):
        await store.put_binding(_binding(2, id_="other"), expected_generation=1)
    rebound = await store.put_binding(_binding(2, session="sesn_2"), expected_generation=1)
    assert await store.get_binding(THREAD) == rebound


async def test_c02_binding_survives_restart_and_is_never_silently_replaced() -> None:
    first = MemoryStateStore()
    bound = await bind_new_thread(first, _binding())
    second = first.restart()
    assert await second.get_binding(THREAD) == bound
    assert dict(bound.native_refs) == {"session": "sesn_1", "environment": "env_1"}
    # A fresh session for the thread needs an explicit rebind, not a new first binding.
    assert await bind_new_thread(second, _binding(session="sesn_fresh", id_="b9")) == bound


async def test_c03_operation_key_reuse(store: StateStore) -> None:
    digest = request_digest({"text": "hi", "mode": "new_turn"})
    begun = await store.begin_operation(
        SCOPE, key="k1", request_digest=digest, operation_id="op1", now=NOW
    )
    assert begun.fresh and begun.record.operation.status == "pending"
    await store.advance_operation(SCOPE, "k1", "sent", now=NOW)
    await store.advance_operation(SCOPE, "k1", "accepted", now=NOW, result={"input_ids": ["in_1"]})
    # A retry with a fresh authorization finds the same operation and its result.
    retry_scope = SCOPE.model_copy(update={"authorization_id": "z2"})
    again = await store.begin_operation(
        retry_scope,
        key="k1",
        request_digest=request_digest({"mode": "new_turn", "text": "hi"}),
        operation_id="op2",
        now=NOW,
    )
    assert not again.fresh
    assert again.record.operation.id == "op1"
    assert again.record.result["input_ids"] == ["in_1"]
    with pytest.raises(OperationConflict):
        await store.begin_operation(
            SCOPE,
            key="k1",
            request_digest=request_digest({"text": "bye"}),
            operation_id="op3",
            now=NOW,
        )


async def test_c03_timeout_after_acceptance_is_unknown_not_resent(store: StateStore) -> None:
    await store.begin_operation(SCOPE, key="k", request_digest="d", operation_id="op", now=NOW)
    await store.advance_operation(SCOPE, "k", "sent", now=NOW)
    record = await store.advance_operation(SCOPE, "k", "outcome_unknown", now=NOW)
    assert recovery(record.operation) == "reconcile"
    with pytest.raises(InvalidTransition):
        await store.advance_operation(SCOPE, "k", "sent", now=NOW)


async def test_operation_keys_are_per_tenant_and_account(store: StateStore) -> None:
    other = Scope(tenant_id="t2", account_id="a1", principal_id="u1", authorization_id="z")
    await store.begin_operation(SCOPE, key="k", request_digest="d1", operation_id="o1", now=NOW)
    begun = await store.begin_operation(
        other, key="k", request_digest="d2", operation_id="o2", now=NOW
    )
    assert begun.fresh


async def test_lease_one_root_turn_and_monotonic_fence(store: StateStore) -> None:
    first = await store.acquire_lease(THREAD, holder="w1", turn_id="t1", now=NOW, ttl=TTL)
    assert first.fence == 1 and not first.took_over
    same = await store.acquire_lease(THREAD, holder="w1", turn_id="t1", now=NOW, ttl=TTL)
    assert same == first
    with pytest.raises(LeaseBusy):
        await store.acquire_lease(THREAD, holder="w2", turn_id="t2", now=NOW, ttl=TTL)
    await store.release_lease(first)
    await store.release_lease(first)
    second = await store.acquire_lease(THREAD, holder="w2", turn_id="t2", now=NOW, ttl=TTL)
    assert second.fence == 2
    with pytest.raises(StaleFence):
        await store.release_lease(first)


async def test_stale_fence_cannot_commit(store: StateStore) -> None:
    old = await store.acquire_lease(THREAD, holder="w1", turn_id="t1", now=NOW, ttl=TTL)
    later = NOW + TTL + timedelta(seconds=1)
    new = await store.acquire_lease(THREAD, holder="w2", turn_id="t1", now=later, ttl=TTL)
    assert new.fence == 2 and new.took_over
    await store.begin_operation(SCOPE, key="k", request_digest="d", operation_id="o", now=NOW)
    with pytest.raises(StaleFence):
        await store.advance_operation(SCOPE, "k", "sent", now=later, fence=old)
    with pytest.raises(StaleFence):
        await store.append_events(
            SESSION,
            [_entry("e1", "agent.message", {"item_id": "i1", "content": []})],
            cursor="c1",
            now=later,
            fence=old,
        )
    with pytest.raises(StaleFence):
        await store.renew_lease(old, now=later, ttl=TTL)
    assert await store.read_events(SESSION.id) == []
    await store.append_events(SESSION, [], cursor="c1", now=later, fence=new)


async def test_c05_overlapping_pages_and_child_completion(store: StateStore) -> None:
    running = _entry("e1", "session.status_running", {"root_turn_id": "root"}, turn_id="root")
    message = _entry("e2", "agent.message", {"item_id": "i1", "content": []}, turn_id="root")
    child_end = _entry(
        "e3",
        "session.turn_ended",
        {"root_turn_id": "child", "outcome": "completed"},
        turn_id="child",
    )
    await store.append_events(SESSION, [running, message], cursor="c2", now=NOW)
    # The saved-items page overlaps what the stream already delivered.
    result = await store.append_events(
        SESSION, [message, child_end, child_end], cursor="c3", now=NOW
    )
    assert result.duplicates == 2
    assert [e.sequence for e in await store.read_events(SESSION.id)] == [0, 1, 2]
    assert result.projection.state == "running"
    assert result.projection.active_root_turn == "root"
    assert result.projection.cursor == "c3"
    gap = _entry("e4", "session.history_gap", {"domain": "stream", "recoverable": False})
    end = _entry(
        "e5", "session.turn_ended", {"root_turn_id": "root", "outcome": "completed"}, turn_id="root"
    )
    final = await store.append_events(SESSION, [gap, end], cursor="c5", now=NOW)
    assert final.projection.state == "idle"
    assert final.projection.gaps == ("ev_e4_1",)


async def test_previews_never_complete_a_turn(store: StateStore) -> None:
    await store.append_events(
        SESSION,
        [_entry("e1", "session.status_running", {"root_turn_id": "root"}, turn_id="root")],
        cursor="c1",
        now=NOW,
    )
    preview = _entry(
        "p1",
        "session.turn_ended",
        {"root_turn_id": "root", "outcome": "completed"},
        authority="preview",
        turn_id="root",
    )
    result = await store.append_events(SESSION, [preview], cursor="c2", now=NOW)
    assert len(result.appended) == 1
    assert result.projection.state == "running"


async def test_journal_revision_of_same_source_is_a_new_entry(store: StateStore) -> None:
    end = {"root_turn_id": "turn_1", "outcome": "errored"}
    corrected = {"root_turn_id": "turn_1", "outcome": "completed"}
    await store.append_events(
        SESSION, [_entry("e1", "session.turn_ended", end)], cursor="a", now=NOW
    )
    result = await store.append_events(
        SESSION,
        [_entry("e1", "session.turn_ended", corrected, authority="reconciled", revision=2)],
        cursor="b",
        now=NOW,
    )
    assert len(result.appended) == 1 and result.appended[0].sequence == 1


async def test_c07_usage_revisions_apply_signed_deltas_once(store: StateStore) -> None:
    rows = [
        await store.record_usage("b1", _usage(r, out))
        for r, out in [(1, None), (2, 100), (3, 120), (4, 110)]
    ]
    deltas = [row.deltas["output_tokens"] for row in rows if row is not None]
    assert deltas == [None, 100, 20, -10]
    # Every revision replayed, and the stale 100 again: nothing new.
    for r, out in [(1, None), (2, 100), (3, 120), (4, 110), (2, 100)]:
        assert await store.record_usage("b1", _usage(r, out)) is None
    pending = await store.pending_outbox()
    assert [row.prior_applied_revision for row in pending] == [None, 1, 2, 3]
    total = 0
    for row in pending:
        assert await store.mark_outbox_applied(row)
        assert not await store.mark_outbox_applied(row)
        total += row.deltas["output_tokens"] or 0
    assert total == 110
    assert await store.pending_outbox() == []


async def test_usage_null_after_known_keeps_the_accounted_value(store: StateStore) -> None:
    await store.record_usage("b1", _usage(1, 100))
    row = await store.record_usage("b1", _usage(2, None))
    assert row is not None and row.is_noop
    later = await store.record_usage("b1", _usage(3, 130))
    assert later is not None and later.deltas["output_tokens"] == 30
