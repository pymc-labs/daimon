"""Binding publication completes inside the existing preparation ceiling."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core import session_preparation
from daimon.core.errors import TurnError
from daimon.core.turn import prepare
from daimon.core.turn.outcomes import TurnObservation
from mux.state.memory import MemoryStateStore
from sqlalchemy.ext.asyncio import async_sessionmaker

from .test_provider_dispatch import ACCOUNT, PROFILE, SESSION, TENANT, admission, deps_for

_NOW = datetime(2026, 10, 12, tzinfo=UTC)


class WaitingStore(MemoryStateStore):
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.cancelled = False

    async def get_binding(self, slot):
        self.calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().get_binding(slot)


@pytest.fixture
def completed_preparation(monkeypatch):
    async def unchanged(deps, current):
        return current

    async def no_record(**kwargs):
        raise AssertionError("preparation must not bill")

    def no_database_finish(self, **kwargs):
        pass

    monkeypatch.setattr(prepare, "reauthorize", unchanged)
    monkeypatch.setattr(TurnObservation, "finish", no_database_finish)

    def completed(current, *, native=False):
        return prepare.PreparedTurn(
            current,
            SESSION.id,
            None,
            None,
            False,
            ACCOUNT,
            no_record,
            session_ref=SESSION if native else None,
        )

    return completed


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
@pytest.mark.parametrize("default_deadline", [False, True])
async def test_delayed_binding_owner_is_cancelled_by_the_original_preparation_deadline(
    monkeypatch,
    completed_preparation,
    provider,
    default_deadline,
):
    store = WaitingStore()
    deps = replace(deps_for(None, async_sessionmaker()), channel_backends=True, state_store=store)
    current = replace(admission(), origin_channel_id="channel")
    if provider == "anthropic":
        current = replace(current, backend_revision=None)
    prepared = completed_preparation(current, native=provider == "openai")
    preparation_completed = False

    async def completed(*args, **kwargs):
        nonlocal preparation_completed
        preparation_completed = True
        return prepared

    if provider == "anthropic":
        monkeypatch.setattr(session_preparation, "prepare_session_for_turn", completed)
    else:
        monkeypatch.setitem(prepare._PROVIDER_PREPARATIONS, PROFILE, completed)
    clocks = []

    def deadline_owner(*, now):
        clocks.append(now)
        return _NOW + timedelta(seconds=0.05)

    monkeypatch.setattr(prepare, "turn_deadline", deadline_owner)
    with pytest.raises(TurnError) as error:
        await prepare.bind_session(
            deps,
            current,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
            deadline=None if default_deadline else _NOW + timedelta(seconds=0.05),
            now=lambda: _NOW,
        )
    assert error.value.kind == "ceiling"
    assert preparation_completed
    assert store.calls == 1 and store.cancelled
    assert len(clocks) == int(default_deadline)


@pytest.mark.parametrize("path,enabled", [("legacy", False), ("legacy", True), ("mux", False)])
async def test_default_and_disabled_preparation_never_wait_on_the_binding_owner(
    monkeypatch,
    completed_preparation,
    path,
    enabled,
):
    store = WaitingStore()
    deps = replace(
        deps_for(None, async_sessionmaker()),
        turn_path=path,
        channel_backends=enabled,
        state_store=store,
    )
    current = replace(admission(), backend_revision=None, origin_channel_id="channel")
    prepared = completed_preparation(current)

    async def completed(*args, **kwargs):
        return prepared

    monkeypatch.setattr(session_preparation, "prepare_session_for_turn", completed)
    result = await prepare.bind_session(
        deps,
        current,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id="thread",
        session_account_id=ACCOUNT,
        reuse_existing=True,
        deadline=_NOW + timedelta(seconds=0.05),
        now=lambda: _NOW,
    )
    assert result is prepared
    assert store.calls == 0 and not store.cancelled
