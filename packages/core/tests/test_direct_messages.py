"""A DM moved with `/dm-me` keeps its source channel, and its turns are admitted against it."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.core import direct_messages
from daimon.core.direct_messages import reply_to_dm
from daimon.core.stores.direct_messages import OwnerDmFence, get_conversation
from daimon.core.stores.domain import Role
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from daimon.testing.turn_fakes import deps_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("source_channel_id", ["chan-1", None], ids=["moved", "before-sources"])
async def test_a_dm_turn_is_admitted_against_its_source_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    source_channel_id: str | None,
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session, tenant=tenant, account_id=account.id, source_channel_id=source_channel_id
        )
    admitted: list[dict[str, Any]] = []

    async def refuse(deps: TurnDeps, **kwargs: Any) -> None:
        admitted.append(kwargs)
        raise AdmissionDenied(reason="channel_budget_exceeded")

    monkeypatch.setattr(direct_messages, "admit", refuse)
    deps = replace(deps_for(None, db_session_factory), turn_path=None)

    with pytest.raises(AdmissionDenied):
        await reply_to_dm(
            deps,
            platform="discord",
            route_key="dm-chan",
            external_user_id="u1",
            message_id="m1",
            expected_scope_id="dm:1",
            text="hello",
            role=Role.USER,
        )

    (call,) = admitted
    assert (call["channel_id"], call["is_dm"]) == ("dm-chan", True), "admitted as a DM"
    assert call["dm_source_channel_id"] == source_channel_id, "against the channel it came from"
    async with db_session_factory() as session:
        stored = await get_conversation(
            session, platform="discord", route_key="dm-chan", external_user_id="u1"
        )
    assert stored is not None and stored.source_channel_id == source_channel_id, "it round-trips"
    assert stored.claimed_at is None, "a refused turn releases the conversation"


@pytest.mark.parametrize("owns_claim", [True, False])
@pytest.mark.parametrize("interrupt_ack", [True, False])
async def test_shutdown_settle_uses_owned_scope_and_cannot_interrupt_a_released_dm(
    db_session_factory, monkeypatch, owns_claim, interrupt_ack
):
    from datetime import UTC, datetime, timedelta

    from daimon.core.stores.direct_messages import (
        OwnerDmFence,
        claim_message,
        finish_message,
    )
    from daimon.core.stores.thread_sessions import get_thread_session_by_id
    from daimon.testing.factories import make_thread_session

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        original = await make_dm_conversation(session, tenant=tenant, account_id=account.id)
        mapping = await make_thread_session(
            session,
            tenant=tenant,
            account=account,
            thread_id=original.scope_id,
            ma_session_id="owned-dm-session",
        )
        now = datetime.now(UTC)
        claimed = await claim_message(
            session,
            fence=OwnerDmFence(original.platform, original.external_user_id),
            route_key=original.route_key,
            message_id="shutdown-test",
            expected_scope_id=original.scope_id,
            claimed_at=now + timedelta(minutes=10),
        )
        assert claimed is not None
        if not owns_claim:
            await finish_message(session, conversation=claimed, history=None)

    client = object()
    calls = []

    async def interrupt(anthropic, *, session_id, scope, timeout_s):
        assert anthropic is client and session_id == "owned-dm-session"
        assert scope.tenant_id == str(tenant.id) and scope.account_id == str(account.id)
        assert not scope.is_platform and not scope.is_legacy_host_authorized
        assert scope.authorization_id == "dm-cancelled-turn" and timeout_s == 3.0
        calls.append(session_id)
        return interrupt_ack

    monkeypatch.setattr(direct_messages, "interrupt_orphaned_session", interrupt)
    deps = replace(deps_for(client, db_session_factory), turn_path=None)
    await direct_messages._settle_cancelled_dm(deps, claimed)
    assert calls == (["owned-dm-session"] if owns_claim else [])
    async with db_session_factory() as session:
        stored = await get_thread_session_by_id(session, id=mapping.id)
    assert stored is not None
    assert (stored.status == "dead") is (owns_claim and not interrupt_ack)


class _FencePing:
    """The fence connection's session: counts pings, or fails them."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.pings = error, 0

    async def execute(self, statement: object) -> None:
        if self.error is not None:
            raise self.error
        assert str(statement) == "SELECT 1", "the ping is a plain query"
        self.pings += 1


async def _watch(monkeypatch: pytest.MonkeyPatch, db: _FencePing, held: list[bool]) -> OwnerDmFence:
    @asynccontextmanager
    async def session():
        yield None

    async def fence_held(_session: object, _fence: object) -> bool:
        return held.pop(0)

    monkeypatch.setattr(direct_messages, "_FENCE_CHECK_S", 0.01)
    monkeypatch.setattr(direct_messages, "fence_held", fence_held)
    fence = OwnerDmFence("discord", "42", 1, db)  # type: ignore[arg-type]
    async with asyncio.timeout(None) as stop:
        await asyncio.wait_for(
            direct_messages._watch_fence(SimpleNamespace(sessionmaker=session), fence, stop),  # type: ignore[arg-type]
            2,
        )
    return fence


async def test_the_fence_watcher_pings_the_fence_connection_each_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _FencePing()
    fence = await _watch(monkeypatch, db, [True, True, False])
    assert db.pings == 3, "each poll sends a query over the idle fence connection"
    assert fence.lost.is_set(), "the lock check still ends the watch once the fence is gone"


async def test_the_fence_watcher_treats_a_dead_fence_connection_as_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fence = await _watch(monkeypatch, _FencePing(OSError("connection closed")), [True])
    assert fence.lost.is_set(), "a failed ping on the fence connection means the hold is gone"
