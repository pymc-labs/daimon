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
from daimon.core.usage_sweep import BacklogReceipt, BacklogTotal
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
        usage,
        "load_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(url="unused", pool_size=5, max_overflow=10, pool_timeout=30.0)
        ),
    )
    monkeypatch.setattr(usage, "build_engine", lambda url, **kwargs: engine)
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


@pytest.mark.parametrize("apply", [False, True], ids=["dry-run", "apply"])
def test_settle_backlog_is_a_dry_run_unless_applied(
    monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    internal = [uuid4(), uuid4()]
    external = uuid4()
    receipt = BacklogReceipt(
        cutoff=datetime(2026, 10, 12, 12, 0, tzinfo=UTC),
        mode="apply" if apply else "dry_run",
        tenants=[
            BacklogTotal(
                tenant_id=internal[0],
                kind="internal",
                sessions=1,
                calls=2,
                would_debit_usd=Decimal("0.5"),
                debited_usd=Decimal("0.5") if apply else None,
            ),
            BacklogTotal(
                tenant_id=external,
                kind="external",
                sessions=3,
                calls=4,
                would_debit_usd=Decimal("0.25"),
                waived_usd=Decimal("0.25"),
            ),
        ],
    )
    settle = AsyncMock(return_value=receipt)
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        usage,
        "load_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(url="unused", pool_size=5, max_overflow=10, pool_timeout=30.0),
            anthropic=SimpleNamespace(
                api_key=SimpleNamespace(get_secret_value=lambda: "key"),
                base_url="https://api.anthropic.com",
                resolved_api_key=lambda: "key",
            ),
            billing=SimpleNamespace(markup=Decimal("1.1")),
        ),
    )
    monkeypatch.setattr(usage, "build_engine", lambda url, **kwargs: engine)
    monkeypatch.setattr(usage, "build_session_factory", lambda engine: "sessionmaker")
    monkeypatch.setattr(usage, "settle_backlog", settle)
    args = ["usage", "settle-backlog", "--internal", str(internal[0])]
    args += ["--internal", str(internal[1]), "--json"]

    result = CliRunner().invoke(app, [*args, "--apply"] if apply else args)

    assert result.exit_code == 0, result.output
    printed = json.loads(result.output)
    assert printed == {
        "cutoff": "2026-10-12T12:00:00Z",
        "mode": "apply" if apply else "dry_run",
        "tenants": [
            {
                "tenant_id": str(internal[0]),
                "kind": "internal",
                "sessions": 1,
                "calls": 2,
                "cost_usd": "0",
                "would_debit_usd": "0.5",
                "debited_usd": "0.5" if apply else None,
                "waived_usd": None,
            },
            {
                "tenant_id": str(external),
                "kind": "external",
                "sessions": 3,
                "calls": 4,
                "cost_usd": "0",
                "would_debit_usd": "0.25",
                "debited_usd": None,
                "waived_usd": "0.25",
            },
        ],
    }, "one machine-readable receipt"
    assert settle.await_args is not None
    assert settle.await_args.kwargs["internal"] == frozenset(internal)
    assert settle.await_args.kwargs["apply"] is apply
    assert settle.await_args.kwargs["markup"] == Decimal("1.1")
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("as_json", [False, True])
def test_pending_billing_command_exposes_unknown_usage_without_anthropic(monkeypatch, as_json):
    from daimon.core.stores.accounting_outbox import PendingUsagePage, PendingUsageRow

    tenant = uuid4()
    row = PendingUsageRow(
        binding_id="binding",
        session_id="session",
        observation_id="selfhosted:request:request-1",
        revision=1,
        model=None,
        completeness="unknown",
        reason="usage_unknown",
        observed_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    page = PendingUsagePage(rows=[row], next_cursor="next-page", truncated=True)
    query = AsyncMock(return_value=page)
    session = object()

    @asynccontextmanager
    async def sessionmaker():
        yield session

    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        usage,
        "load_settings",
        lambda: SimpleNamespace(
            database=SimpleNamespace(url="unused", pool_size=5, max_overflow=10, pool_timeout=30.0)
        ),
    )
    monkeypatch.setattr(usage, "build_engine", lambda url, **kwargs: engine)
    monkeypatch.setattr(usage, "build_session_factory", lambda engine: sessionmaker)
    monkeypatch.setattr(usage, "list_pending_usage", query)
    args = ["usage", "pending", str(tenant), "--limit", "10", "--cursor", "previous-page"]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    if as_json:
        assert json.loads(result.output) == page.model_dump(mode="json")
    else:
        assert "truncated: true" in result.output
        assert "next_cursor: next-page" in result.output
    query.assert_awaited_once_with(session, tenant_id=tenant, limit=10, cursor="previous-page")
    engine.dispose.assert_awaited_once()
