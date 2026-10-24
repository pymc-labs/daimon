"""C03 waits for owned root completion; acceptance never frees occupancy."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Sequence
from typing import cast

import pytest
from mux.contracts.actions import InputEvent
from mux.contracts.events import Event
from mux.contracts.ids import Page, PageRequest, ResourceRef, Scope
from mux.contracts.receipts import SendReceipt
from mux.contracts.resources import Session
from mux.state.lease import Lease
from mux.state.operations import OperationRecord
from mux.state.store import StateStore
from mux.testing.conformance import state_fixtures
from mux.testing.conformance.reference import (
    NOW,
    ReferenceDriver,
    ReferenceEvents,
    ReferenceSessions,
    create,
)
from mux.testing.conformance.runner import ConformanceFailure


async def test_c03_waits_past_idle_until_matching_saved_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create()
    driver = cast(ReferenceDriver, adapter.driver)
    transport = driver.events.t
    saved_list = ReferenceEvents.list
    saved_send = ReferenceEvents.send
    reads = 0
    occupied = 0

    async def list_(
        self: ReferenceEvents, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[Event]:
        nonlocal reads
        reads += 1
        history = await saved_list(self, scope, session, page=page)
        if reads < 3:
            return Page[Event](
                data=tuple(e for e in history.data if e.type != "session.turn_ended")
            )
        return history

    async def retrieve(self: ReferenceSessions, scope: Scope, session: ResourceRef) -> Session:
        nonlocal occupied
        occupied += 1
        self.t.check(scope, session)
        return self.t.session.model_copy(
            update={"state": "running", "active_root_turn": "same-key"}
            if occupied == 1
            else {"state": "idle", "active_root_turn": None}
        )

    async def send(
        self: ReferenceEvents,
        scope: Scope,
        session: ResourceRef,
        events: Sequence[InputEvent],
        *,
        key: str,
        expected_turn: str | None = None,
    ) -> SendReceipt:
        if key == "unknown-key" and self.t.mutation_count == 1 and (reads < 3 or occupied < 3):
            raise AssertionError("second new-turn send preceded real terminal/occupancy reads")
        return await saved_send(self, scope, session, events, key=key, expected_turn=expected_turn)

    monkeypatch.setattr(ReferenceEvents, "list", list_)
    monkeypatch.setattr(ReferenceEvents, "send", send)
    monkeypatch.setattr(ReferenceSessions, "retrieve", retrieve)
    monkeypatch.setattr(state_fixtures, "C03_COMPLETION_POLL_S", 0.001)
    result = await state_fixtures.c03(adapter.driver, adapter.store, transport)
    assert result.status == "pass" and transport.mutation_count == 2
    assert reads >= 3 and occupied >= 3


@pytest.mark.parametrize(
    "mutation",
    [
        "idle_only",
        "busy",
        "foreign_root",
        "foreign_session",
        "foreign_provider",
        "child",
        "preview",
        "gap",
        "payload_root",
    ],
)
async def test_c03_unproven_completion_stops_before_second_send(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    adapter = create()
    driver = cast(ReferenceDriver, adapter.driver)
    saved_list = ReferenceEvents.list

    async def list_(
        self: ReferenceEvents, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[Event]:
        history = await saved_list(self, scope, session, page=page)
        terminal = next(e for e in history.data if e.type == "session.turn_ended")
        if mutation == "idle_only":
            return Page[Event](data=())
        mutations: dict[str, dict[str, object]] = {
            "foreign_root": {
                "turn_id": "foreign",
                "payload": {"root_turn_id": "foreign", "outcome": "completed"},
            },
            "foreign_session": {"session_id": "foreign"},
            "foreign_provider": {
                "native": terminal.native.model_copy(update={"provider": "openai"})
            },
            "child": {"thread_id": "child"},
            "preview": {"authority": "preview"},
            "gap": {"authority": "gap"},
            "payload_root": {"payload": {"root_turn_id": "foreign", "outcome": "completed"}},
        }
        return Page[Event](data=(terminal.model_copy(update=mutations.get(mutation, {})),))

    async def retrieve(self: ReferenceSessions, scope: Scope, session: ResourceRef) -> Session:
        self.t.check(scope, session)
        return self.t.session.model_copy(
            update={"state": "running", "active_root_turn": "same-key"}
            if mutation == "busy"
            else {"state": "idle", "active_root_turn": None}
        )

    monkeypatch.setattr(ReferenceEvents, "list", list_)
    monkeypatch.setattr(ReferenceSessions, "retrieve", retrieve)
    monkeypatch.setattr(state_fixtures, "C03_COMPLETION_TIMEOUT_S", 0.02)
    monkeypatch.setattr(state_fixtures, "C03_COMPLETION_POLL_S", 0.001)
    with pytest.raises(ConformanceFailure, match="first root completion was not observed"):
        await state_fixtures.c03(adapter.driver, adapter.store, driver.events.t)
    assert driver.events.t.mutation_count == 1 and not driver.events.t.faults
    assert await driver.events.t.store.get_operation(driver.events.t.scope, "unknown-key") is None


async def test_rootless_receipt_joins_owned_saved_input_and_paginated_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create()
    driver = cast(ReferenceDriver, adapter.driver)
    scenario = await driver.events.t.arrange("C03")
    input_ = driver.events.t.event(0, "user.message", {"input_id": "accepted-input", "content": []})
    terminal = driver.events.t.event(
        1, "session.turn_ended", {"root_turn_id": "root", "outcome": "completed"}
    )
    cursors: list[str | None] = []

    async def list_(
        self: ReferenceEvents, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[Event]:
        self.t.check(scope, session)
        cursors.append(page.cursor)
        return (
            Page[Event](data=(input_,), has_more=True, next_cursor="saved-end")
            if page.cursor is None
            else Page[Event](data=(terminal,))
        )

    monkeypatch.setattr(ReferenceEvents, "list", list_)
    receipt = SendReceipt(operation_id="accepted", status="queued", input_ids=("accepted-input",))
    await state_fixtures.c03_completion(
        adapter.driver, scenario.scope, scenario.session.ref, receipt
    )
    assert cursors == [None, "saved-end"] and driver.events.t.mutation_count == 0


async def test_completion_deadline_cancels_an_outstanding_history_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = create()
    driver = cast(ReferenceDriver, adapter.driver)
    cancelled = asyncio.Event()

    async def list_(
        self: ReferenceEvents, scope: Scope, session: ResourceRef, *, page: PageRequest
    ) -> Page[Event]:
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            cancelled.set()

    monkeypatch.setattr(ReferenceEvents, "list", list_)
    monkeypatch.setattr(state_fixtures, "C03_COMPLETION_TIMEOUT_S", 0.02)
    with pytest.raises(ConformanceFailure, match="first root completion was not observed"):
        await state_fixtures.c03(adapter.driver, adapter.store, driver.events.t)
    assert cancelled.is_set() and driver.events.t.mutation_count == 1 and not driver.events.t.faults


def test_completion_refusals_survive_optimization() -> None:
    script = """
import asyncio
from mux.testing.conformance import state_fixtures as f
from mux.testing.conformance.reference import create,ReferenceEvents
from mux.testing.conformance.runner import ConformanceFailure
from mux.contracts.ids import Page
f.C03_COMPLETION_TIMEOUT_S=.02
f.C03_COMPLETION_POLL_S=.001
saved=ReferenceEvents.list
async def check():
    for kind in ('idle_only','preview','foreign','child'):
        async def history(self,scope,session,*,page):
            original=await saved(self,scope,session,page=page)
            e=next(e for e in original.data if e.type=='session.turn_ended')
            changes={'preview':{'authority':'preview'},'foreign':{'session_id':'foreign'},'child':{'thread_id':'child'}}
            if kind=='idle_only': return Page(data=())
            return Page(data=(e.model_copy(update=changes[kind]),))
        ReferenceEvents.list=history
        a=create()
        try: await f.c03(a.driver,a.store,a.transport)
        except ConformanceFailure: pass
        else: raise RuntimeError('unproven completion accepted under -O')
        if a.transport.mutation_count!=1 or a.transport.faults:
            raise RuntimeError('second send or fault reached under -O')
asyncio.run(check())
"""
    run = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr.decode())


class ChangingInputRoots(ReferenceEvents):
    """Real durable acknowledgements; only authoritative history is corrupted."""

    def __init__(
        self, original: ReferenceEvents, *, rootless: bool, later: bool, paginated: bool, kind: str
    ) -> None:
        super().__init__(original.t)
        self.rootless = rootless
        self.later = later
        self.paginated = paginated
        self.kind = kind
        self.polls = 0
        self.pages = 0

    async def _record_acknowledgement(
        self, store: StateStore, scope: Scope, key: str, session: ResourceRef, fence: Lease
    ) -> OperationRecord:
        if not self.rootless:
            return await super()._record_acknowledgement(store, scope, key, session, fence)
        await store.advance_operation(
            scope,
            key,
            "accepted",
            now=NOW,
            fence=fence,
            resource=session,
            result={"input_ids": [key]},
        )
        return await store.advance_operation(scope, key, "processed", now=NOW, fence=fence)

    async def list(self, scope: Scope, session: ResourceRef, *, page: PageRequest) -> Page[Event]:
        self.t.check(scope, session)
        self.pages += 1
        if page.cursor is None:
            self.polls += 1
        root = "root-A" if self.rootless else "same-key"
        input_ = self.t.event(
            0, "user.message", {"input_id": "same-key", "content": []}
        ).model_copy(update={"turn_id": root})
        terminal = self.t.event(
            2, "session.turn_ended", {"root_turn_id": root, "outcome": "completed"}
        ).model_copy(update={"turn_id": root})
        if self.later and self.polls == 1:
            return Page[Event](data=(input_,))
        other = input_.model_copy(
            update={"id": "other-input-root", "sequence": 1, "turn_id": "root-B"}
        )
        records = (input_, other, terminal) if self.kind == "conflicting" else (other, terminal)
        if not self.paginated:
            return Page[Event](data=records)
        if page.cursor is None:
            return Page[Event](data=records[:1], has_more=True, next_cursor="remaining-roots")
        return Page[Event](data=records[1:])


async def check_changing_input_roots(
    *, rootless: bool, later: bool, paginated: bool, kind: str
) -> None:
    adapter = create()
    driver = cast(ReferenceDriver, adapter.driver)
    events = ChangingInputRoots(
        driver.events, rootless=rootless, later=later, paginated=paginated, kind=kind
    )
    driver.events = events
    diagnostic = (
        "C03: saved input has conflicting root identities"
        if kind == "conflicting"
        else "C03: saved input root identity changed"
    )
    # A rootless first poll may legitimately resolve to B. Reassignment needs
    # either an acknowledged root or a prior poll resolving A.
    try:
        await state_fixtures.c03(adapter.driver, adapter.store, events.t)
    except ConformanceFailure as exc:
        if str(exc) != diagnostic:
            raise AssertionError(f"unexpected refusal: {exc}") from exc
    else:
        raise AssertionError("conflicting/reassigned input root permitted canonical C03")
    if events.polls != (2 if later else 1) or events.pages != events.polls + int(paginated):
        raise AssertionError("mutation did not exercise the intended poll and pagination")
    if events.t.mutation_count != 1 or events.t.faults:
        raise AssertionError("root refusal occurred after second mutation or ack-loss fault")
    if await events.t.store.get_operation(events.t.scope, "unknown-key") is not None:
        raise AssertionError("root refusal occurred after second operation intent")


ROOT_MUTANTS = [
    (rootless, later, paginated, kind)
    for rootless in (False, True)
    for later in (False, True)
    for paginated in (False, True)
    for kind in ("conflicting", "reassigned")
    if not (rootless and not later and kind == "reassigned")
]


@pytest.mark.parametrize("rootless,later,paginated,kind", ROOT_MUTANTS)
async def test_canonical_c03_revalidates_input_root_on_every_poll(
    monkeypatch: pytest.MonkeyPatch, rootless: bool, later: bool, paginated: bool, kind: str
) -> None:
    monkeypatch.setattr(state_fixtures, "C03_COMPLETION_POLL_S", 0.001)
    await check_changing_input_roots(rootless=rootless, later=later, paginated=paginated, kind=kind)


def test_changing_input_root_refusals_survive_optimization() -> None:
    script = """
import asyncio
from mux.testing.conformance import state_fixtures
from packages.mux.tests.testing.conformance.test_c03_completion import ROOT_MUTANTS, check_changing_input_roots
state_fixtures.C03_COMPLETION_POLL_S = .001
async def check():
    for rootless, later, paginated, kind in ROOT_MUTANTS:
        await check_changing_input_roots(
            rootless=rootless, later=later, paginated=paginated, kind=kind)
asyncio.run(check())
"""
    run = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, timeout=30)
    if run.returncode:
        raise AssertionError(run.stderr.decode())
