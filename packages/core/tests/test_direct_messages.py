"""A DM moved with `/dm-me` keeps its source channel, and its turns are admitted against it."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from daimon.core import direct_messages
from daimon.core.direct_messages import reply_to_dm
from daimon.core.errors import UserFacingError
from daimon.core.stores.direct_messages import get_conversation
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import set_provision_status
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
    assert stored.active_until is None, "a refused turn releases the conversation"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("pending", "still setting up this server"), ("failed", "isn't available")],
)
async def test_a_dm_in_a_server_still_being_set_up_says_to_try_again(
    db_session_factory: async_sessionmaker[AsyncSession], status: str, expected: str
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(session, tenant=tenant, account_id=account.id)
    await set_provision_status(db_session_factory, tenant_id=tenant.id, status=status)
    deps = replace(deps_for(None, db_session_factory), turn_path=None)

    with pytest.raises(UserFacingError, match=expected):
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


@pytest.mark.parametrize("owns_claim", [True, False])
@pytest.mark.parametrize("interrupt_ack", [True, False])
async def test_shutdown_settle_uses_owned_scope_and_cannot_interrupt_a_released_dm(
    db_session_factory, monkeypatch, owns_claim, interrupt_ack
):
    from datetime import UTC, datetime, timedelta

    from daimon.core.stores.direct_messages import claim_message, finish_message
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
            platform=original.platform,
            route_key=original.route_key,
            external_user_id=original.external_user_id,
            message_id="shutdown-test",
            expected_scope_id=original.scope_id,
            now=now,
            active_until=now + timedelta(minutes=10),
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
        assert scope.authorization_id == "dm-shutdown-drain" and timeout_s == 3.0
        calls.append(session_id)
        return interrupt_ack

    monkeypatch.setattr(direct_messages, "interrupt_orphaned_session", interrupt)
    deps = replace(deps_for(client, db_session_factory), turn_path=None)
    await direct_messages._settle_shutdown_cancelled_dm(deps, claimed)
    assert calls == (["owned-dm-session"] if owns_claim else [])
    async with db_session_factory() as session:
        stored = await get_thread_session_by_id(session, id=mapping.id)
    assert stored is not None
    assert (stored.status == "dead") is (owns_claim and not interrupt_ack)
