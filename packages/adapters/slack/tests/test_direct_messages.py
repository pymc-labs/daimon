"""`/dm` and later DM turns answer to the source channel's budget.

Admission, the live role lookup and the DM store are patched on the module.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import direct_messages as dm_module
from daimon.core.stores.domain import Role
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.factories import make_account, make_dm_conversation, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM = "T_DM_BUDGET"
_CHANNEL = "C_SOURCE"


@pytest.mark.parametrize("over_budget", [True, False])
async def test_dm_admits_and_records_the_channel_it_ran_in(
    monkeypatch: pytest.MonkeyPatch, over_budget: bool
) -> None:
    admitted: list[dict[str, Any]] = []

    async def admit(deps: object, **kwargs: Any) -> MagicMock:
        admitted.append(kwargs)
        if over_budget:
            raise AdmissionDenied(reason="channel_budget_exceeded")
        return MagicMock(source_sealed=False)

    client = MagicMock()
    client.conversations_history = AsyncMock(return_value={"messages": []})
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_NEW"}})
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(dm_module, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(dm_module, "sealed_channel_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(dm_module, "admit", admit)
    start_dm = AsyncMock()
    monkeypatch.setattr(dm_module, "start_dm", start_dm)

    await dm_module.handle_dm_command(
        MagicMock(), {"team_id": _TEAM, "user_id": "U1", "channel_id": _CHANNEL, "text": ""}
    )

    (call,) = admitted
    assert (call["is_dm"], call["dm_source_channel_id"]) == (True, _CHANNEL), (
        "admitted against the channel /dm ran in"
    )
    reply = client.chat_postEphemeral.await_args.kwargs["text"]
    if over_budget:
        assert reply.startswith("This channel has used its spending budget."), reply
        client.conversations_open.assert_not_awaited()
        start_dm.assert_not_awaited()
    else:
        assert reply == "Ready in your DMs.", "a channel within its budget opens the DM"
        assert start_dm.await_args.kwargs["source_channel_id"] == _CHANNEL, (
            "the DM records its source channel"
        )


async def test_a_later_dm_turn_over_its_source_budget_tells_the_member(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack")
        account = await make_account(session, tenant=tenant)
        await make_dm_conversation(
            session,
            tenant=tenant,
            account_id=account.id,
            route_key=f"{_TEAM}:D1",
            external_user_id="U1",
            workspace_id=_TEAM,
            source_channel_id=_CHANNEL,
        )
    client = MagicMock()
    client.chat_postMessage = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(
        dm_module,
        "reply_to_dm",
        AsyncMock(side_effect=AdmissionDenied(reason="channel_budget_exceeded")),
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    event = {"type": "message", "channel_type": "im", "channel": "D1", "user": "U1"}

    await dm_module.handle_direct_message(
        runtime, {**event, "ts": "1.1", "text": "hi"}, team_id=_TEAM
    )

    reply = client.chat_postMessage.await_args.kwargs["text"]
    assert reply.startswith("This channel has used its spending budget."), reply
