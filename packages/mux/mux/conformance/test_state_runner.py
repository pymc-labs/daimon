"""Broken implementations must fail the new state probes, including with -O."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import cast

import pytest

from mux.conformance.fixtures import FIXTURES
from mux.conformance.reference import ReferenceDriver, ReferenceEvents, Transport, create
from mux.conformance.runner import Adapter, Registry, run
from mux.contracts.actions import InputEvent, UserMessage
from mux.contracts.errors import BindingConflict
from mux.contracts.events import TextPart
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.state.lease import Lease, Slot
from mux.state.memory import CrashPoint, MemoryStateData, MemoryStateStore
from mux.state.operations import OperationRecord
from mux.state.store import binding_slot
from mux.state.usage_ledger import OutboxRow


class RestartableVariant(MemoryStateStore):
    def restart(self, *, crash: Mapping[str, CrashPoint] | None = None) -> RestartableVariant:
        return type(self)(self.data, crash=crash)


class DuplicateSend(ReferenceEvents):
    def _submit(self, events: Sequence[InputEvent], *, key: str) -> SendReceipt:
        super()._submit(events, key=key)
        return super()._submit(events, key=key)


class IgnoreChangedInput(ReferenceEvents):
    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        if key == "same-key":
            events = (UserMessage(content=(TextPart(text="durable input"),)),)
        return await super().send(scope, session, events, key=key, expected_turn=expected_turn)


class ResendUnknown(ReferenceEvents):
    async def send(
        self,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        if key == "unknown-key" and await self.t.store.get_operation(scope, key) is not None:
            self._submit(events, key=key)
        return await super().send(scope, session, events, key=key, expected_turn=expected_turn)


class InventCompletion(ReferenceEvents):
    @staticmethod
    def _receipt(record: OperationRecord) -> SendReceipt:
        return SendReceipt(
            operation_id=record.operation.id, status="processed", input_ids=("invented",)
        )


class QueuedAcknowledgement(ReferenceEvents):
    @staticmethod
    def _receipt(record: OperationRecord) -> SendReceipt:
        receipt = ReferenceEvents._receipt(record)
        return (
            receipt.model_copy(update={"status": "queued"})
            if receipt.status == "processed"
            else receipt
        )


class ForgetRestart(RestartableVariant):
    def restart(self, *, crash: Mapping[str, CrashPoint] | None = None) -> ForgetRestart:
        return type(self)(self.data if crash is not None else None, crash=crash)


class PartialJournal(RestartableVariant):
    def _commit(self, method: str, staged: MemoryStateData) -> None:
        if method == "append_events":
            self.data.commit(staged)
        super()._commit(method, staged)


class LoseCursor(RestartableVariant):
    def _commit(self, method: str, staged: MemoryStateData) -> None:
        if method == "append_events":
            staged.projections = {
                key: value.model_copy(update={"cursor": "lost"})
                for key, value in staged.projections.items()
            }
        super()._commit(method, staged)


class IgnoreFence(RestartableVariant):
    def _fenced(
        self, staged: MemoryStateData, target: Slot | None, fence: Lease | None, now: datetime
    ) -> None:
        return


class LeaseLossMeansUnsent(RestartableVariant):
    async def acquire_lease(
        self, slot: Slot, *, holder: str, turn_id: str, now: datetime, ttl: timedelta
    ) -> Lease:
        lease = await super().acquire_lease(slot, holder=holder, turn_id=turn_id, now=now, ttl=ttl)
        if lease.took_over:
            self.data.operations = {
                key: record.model_copy(
                    update={"operation": record.operation.model_copy(update={"status": "pending"})}
                )
                if record.operation.status in ("sent", "accepted", "outcome_unknown")
                else record
                for key, record in self.data.operations.items()
            }
        return lease


class NullIsZero(RestartableVariant):
    async def record_usage(
        self, binding_id: str, observation: UsageObservation
    ) -> OutboxRow | None:
        if observation.output_tokens is None:
            observation = observation.model_copy(update={"output_tokens": 0})
        return await super().record_usage(binding_id, observation)


class UnsignedCorrection(RestartableVariant):
    async def record_usage(
        self, binding_id: str, observation: UsageObservation
    ) -> OutboxRow | None:
        row = await super().record_usage(binding_id, observation)
        if row is not None and (row.deltas["output_tokens"] or 0) < 0:
            return row.model_copy(update={"deltas": {**row.deltas, "output_tokens": 10}})
        return row


class ReplayUsage(RestartableVariant):
    async def record_usage(
        self, binding_id: str, observation: UsageObservation
    ) -> OutboxRow | None:
        row = await super().record_usage(binding_id, observation)
        return row or OutboxRow(
            binding_id=binding_id,
            observation_id=observation.id,
            revision=observation.revision,
            deltas={},
            observation=observation,
        )


class DoubleApply(RestartableVariant):
    async def mark_outbox_applied(self, row: OutboxRow) -> bool:
        await super().mark_outbox_applied(row)
        return True


class LostOutbox(RestartableVariant):
    async def pending_outbox(self, *, limit: int = 100) -> Sequence[OutboxRow]:
        return []


class SplitWinner(RestartableVariant):
    async def put_binding(
        self, binding: ProviderBinding, *, expected_generation: int
    ) -> ProviderBinding:
        try:
            return await super().put_binding(binding, expected_generation=expected_generation)
        except BindingConflict:
            return binding


def variant(
    events: type[ReferenceEvents] = ReferenceEvents,
    store: type[MemoryStateStore] = MemoryStateStore,
) -> Adapter:
    t = Transport()
    t.store = store()
    driver = ReferenceDriver(t)
    driver.events = events(t)
    return Adapter(cast(ManagedAgents, driver), t.store, t)


MUTANTS: dict[str, tuple[str, Callable[[], Adapter]]] = {
    "duplicate_send": ("C03", lambda: variant(events=DuplicateSend)),
    "ignore_digest": ("C03", lambda: variant(events=IgnoreChangedInput)),
    "resend_unknown": ("C03", lambda: variant(events=ResendUnknown)),
    "lost_restart": ("C04", lambda: variant(store=ForgetRestart)),
    "invent_completion": ("C04", lambda: variant(events=InventCompletion)),
    "partial_journal": ("C04", lambda: variant(store=PartialJournal)),
    "lost_cursor": ("C04", lambda: variant(store=LoseCursor)),
    "stale_fence": ("C04", lambda: variant(store=IgnoreFence)),
    "lease_loss_unsent": ("C04", lambda: variant(store=LeaseLossMeansUnsent)),
    "null_zero": ("C07", lambda: variant(store=NullIsZero)),
    "unsigned_correction": ("C07", lambda: variant(store=UnsignedCorrection)),
    "replayed_usage": ("C07", lambda: variant(store=ReplayUsage)),
    "double_apply": ("C07", lambda: variant(store=DoubleApply)),
    "lost_outbox": ("C07", lambda: variant(store=LostOutbox)),
    "split_winner": ("C13", lambda: variant(store=SplitWinner)),
}


@pytest.mark.parametrize("name", MUTANTS)
async def test_state_mutant_fails_its_semantic_probe(name: str) -> None:
    fixture, factory = MUTANTS[name]
    registry = Registry()
    registry.register(name, factory)
    result = next(r for r in await run(registry, name) if r.fixture_id == fixture)
    assert result.status == "fail" and result.evidence[0].startswith(f"check failed: {fixture}:")


@pytest.mark.parametrize("fixture", ["C03", "C04", "C07", "C13"])
async def test_missing_state_adapter_stays_pending(fixture: str) -> None:
    a = create()
    result = await FIXTURES[fixture](a.driver, None, a.transport)
    assert result.status == "pending" and "StateStore" in result.evidence[0]


@pytest.mark.parametrize("fixture", ["C03", "C04"])
async def test_queued_acknowledgement_is_valid_without_turn_completion(fixture: str) -> None:
    a = variant(events=QueuedAcknowledgement)
    result = await FIXTURES[fixture](a.driver, a.store, a.transport)
    assert result.status == "pass"


async def check_optimized_matrix() -> None:
    registry = Registry()
    registry.register("reference", create)
    results = await run(registry, "reference")
    if {r.fixture_id for r in results if r.status == "pending"} != {
        "C01",
        "C12",
        "C14",
        "C17",
        "C18",
    }:
        raise RuntimeError("reference pending matrix changed under optimized Python")
    if any(r.status == "fail" for r in results):
        raise RuntimeError("valid reference failed under optimized Python")
    for fixture in ("C03", "C04"):
        adapter = variant(events=QueuedAcknowledgement)
        result = await FIXTURES[fixture](adapter.driver, adapter.store, adapter.transport)
        if result.status != "pass":
            raise RuntimeError("valid queued acknowledgement failed under optimized Python")
    for name, (fixture, factory) in MUTANTS.items():
        registry.register(name, factory)
        result = next(r for r in await run(registry, name) if r.fixture_id == fixture)
        if result.status != "fail" or not result.evidence[0].startswith(
            f"check failed: {fixture}:"
        ):
            raise RuntimeError(f"{name} escaped its semantic probe under optimized Python")


def test_state_mutants_and_valid_matrix_under_optimized_python() -> None:
    code = (
        "from mux.conformance.test_state_runner import check_optimized_matrix; "
        "import asyncio; asyncio.run(check_optimized_matrix())"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", code], text=True, capture_output=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr


async def test_reference_factories_do_not_share_committed_state() -> None:
    first, second = create(), create()
    assert first.store is not None and second.store is not None and first.store is not second.store
    scenario = await first.transport.arrange("C13")
    await first.store.put_binding(scenario.session.binding, expected_generation=0)
    assert await second.store.get_binding(binding_slot(scenario.session.binding)) is None
