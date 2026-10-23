"""The workspace-transfer checkpoint turn, started from chat-turn preparation.

`bind_session_impl` builds the production `WorkspaceTransferRunner` and hands it
to `prepare_session_for_turn`. When a thread's session must be replaced and the
old one has something worth handing over, the runner drives a real checkpoint
turn on the old session. These tests start from that preparation entry point
(not `transfer_workspace`) on both turn paths, and spy on
`daimon.core.turn.driver.turn_io` to prove which turn I/O the checkpoint used.

Two contexts matter on mux, where the driver reads `current_persistence`:

* chat preparation, which runs before any turn persistence is active;
* preparation that runs inside another turn's activated persistence (a fleet
  worker allocation, a headless run started from a turn). The checkpoint turn
  must not inherit that turn's persistence, which owns a different session.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from daimon.core import headless_runner, workspace_transfer
from daimon.core.checkpoint_prompt import handoff_filename
from daimon.core.stores import mux_state
from daimon.core.turn.binding import prepared_persistence
from daimon.core.turn.persistence import TurnPersistence, current_persistence
from daimon.core.turn.prepare import PreparedTurn, bind_session_impl
from daimon.core.workspace_transfer import FullHandoff
from daimon.testing.factories import make_account, make_tenant
from mux.state.memory import MemoryStateStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_headless_runner import (
    _build_client,  # pyright: ignore[reportPrivateUsage]
    _idle_events,  # pyright: ignore[reportPrivateUsage]
)
from .test_session_preparation import (
    _admission,  # pyright: ignore[reportPrivateUsage]
    _agent,  # pyright: ignore[reportPrivateUsage]
    _deps,  # pyright: ignore[reportPrivateUsage]
    _register,  # pyright: ignore[reportPrivateUsage]
    _Transport,  # pyright: ignore[reportPrivateUsage]
)
from .test_workspace_transfer import (
    TARBALL,
    _script_checkpoint_reply,  # pyright: ignore[reportPrivateUsage]
    _seed_conversation,  # pyright: ignore[reportPrivateUsage]
)
from .turn_path_support import HeadlessIdentity, TurnIOSpy, TurnPath


class _TransferSpy:
    """Record what `transfer_workspace` was called with, then run it unchanged.

    The handoff bundle's filename carries the transfer id, which the
    preparation row generates, so the fake's output file is written when the
    transfer starts rather than before it.
    """

    def __init__(self, transport: _Transport) -> None:
        self.transport = transport
        self.calls: list[dict[str, Any]] = []
        self.outcomes: list[object] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = workspace_transfer.transfer_workspace

        async def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            self.transport.state.write_output(
                kwargs["old_session_id"],
                handoff_filename(kwargs["transfer_id"]),
                TARBALL,
            )
            outcome = await real(*args, **kwargs)
            self.outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(workspace_transfer, "transfer_workspace", spy)


async def _bind(
    deps: Any,
    agent: Any,
    *,
    tenant_id: uuid.UUID,
    account: Any,
    thread_id: str,
) -> PreparedTurn:
    return await bind_session_impl(
        deps,
        _admission(account=account, agent=agent),
        tenant_id=tenant_id,
        platform="discord",
        external_user_id="user-1",
        thread_id=thread_id,
        session_account_id=account.id,
        reuse_existing=True,
    )


async def _open_unrelated_turn(deps: Any, *, tenant_id: uuid.UUID, account: Any) -> TurnPersistence:
    """The persistence of a turn on another thread, ready to `activate()`."""
    other = await _bind(deps, _agent(), tenant_id=tenant_id, account=account, thread_id="thread-2")
    return await prepared_persistence(
        deps,
        other.admission,
        tenant_id=tenant_id,
        platform="discord",
        thread_id="thread-2",
        session_id=other.ma_session_id,
        mapping_id=other.mapping_id,
        session_ref=other.session_ref,
        operation_key="outer-turn",
    )


async def _worth_checkpointing_thread(
    transport: _Transport, deps: Any, *, tenant_id: uuid.UUID, account: Any, thread_id: str
) -> PreparedTurn:
    """Bind a thread's first session and give it a conversation worth handing over."""
    first = await _bind(deps, _agent(), tenant_id=tenant_id, account=account, thread_id=thread_id)
    _seed_conversation(transport.state, first.ma_session_id, with_reply=True)
    _script_checkpoint_reply(
        transport.state, first.ma_session_id, "-rw-r--r-- 1 root root 42 h.tgz"
    )
    return first


async def test_chat_preparation_runs_the_checkpoint_turn_on_the_selected_path(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    turn_path: TurnPath,
    turn_io_used: TurnIOSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model change replaces the thread's session; the runner built by
    `bind_session_impl` checkpoints the old one on the path the deployment
    selected and the successor receives the bundle."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = replace(_deps(db_session_factory, transport), turn_path=turn_path)
    spy = _TransferSpy(transport)
    spy.install(monkeypatch)

    first = await _worth_checkpointing_thread(
        transport, deps, tenant_id=tenant.id, account=account, thread_id="thread-1"
    )
    assert turn_io_used.classes == [], "creating a session runs no turn"
    assert current_persistence.get() is None, "chat preparation has no outer turn persistence"

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    second = await _bind(deps, moved, tenant_id=tenant.id, account=account, thread_id="thread-1")

    assert second.ma_session_id != first.ma_session_id
    assert second.continuity.state == "replaced"
    assert len(spy.calls) == 1, "the production runner started exactly one transfer"
    assert spy.calls[0]["turn_path"] == turn_path, "the runner passes the deployment's turn path"
    assert spy.calls[0]["account_id"] == account.id
    assert isinstance(spy.outcomes[0], FullHandoff), f"expected a full handoff: {spy.outcomes[0]!r}"
    assert second.continuity.transfer_kind == "full"
    sent_to_old = [sid for sid, _ in transport.state.sent_batches]
    assert sent_to_old == [first.ma_session_id], "the checkpoint executed the OLD session"
    turn_io_used.assert_ran(turn_path)
    assert len(turn_io_used.classes) == 1


async def test_a_checkpoint_started_inside_another_turn_does_not_inherit_its_persistence(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    turn_path: TurnPath,
    turn_io_used: TurnIOSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation can run while an unrelated turn's persistence is active (a
    fleet worker allocation, a headless run started from a turn). On mux that
    persistence owns a different session, so a checkpoint turn that inherited
    it would be refused by `check_session`. The checkpoint turn runs in a
    context of its own and the outer turn's persistence is left active."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = replace(
        _deps(db_session_factory, transport),
        turn_path=turn_path,
        state_store=mux_state.PostgresStateStore(db_session_factory),
    )
    spy = _TransferSpy(transport)
    spy.install(monkeypatch)

    first = await _worth_checkpointing_thread(
        transport, deps, tenant_id=tenant.id, account=account, thread_id="thread-1"
    )
    outer = await _open_unrelated_turn(deps, tenant_id=tenant.id, account=account)

    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)
    with outer.activate():
        second = await _bind(
            deps, moved, tenant_id=tenant.id, account=account, thread_id="thread-1"
        )
        assert current_persistence.get() is outer, "the outer turn's persistence stays active"

    assert second.ma_session_id != first.ma_session_id
    assert isinstance(spy.outcomes[0], FullHandoff), (
        f"the checkpoint turn was refused or failed inside the outer turn: {spy.outcomes[0]!r}"
    )
    assert [sid for sid, _ in transport.state.sent_batches] == [first.ma_session_id]
    turn_io_used.assert_ran(turn_path)


async def test_a_headless_run_started_inside_another_turn_does_not_inherit_its_persistence(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    headless_identity: HeadlessIdentity,
    turn_path: TurnPath,
    turn_io_used: TurnIOSpy,
) -> None:
    """The same hazard for every headless caller (routines, smoke, `daimon
    run`): a headless run owns its own session, not the one the enclosing turn
    owns, so it starts without the enclosing turn's persistence."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    deps = replace(
        _deps(db_session_factory, _Transport()),
        turn_path=turn_path,
        state_store=mux_state.PostgresStateStore(db_session_factory),
    )
    outer = await _open_unrelated_turn(deps, tenant_id=tenant.id, account=account)

    with outer.activate():
        tail = await headless_runner.run_turn(
            anthropic=_build_client(_idle_events()),
            agent_id="agent_x",
            environment_id="env_x",
            trigger_message="hi",
            tenant_id=headless_identity.tenant_id,
            account_id=headless_identity.account_id,
            turn_path=turn_path,
        )
        assert current_persistence.get() is outer, "the enclosing turn's persistence is restored"

    assert tail == "ok"
    turn_io_used.assert_ran(turn_path)


async def test_a_checkpoint_inside_the_same_slots_worker_allocation_neither_waits_nor_is_fenced_by_it(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    turn_path: TurnPath,
    turn_io_used: TurnIOSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet worker prepares inside `allocation.run(...)`, a binding-less
    persistence for the worker's own slot that holds that slot's lease
    (`headless_runner.py` allocation, `TurnPersistence.run`). If a live session
    for that same thread exists without a published binding (a crash between
    the mapping write and the binding), the replacement checkpoints it inside
    that allocation. The allocation can never own the session
    (`check_session` needs an acknowledged binding), and a checkpoint turn
    that took its own lease on the slot would wait on the one the allocation
    holds until the deadline. It must instead run unleased, to completion."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    transport = _Transport()
    deps = replace(
        _deps(db_session_factory, transport),
        turn_path=turn_path,
        state_store=mux_state.PostgresStateStore(db_session_factory),
    )
    spy = _TransferSpy(transport)
    spy.install(monkeypatch)
    first = await _worth_checkpointing_thread(
        transport, deps, tenant_id=tenant.id, account=account, thread_id="thread-1"
    )
    # The slot and scope of the thread's turns, read from a persistence built
    # against a scratch store so that no binding is published in the real one.
    scratch = await prepared_persistence(
        replace(deps, state_store=MemoryStateStore()),
        first.admission,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        session_id=first.ma_session_id,
        mapping_id=first.mapping_id,
        session_ref=first.session_ref,
        operation_key="scratch",
    )
    store = deps.state_store
    assert store is not None
    assert await store.get_binding(scratch.slot) is None, "the old session has no published binding"
    allocation = TurnPersistence(
        store, None, scratch.scope, slot=scratch.slot, operation_key="worker-allocation"
    )
    moved = _agent(model_id="claude-opus-5")
    _register(transport.state, moved)

    with allocation.activate():
        second = await asyncio.wait_for(
            allocation.run(
                lambda: _bind(
                    deps, moved, tenant_id=tenant.id, account=account, thread_id="thread-1"
                )
            ),
            timeout=30,
        )

    assert second.ma_session_id != first.ma_session_id
    assert isinstance(spy.outcomes[0], FullHandoff), (
        f"the checkpoint was refused or failed inside the allocation: {spy.outcomes[0]!r}"
    )
    assert [sid for sid, _ in transport.state.sent_batches] == [first.ma_session_id]
    turn_io_used.assert_ran(turn_path)
