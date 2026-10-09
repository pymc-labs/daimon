"""The native export/restore bridge preserves each host fallback and first-send input."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from daimon.core import workspace_transfer
from daimon.core.session_snapshot import snapshot_from_retrieved_session
from daimon.core.workspace_transfer import (
    FullHandoff,
    HistoryOnly,
    TranscriptOnly,
    WorkspaceTransferRunner,
    as_prepared_replacement,
)
from daimon.testing.ma_models import ma_session
from daimon.testing.ma_transport import ScriptedTransport
from mux.drivers.anthropic.sessions_lifecycle import AnthropicWorkspaceTransfer

NOW = datetime(2026, 10, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    "outcome",
    [
        FullHandoff(
            "file_bundle", "/daimon-handoff.tar.gz", 12, "participant text", ("left behind",)
        ),
        TranscriptOnly("participant text", "checkpoint_timeout"),
        HistoryOnly("events_unavailable"),
    ],
)
async def test_runner_declares_and_accepts_losses_without_changing_prepared_inputs(
    outcome, monkeypatch
):
    transfer = AsyncMock(return_value=outcome)
    monkeypatch.setattr(workspace_transfer, "transfer_workspace", transfer)
    snapshots = []
    exports, restores = AnthropicWorkspaceTransfer.export, AnthropicWorkspaceTransfer.restore

    def observe_export(self, scope, source, *, inline, key):
        result = exports(self, scope, source, inline=inline, key=key)
        snapshots.append(result)
        return result

    accepted = []

    def observe_restore(self, scope, export, *, inline, accept_losses, key):
        accepted.append(accept_losses)
        return restores(self, scope, export, inline=inline, accept_losses=accept_losses, key=key)

    monkeypatch.setattr(AnthropicWorkspaceTransfer, "export", observe_export)
    monkeypatch.setattr(AnthropicWorkspaceTransfer, "restore", observe_restore)
    snapshot = snapshot_from_retrieved_session(ma_session())
    transport = ScriptedTransport()
    before_send = AsyncMock()
    async with transport.client() as client:
        runner = WorkspaceTransferRunner(
            anthropic=client,
            sessionmaker=AsyncMock(),
            tenant_id=uuid.UUID(int=1),
            external_user_id="user",
            markup=Decimal("1.2"),
            channel_id="channel",
        )
        result = await runner(
            old_session_id="sess_old",
            old_snapshot=snapshot,
            transfer_id=uuid.UUID(int=2),
            deadline=NOW + timedelta(seconds=30),
            destination_model_id=snapshot.model_id,
            destination_agent_name="successor",
            requested_work="continue",
            before_send=before_send,
        )
    assert result == as_prepared_replacement(
        outcome,
        destination_model_id=snapshot.model_id,
        from_agent_name=snapshot.agent_name,
        to_agent_name="successor",
        requested_work="continue",
    )
    transfer.assert_awaited_once()
    assert transfer.await_args.kwargs["before_send"] is before_send
    assert transfer.await_args.kwargs["markup"] == Decimal("1.2")
    assert transfer.await_args.kwargs["channel_id"] == "channel"
    assert snapshots and accepted == [frozenset(snapshots[0].losses)]
    assert all(item == snapshots[0] for item in snapshots)
    assert transport.requests == []
