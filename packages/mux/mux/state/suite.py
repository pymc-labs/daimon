"""The `StateStore` protocol suite: one set of checks every store must pass.

Each check takes a `StoreMaker`, which returns a store over the same durable
state every time it is called (a second call is a process restart). The
memory store runs it in `packages/mux/tests`, and Daimon's Postgres store
runs the same checks. Tenant ids are UUIDs so a database store can hold
them as foreign keys; a database harness creates the three `TENANTS` first.
Crash injection is store-specific and lives with each store's tests.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from mux.contracts.config import ConfigRevision, resolve_default
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ChannelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import BindingConflict, OperationConflict, ScopeViolation
from mux.state.journal import JournalEntry
from mux.state.lease import Lease, LeaseBusy, Slot, StaleFence
from mux.state.operations import InvalidTransition, SendClaimed, recovery, request_digest
from mux.state.store import ConfigRevisionConflict, StateStore, bind_new_slot
from mux.state.usage_ledger import UsageRevisionConflict

type StoreMaker = Callable[[], StateStore]

TENANT_A = "00000000-0000-4000-8000-00000000000a"
TENANT_B = "00000000-0000-4000-8000-00000000000b"
TENANT_C = "00000000-0000-4000-8000-00000000000c"
TENANTS = (TENANT_A, TENANT_B, TENANT_C)


class Raised[E: BaseException]:
    value: E


@contextmanager
def raises[E: BaseException](kind: type[E], match: str | None = None) -> Iterator[Raised[E]]:
    """`pytest.raises` without pytest, so the suite ships with the library."""
    caught = Raised[E]()
    try:
        yield caught
    except kind as exc:
        if match is not None and not re.search(match, str(exc)):
            raise AssertionError(f"{exc!r} does not match {match!r}") from exc
        caught.value = exc
        return
    raise AssertionError(f"did not raise {kind.__name__}")


NOW = datetime(2026, 10, 9, tzinfo=UTC)
TTL = timedelta(minutes=5)
CHANNEL = ChannelRef(tenant_id=TENANT_A, platform="discord", channel_id="c1")
THREAD = ThreadRef(channel=CHANNEL, thread_id="th1")
SHARED = Slot(thread=THREAD)
PRIVATE_A = Slot(thread=THREAD, account_id="a1")
SCOPE = Scope(tenant_id=TENANT_A, account_id="a1", principal_id="u1", authorization_id="z1")
SESSION = ResourceRef(id="s1", kind="session", provider="anthropic", account_scope_id="ws")
PROV = NativeProvenance(provider="anthropic", api_revision="2026-07")


async def _lease(store: StateStore, slot: Slot = PRIVATE_A, holder: str = "w1") -> Lease:
    return await store.acquire_lease(slot, holder=holder, turn_id="turn_1", now=NOW, ttl=TTL)


def _binding(
    generation: int = 1,
    session: str = "sesn_1",
    id_: str = "b1",
    account: str | None = None,
    thread: ThreadRef = THREAD,
) -> ProviderBinding:
    return ProviderBinding(
        id=id_,
        thread=thread,
        provider="anthropic",
        profile="anthropic.managed_agents",
        native_refs={"session": session, "environment": "env_1"},
        generation=generation,
        config_revision=1,
        legacy_account_id=account,
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
            "id": f"ev_{source}_{revision}_{authority}",
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


def _message(source: str, text: str = "hi") -> JournalEntry:
    content = [{"type": "text", "text": text}]
    return _entry(source, "agent.message", {"item_id": source, "content": content})


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


async def _own_session(store: StateStore) -> ProviderBinding:
    """Bind caller a1's private slot to session s1, so s1's journal is a1's."""
    owner = _binding(session=SESSION.id, id_="b_s1", account="a1")
    return await store.put_binding(owner, expected_generation=0)


async def _begin(store: StateStore, scope: Scope = SCOPE, slot: Slot | None = PRIVATE_A) -> None:
    await store.begin_operation(
        scope, key="k", request_digest="d", operation_id="op", now=NOW, slot=slot
    )


async def check_config_revisions_are_immutable(make: StoreMaker) -> None:
    store = make()
    rev = ConfigRevision.create(CHANNEL, 1, resolve_default(None))
    assert await store.put_config_revision(rev) == rev
    assert await store.put_config_revision(rev) == rev
    other = ConfigRevision.create(
        CHANNEL, 1, resolve_default(None).model_copy(update={"thread_mode": "shared"})
    )
    with raises(ConfigRevisionConflict):
        await store.put_config_revision(other)
    assert await store.latest_config_revision(CHANNEL) == rev


async def check_c13_parallel_new_binding_race_yields_one_binding(make: StoreMaker) -> None:
    store = make()
    candidates = [_binding(session=f"sesn_{i}", id_=f"b{i}") for i in range(8)]
    winners = await asyncio.gather(*(bind_new_slot(store, c) for c in candidates))
    assert len({w.id for w in winners}) == 1
    assert await store.get_binding(SHARED) == winners[0]


async def check_private_bindings_of_one_thread_stay_apart(make: StoreMaker) -> None:
    store = make()
    a = await bind_new_slot(store, _binding(session="sesn_a", id_="ba", account="a1"))
    b = await bind_new_slot(store, _binding(session="sesn_b", id_="bb", account="a2"))
    assert a.native_refs["session"] == "sesn_a"
    assert b.native_refs["session"] == "sesn_b"
    assert await store.get_binding(PRIVATE_A) == a
    assert await store.get_binding(Slot(thread=THREAD, account_id="a2")) == b
    assert await store.get_binding(SHARED) is None


async def check_binding_cas_on_generation(make: StoreMaker) -> None:
    store = make()
    await store.put_binding(_binding(), expected_generation=0)
    with raises(BindingConflict) as raised:
        await store.put_binding(_binding(), expected_generation=0)
    assert (raised.value.expected_generation, raised.value.actual_generation) == (0, 1)
    with raises(ValueError, match="keeps binding id"):
        await store.put_binding(_binding(2, id_="other"), expected_generation=1)
    rebound = await store.put_binding(_binding(2, session="sesn_2"), expected_generation=1)
    assert await store.get_binding(SHARED) == rebound


async def check_a_binding_id_names_one_slot(make: StoreMaker) -> None:
    store = make()
    await store.put_binding(_binding(), expected_generation=0)
    elsewhere = ThreadRef(channel=CHANNEL, thread_id="th2")
    with raises(ValueError, match="another slot"):
        await store.put_binding(_binding(thread=elsewhere), expected_generation=0)


async def check_c02_binding_survives_restart_and_is_never_silently_replaced(
    make: StoreMaker,
) -> None:
    first = make()
    bound = await bind_new_slot(first, _binding())
    second = make()
    assert await second.get_binding(SHARED) == bound
    assert dict(bound.native_refs) == {"session": "sesn_1", "environment": "env_1"}
    # A fresh session for the thread needs an explicit rebind, not a new first binding.
    assert await bind_new_slot(second, _binding(session="sesn_fresh", id_="b9")) == bound


async def check_c03_operation_key_reuse(make: StoreMaker) -> None:
    store = make()
    fence = await _lease(store)
    digest = request_digest({"text": "hi", "mode": "new_turn"})
    begun = await store.begin_operation(
        SCOPE, key="k1", request_digest=digest, operation_id="op1", now=NOW, slot=PRIVATE_A
    )
    assert begun.fresh and begun.record.operation.status == "pending"
    await store.claim_send(SCOPE, "k1", now=NOW, fence=fence)
    await store.advance_operation(
        SCOPE, "k1", "accepted", now=NOW, fence=fence, result={"input_ids": ["in_1"]}
    )
    # A retry with a fresh authorization finds the same operation and its result.
    retry_scope = SCOPE.model_copy(update={"authorization_id": "z2"})
    again = await store.begin_operation(
        retry_scope,
        key="k1",
        request_digest=request_digest({"mode": "new_turn", "text": "hi"}),
        operation_id="op2",
        now=NOW,
        slot=PRIVATE_A,
    )
    assert not again.fresh
    assert again.record.operation.id == "op1"
    assert again.record.result["input_ids"] == ["in_1"]
    with raises(OperationConflict):
        await store.begin_operation(
            SCOPE,
            key="k1",
            request_digest=request_digest({"text": "bye"}),
            operation_id="op3",
            now=NOW,
            slot=PRIVATE_A,
        )


async def check_c03_timeout_after_acceptance_is_unknown_not_resent(make: StoreMaker) -> None:
    store = make()
    fence = await _lease(store)
    await _begin(store)
    await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    record = await store.advance_operation(SCOPE, "k", "outcome_unknown", now=NOW, fence=fence)
    assert recovery(record.operation) == "reconcile"
    with raises(SendClaimed):
        await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    with raises(InvalidTransition):
        await store.advance_operation(SCOPE, "k", "sent", now=NOW, fence=fence)


async def check_exactly_one_concurrent_sender_wins_the_claim(make: StoreMaker) -> None:
    store = make()
    fence = await _lease(store)
    await _begin(store)
    # Same holder and turn: every caller shares the fence, so only the claim decides.
    fences = [await _lease(store) for _ in range(8)]
    assert all(f == fence for f in fences)
    outcomes = await asyncio.gather(
        *(store.claim_send(SCOPE, "k", now=NOW, fence=f) for f in fences),
        return_exceptions=True,
    )
    assert sum(not isinstance(o, BaseException) for o in outcomes) == 1
    assert sum(isinstance(o, SendClaimed) for o in outcomes) == 7


async def check_reconciled_absence_allows_one_resend_under_the_same_key(make: StoreMaker) -> None:
    store = make()
    fence = await _lease(store)
    await _begin(store)
    await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    reopened = await store.advance_operation(SCOPE, "k", "pending", now=NOW, fence=fence)
    assert recovery(reopened.operation) == "send"
    resent = await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    assert resent.operation.status == "sent" and resent.operation.id == "op"
    await store.advance_operation(SCOPE, "k", "processed", now=NOW, fence=fence)
    with raises(InvalidTransition):
        await store.advance_operation(SCOPE, "k", "pending", now=NOW, fence=fence)


async def check_another_principal_cannot_use_an_operation_key(make: StoreMaker) -> None:
    store = make()
    fence = await _lease(store)
    await _begin(store)
    other = SCOPE.model_copy(update={"principal_id": "u2"})
    with raises(ScopeViolation):
        await _begin(store, other)
    with raises(ScopeViolation):
        await store.get_operation(other, "k")
    with raises(ScopeViolation):
        await store.claim_send(other, "k", now=NOW, fence=fence)
    with raises(ScopeViolation):
        await store.advance_operation(other, "k", "failed", now=NOW, fence=fence)
    record = await store.get_operation(SCOPE, "k")
    assert record is not None and record.operation.status == "pending"


async def check_operation_keys_are_per_tenant_and_account(make: StoreMaker) -> None:
    store = make()
    other = Scope(tenant_id=TENANT_B, account_id="a1", principal_id="u1", authorization_id="z")
    await store.begin_operation(SCOPE, key="k", request_digest="d1", operation_id="o1", now=NOW)
    begun = await store.begin_operation(
        other, key="k", request_digest="d2", operation_id="o2", now=NOW
    )
    assert begun.fresh


async def check_an_operation_slot_must_be_in_scope(make: StoreMaker) -> None:
    store = make()
    foreign = Slot(thread=THREAD, account_id="a2")
    with raises(ScopeViolation):
        await _begin(store, slot=foreign)
    other_tenant = Slot(
        thread=ThreadRef(
            channel=CHANNEL.model_copy(update={"tenant_id": TENANT_C}), thread_id="th1"
        )
    )
    with raises(ScopeViolation):
        await _begin(store, slot=other_tenant)


async def check_a_slot_bound_operation_needs_its_own_lease(make: StoreMaker) -> None:
    store = make()
    await _begin(store)
    with raises(ScopeViolation):
        await store.claim_send(SCOPE, "k", now=NOW, fence=None)
    other_thread = Slot(thread=ThreadRef(channel=CHANNEL, thread_id="th2"), account_id="a1")
    foreign = await _lease(store, other_thread)
    with raises(ScopeViolation):
        await store.claim_send(SCOPE, "k", now=NOW, fence=foreign)
    mine = await _lease(store)
    forged = mine.model_copy(update={"holder": "intruder"})
    with raises(StaleFence):
        await store.claim_send(SCOPE, "k", now=NOW, fence=forged)
    forged_turn = mine.model_copy(update={"turn_id": "turn_9"})
    with raises(StaleFence):
        await store.claim_send(SCOPE, "k", now=NOW, fence=forged_turn)
    await store.claim_send(SCOPE, "k", now=NOW, fence=mine)


async def check_lease_one_root_turn_and_monotonic_fence(make: StoreMaker) -> None:
    store = make()
    first = await _lease(store)
    assert first.fence == 1 and not first.took_over
    with raises(LeaseBusy):
        await _lease(store, holder="w2")
    # Another caller's private slot on the same thread is its own lease.
    assert (await _lease(store, Slot(thread=THREAD, account_id="a2"), "w2")).fence == 1
    await store.release_lease(first)
    await store.release_lease(first)
    second = await _lease(store, holder="w2")
    assert second.fence == 2
    with raises(StaleFence):
        await store.release_lease(first)
    with raises(StaleFence):
        await store.renew_lease(second.model_copy(update={"holder": "w1"}), now=NOW, ttl=TTL)


async def check_stale_fence_cannot_commit(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    old = await _lease(store)
    later = NOW + TTL + timedelta(seconds=1)
    new = await store.acquire_lease(PRIVATE_A, holder="w2", turn_id="t1", now=later, ttl=TTL)
    assert new.fence == 2 and new.took_over
    await _begin(store)
    with raises(StaleFence):
        await store.claim_send(SCOPE, "k", now=later, fence=old)
    with raises(StaleFence):
        await store.append_events(SESSION, [_message("e1")], fence=old, cursor="c1", now=later)
    with raises(StaleFence):
        await store.renew_lease(old, now=later, ttl=TTL)
    assert await store.read_events(SESSION.id) == []
    await store.append_events(SESSION, [], fence=new, cursor="c1", now=later)


async def check_a_session_journal_takes_only_its_slot_lease(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    mine = await _lease(store)
    await store.append_events(SESSION, [_message("e1")], fence=mine, cursor="c1", now=NOW)
    for slot in (
        Slot(thread=THREAD, account_id="a2"),
        Slot(thread=ThreadRef(channel=CHANNEL, thread_id="th2"), account_id="a1"),
        Slot(
            thread=ThreadRef(
                channel=CHANNEL.model_copy(update={"tenant_id": TENANT_B}), thread_id="th1"
            ),
            account_id="a1",
        ),
    ):
        foreign = await _lease(store, slot)
        with raises(ScopeViolation):
            await store.append_events(
                SESSION, [_message("e2")], fence=foreign, cursor="c2", now=NOW
            )
    assert len(await store.read_events(SESSION.id)) == 1


async def check_c05_overlapping_pages_and_child_completion(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    fence = await _lease(store)
    running = _entry("e1", "session.status_running", {"root_turn_id": "root"}, turn_id="root")
    message = _message("e2")
    child_end = _entry(
        "e3",
        "session.turn_ended",
        {"root_turn_id": "child", "outcome": "completed"},
        turn_id="child",
    )
    await store.append_events(SESSION, [running, message], fence=fence, cursor="c2", now=NOW)
    # The saved-items page overlaps what the stream already delivered.
    result = await store.append_events(
        SESSION, [message, child_end, child_end], fence=fence, cursor="c3", now=NOW
    )
    assert result.duplicates == 2
    assert [e.sequence for e in await store.read_events(SESSION.id)] == [0, 1, 2]
    assert result.projection.state == "running"
    assert result.projection.active_root_turn == "root"
    assert result.projection.cursor == "c3"
    gap = _entry("e4", "session.history_gap", {"domain": "stream", "recoverable": False})
    end = _entry(
        "e5",
        "session.turn_ended",
        {"root_turn_id": "root", "outcome": "completed"},
        turn_id="root",
    )
    final = await store.append_events(SESSION, [gap, end], fence=fence, cursor="c5", now=NOW)
    assert final.projection.state == "idle"
    assert final.projection.gaps == ("ev_e4_1_record",)


async def check_previews_never_complete_a_turn_nor_block_the_record(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    fence = await _lease(store)
    running = _entry("e1", "session.status_running", {"root_turn_id": "root"}, turn_id="root")
    await store.append_events(SESSION, [running], fence=fence, cursor="c1", now=NOW)
    ended = {"root_turn_id": "root", "outcome": "completed"}
    preview = _entry("end", "session.turn_ended", ended, authority="preview", turn_id="root")
    result = await store.append_events(SESSION, [preview], fence=fence, cursor="c2", now=NOW)
    assert len(result.appended) == 1
    assert result.projection.state == "running"
    record = _entry("end", "session.turn_ended", ended, turn_id="root")
    result = await store.append_events(SESSION, [record], fence=fence, cursor="c3", now=NOW)
    assert len(result.appended) == 1
    assert result.projection.state == "idle"


async def check_journal_revision_of_same_source_is_a_new_entry(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    fence = await _lease(store)
    end = {"root_turn_id": "turn_1", "outcome": "errored"}
    corrected = {"root_turn_id": "turn_1", "outcome": "completed"}
    await store.append_events(
        SESSION, [_entry("e1", "session.turn_ended", end)], fence=fence, cursor="a", now=NOW
    )
    result = await store.append_events(
        SESSION,
        [_entry("e1", "session.turn_ended", corrected, authority="reconciled", revision=2)],
        fence=fence,
        cursor="b",
        now=NOW,
    )
    assert len(result.appended) == 1 and result.appended[0].sequence == 1


async def check_committed_state_cannot_be_changed_through_values(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    fence = await _lease(store)
    await _begin(store)
    await store.claim_send(SCOPE, "k", now=NOW, fence=fence)
    record = await store.advance_operation(
        SCOPE, "k", "accepted", now=NOW, fence=fence, result={"input_ids": ["in_1"]}
    )
    input_ids = record.result["input_ids"]
    assert isinstance(input_ids, list)
    input_ids.append("forged")
    entry = _message("e1", "original")
    await store.append_events(SESSION, [entry], fence=fence, cursor="c", now=NOW)
    entry.event.payload["content"][0]["text"] = "forged"  # type: ignore[index]
    (read,) = await store.read_events(SESSION.id)
    read.payload["content"][0]["text"] = "forged too"  # type: ignore[index]
    restarted = make()
    stored = await restarted.get_operation(SCOPE, "k")
    assert stored is not None and stored.result["input_ids"] == ["in_1"]
    (event,) = await restarted.read_events(SESSION.id)
    assert event.payload["content"][0]["text"] == "original"  # type: ignore[index]


async def check_c07_usage_revisions_apply_signed_deltas_once(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    rows = [
        await store.record_usage("b_s1", _usage(r, out))
        for r, out in [(1, None), (2, 100), (3, 120), (4, 110)]
    ]
    deltas = [row.deltas["output_tokens"] for row in rows if row is not None]
    assert deltas == [None, 100, 20, -10]
    # Every revision replayed, and the stale 100 again: nothing new.
    for r, out in [(1, None), (2, 100), (3, 120), (4, 110), (2, 100)]:
        assert await store.record_usage("b_s1", _usage(r, out)) is None
    pending = await store.pending_outbox()
    assert [row.prior_applied_revision for row in pending] == [None, 1, 2, 3]
    total = 0
    for row in pending:
        assert await store.mark_outbox_applied(row)
        assert not await store.mark_outbox_applied(row)
        total += row.deltas["output_tokens"] or 0
    assert total == 110
    assert await store.pending_outbox() == []


async def check_a_changed_replay_of_the_current_revision_is_refused(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    await store.record_usage("b_s1", _usage(1, 110))
    with raises(UsageRevisionConflict):
        await store.record_usage("b_s1", _usage(1, 999))


async def check_usage_null_after_known_keeps_the_accounted_value(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    await store.record_usage("b_s1", _usage(1, 100))
    row = await store.record_usage("b_s1", _usage(2, None))
    assert row is not None and row.is_noop
    later = await store.record_usage("b_s1", _usage(3, 130))
    assert later is not None and later.deltas["output_tokens"] == 30


async def check_a_foreign_lease_cannot_claim_an_unwritten_journal(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    for slot in (
        Slot(thread=THREAD, account_id="a2"),
        Slot(thread=ThreadRef(channel=CHANNEL, thread_id="th2"), account_id="a1"),
        Slot(
            thread=ThreadRef(
                channel=CHANNEL.model_copy(update={"tenant_id": TENANT_B}), thread_id="th1"
            ),
            account_id="a1",
        ),
    ):
        foreign = await _lease(store, slot)
        with raises(ScopeViolation):
            await store.append_events(
                SESSION, [_message("e1")], fence=foreign, cursor="c1", now=NOW
            )
    assert await store.read_events(SESSION.id) == []
    mine = await _lease(store)
    await store.append_events(SESSION, [_message("e1")], fence=mine, cursor="c1", now=NOW)


async def check_a_session_no_binding_names_takes_no_appends(make: StoreMaker) -> None:
    store = make()
    fence = await _lease(store)
    with raises(ScopeViolation):
        await store.append_events(SESSION, [_message("e1")], fence=fence, cursor="c1", now=NOW)


async def check_a_rebind_keeps_the_old_session_journal_with_its_slot(make: StoreMaker) -> None:
    store = make()
    first = await _own_session(store)
    rebound = first.model_copy(update={"generation": 2, "native_refs": {"session": "s2"}})
    await store.put_binding(rebound, expected_generation=1)
    mine = await _lease(store)
    await store.append_events(SESSION, [_message("e1")], fence=mine, cursor="c1", now=NOW)
    other = _binding(session=SESSION.id, id_="b_other", account="a2")
    with raises(ValueError, match="another slot"):
        await store.put_binding(other, expected_generation=0)


async def check_usage_is_charged_only_under_the_binding_of_its_session(make: StoreMaker) -> None:
    store = make()
    await _own_session(store)
    other = _binding(session="s9", id_="b_s9", account="a2")
    await store.put_binding(other, expected_generation=0)
    for binding_id in ("b_unknown", "b_s9"):
        with raises(ScopeViolation):
            await store.record_usage(binding_id, _usage(1, 100))
    assert await store.record_usage("b_s1", _usage(1, 100)) is not None


CHECKS: tuple[Callable[[StoreMaker], Awaitable[None]], ...] = tuple(
    check for name, check in sorted(globals().items()) if name.startswith("check_")
)
"""Every check, for a store's test module to parametrize over."""
