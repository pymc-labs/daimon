"""Checkpoint authorization reaches the turn boundary without another provider read."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsTextBlock,
)
from daimon.core import workspace_transfer
from daimon.core.config import load_turn_settings
from daimon.core.errors import TurnError
from daimon.core.session_snapshot import SessionSnapshot, snapshot_from_retrieved_session
from daimon.core.turn.state import TurnState
from daimon.core.workspace_ports_compat import restore_transfer_records
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

NOW = datetime(2026, 10, 9, tzinfo=UTC)
TENANT = uuid.UUID(int=1)
ACCOUNT = uuid.UUID(int=2)


def snapshot() -> SessionSnapshot:
    return snapshot_from_retrieved_session(ma_session())


def factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], AsyncMock())


def set_path(monkeypatch: pytest.MonkeyPatch, path: Literal["legacy", "mux"]) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", path)
    assert load_turn_settings().path == path


@pytest.mark.parametrize(
    "path,account_id", [("legacy", ACCOUNT), ("mux", ACCOUNT), ("legacy", None), ("mux", None)]
)
async def test_checkpoint_passes_real_scope_and_preserves_the_existing_read(
    path: Literal["legacy", "mux"],
    account_id: uuid.UUID | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_path(monkeypatch, path)
    turn = AsyncMock(
        return_value=TurnState(error=TurnError(kind="upstream", message="test failure"))
    )
    monkeypatch.setattr(workspace_transfer, "run_turn", turn)
    replay = AsyncMock(wraps=workspace_transfer.replay_events)
    monkeypatch.setattr(workspace_transfer, "replay_events", replay)
    before_send = AsyncMock()
    event = BetaManagedAgentsAgentMessageEvent(
        id="event_work",
        type="agent.message",
        processed_at=NOW,
        content=[BetaManagedAgentsTextBlock(type="text", text="Existing work")],
    )
    old, new = ScriptedTransport(), ScriptedTransport()
    for transport in (old, new):
        transport.queue(
            ScriptedReply(
                "GET",
                "/v1/sessions/sess_old/events",
                httpx.Response(
                    200, json={"data": [event.model_dump(mode="json")], "has_more": False}
                ),
            )
        )
    async with old.client() as legacy, new.client() as client:
        _ = [item async for item in legacy.beta.sessions.events.list(session_id="sess_old")]
        result = await workspace_transfer.transfer_workspace(
            client,
            factory(),
            old_session_id="sess_old",
            old_snapshot=snapshot(),
            tenant_id=TENANT,
            account_id=account_id,
            external_user_id="user",
            transfer_id=uuid.UUID(int=3),
            markup=Decimal("1.2"),
            checkpoint_deadline=NOW + timedelta(seconds=30),
            from_agent_name="source",
            before_send=before_send,
        )
    old.assert_consumed()
    new.assert_consumed()
    assert old.requests == new.requests
    assert len(new.requests) == 1
    assert isinstance(result, workspace_transfer.TranscriptOnly)
    assert "Existing work" in result.transcript
    assert result.gap_reason == "checkpoint_failed"
    turn.assert_awaited_once()
    assert turn.await_args is not None
    scope = turn.await_args.kwargs["scope"]
    assert scope == (
        Scope(
            tenant_id=str(TENANT),
            account_id=str(ACCOUNT),
            principal_id="daimon",
            authorization_id="workspace-checkpoint",
        )
        if account_id is not None
        else None
    )
    assert turn.await_args.kwargs["before_send"] is before_send
    replay.assert_awaited_once()
    assert replay.await_args is not None
    assert replay.await_args.kwargs.get("scope") == scope


async def test_runner_carries_the_authorized_account_to_transfer_and_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transfer = AsyncMock(return_value=workspace_transfer.HistoryOnly("events_unavailable"))
    restore = Mock(wraps=restore_transfer_records)
    monkeypatch.setattr(workspace_transfer, "transfer_workspace", transfer)
    monkeypatch.setattr(workspace_transfer, "restore_transfer_records", restore)
    transport = ScriptedTransport()
    async with transport.client() as client:
        current = snapshot()
        runner = workspace_transfer.WorkspaceTransferRunner(
            anthropic=client,
            sessionmaker=factory(),
            tenant_id=TENANT,
            account_id=ACCOUNT,
            external_user_id="user",
            markup=Decimal("1.2"),
        )
        await runner(
            old_session_id="sess_old",
            old_snapshot=current,
            transfer_id=uuid.UUID(int=3),
            deadline=NOW + timedelta(seconds=30),
            destination_model_id=current.model_id,
            destination_agent_name="successor",
            requested_work=None,
        )
    assert transfer.await_args is not None
    assert transfer.await_args.kwargs["account_id"] == ACCOUNT
    restore.assert_called_once()
    scope = cast(Scope, restore.call_args.kwargs["scope"])
    assert scope.tenant_id == str(TENANT) and scope.account_id == str(ACCOUNT)
    assert not scope.is_platform and not scope.is_legacy_host_authorized
    assert not transport.requests
