"""The operator query validates filters and emits typed measurements."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from daimon.adapters.cli.commands import usage
from daimon.adapters.cli.main import app
from daimon.core.stores.turn_usage import ChannelUsageRow, TurnUsageRow
from typer.testing import CliRunner

pytestmark = pytest.mark.no_cli_local_seed


@pytest.mark.parametrize("summary", [False, True])
def test_usage_command_is_read_only_and_scoped(
    monkeypatch: pytest.MonkeyPatch, summary: bool
) -> None:
    tenant_id = uuid4()
    row = TurnUsageRow(
        id=uuid4(),
        tenant_id=tenant_id,
        account_id=None,
        platform="slack",
        channel_id="channel",
        thread_id="thread",
        origin="routine",
        agent_id="agent",
        session_id="session",
        reason="completed",
        started_at=datetime.now(UTC),
        duration_ms=10,
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=40,
        model_calls=1,
        model_ids=["unknown"],
        cost_usd=None,
        unpriced_calls=1,
        billing_posture="exempt",
    )
    group = ChannelUsageRow(
        platform="slack",
        channel_id="channel",
        origin="routine",
        turns=1,
        measured_turns=1,
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=40,
        model_calls=1,
        cost_usd=None,
        known_cost_usd=Decimal(0),
        unpriced_calls=1,
    )
    query = AsyncMock(return_value=[group] if summary else [row])
    session = object()

    @asynccontextmanager
    async def sessionmaker():
        yield session

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        usage, "load_settings", lambda: SimpleNamespace(database=SimpleNamespace(url="unused"))
    )
    monkeypatch.setattr(usage, "build_engine", lambda url: engine)
    monkeypatch.setattr(usage, "build_session_factory", lambda engine: sessionmaker)
    monkeypatch.setattr(usage, "usage_by_channel" if summary else "list_turn_usage", query)
    args = [
        "usage",
        "turns",
        str(tenant_id),
        "--channel",
        "channel",
        "--origin",
        "routine",
        "--json",
    ]
    if summary:
        args.append("--summary")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["channel_id"] == "channel"
    if summary:
        assert payload[0]["turns"] == 1
    else:
        assert payload[0]["tenant_id"] == str(tenant_id)
    assert payload[0]["cost_usd"] is None
    assert query.await_args is not None
    assert query.await_args.args == (session,)
    assert query.await_args.kwargs["tenant_id"] == tenant_id
    assert query.await_args.kwargs["channel_id"] == "channel"
    assert query.await_args.kwargs["origin"] == "routine"
    engine.dispose.assert_awaited_once()


def test_usage_command_rejects_unknown_origin_before_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden():
        pytest.fail("configuration must not be read for an invalid origin")

    monkeypatch.setattr(usage, "load_settings", forbidden)
    result = CliRunner().invoke(app, ["usage", "turns", str(uuid4()), "--origin", "invalid"])
    assert result.exit_code == 2
    assert "origin must be" in result.output
