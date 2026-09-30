"""A DM moved with `/dm` keeps its source channel, and its turns are admitted against it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from daimon.core import direct_messages
from daimon.core.direct_messages import reply_to_dm
from daimon.core.stores.direct_messages import (
    DirectMessageRow,
    get_conversation,
    set_dm_enabled,
    start_conversation,
)
from daimon.core.stores.domain import Role
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_account, make_tenant
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
        await set_dm_enabled(session, tenant_id=tenant.id, enabled=True)
        await start_conversation(
            session,
            conversation=DirectMessageRow(
                platform="discord",
                route_key="dm-chan",
                external_user_id="u1",
                tenant_id=tenant.id,
                account_id=account.id,
                workspace_id="g1",
                channel_id="dm-chan",
                source_channel_id=source_channel_id,
                scope_id="dm:1",
                source_url="https://discord.com/channels/g1/chan-1",
                context="",
                memory_read_only=False,
                history=[],
                recent_message_ids=[],
                active_until=None,
            ),
            now=datetime.now(UTC),
        )
    admitted: list[dict[str, Any]] = []

    async def refuse(deps: TurnDeps, **kwargs: Any) -> None:
        admitted.append(kwargs)
        raise AdmissionDenied(reason="channel_budget_exceeded")

    monkeypatch.setattr(direct_messages, "admit", refuse)
    deps = cast(TurnDeps, type("Deps", (), {"sessionmaker": db_session_factory})())

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
