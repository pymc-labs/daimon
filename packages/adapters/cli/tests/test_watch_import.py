"""Operator import CLI is discoverable and rejects oversized exports before parsing."""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.cli.commands import watches as watch_commands
from daimon.adapters.cli.commands.watches import watches_app
from typer.testing import CliRunner


def test_watch_import_cli_rejects_oversized_manifest(tmp_path):
    export = tmp_path / "watches.json"
    export.write_text("x" * 1048577)
    result = CliRunner().invoke(watches_app, ["import", str(export)])
    assert result.exit_code == 2 and "exceeds 1 MiB" in result.output


def test_watch_confirm_requires_all_binding_fields():
    result = CliRunner().invoke(watches_app, ["confirm", "--help"])
    assert result.exit_code == 0
    assert "OWNER_ACCOUNT_ID" in result.output


@pytest.mark.parametrize("action", ["import", "confirm"])
def test_watch_cli_reports_mapping_errors_and_thread_only_confirmation(
    tmp_path, monkeypatch, action
):
    @asynccontextmanager
    async def begin():
        yield object()

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=SimpleNamespace(begin=begin))

    monkeypatch.setattr(watch_commands, "build_runtime", runtime)
    monkeypatch.setattr(watch_commands, "load_settings", lambda: None)
    identifier = str(uuid.uuid4())
    if action == "import":
        export = tmp_path / "watches.json"
        export.write_text("{}")
        monkeypatch.setattr(
            watch_commands.WatchExport,
            "model_validate_json",
            lambda _: SimpleNamespace(watches=[object()]),
        )
        monkeypatch.setattr(
            watch_commands,
            "import_watch",
            AsyncMock(
                return_value=SimpleNamespace(
                    id=identifier,
                    source_state={
                        "reason": "source_unmapped",
                        "mapping_error": "Unsupported paging arguments",
                    },
                )
            ),
        )
        result = CliRunner().invoke(watches_app, ["import", str(export)])
        assert result.exit_code == 0, result.output
        assert "source_unmapped: Unsupported paging arguments" in result.output
    else:
        monkeypatch.setattr(
            watch_commands,
            "get_watch",
            AsyncMock(
                return_value=SimpleNamespace(
                    source_state={"tool": "read_thread"},
                )
            ),
        )
        confirm = AsyncMock(return_value=SimpleNamespace(id=identifier, status="active"))
        monkeypatch.setattr(watch_commands, "confirm_import", confirm)
        result = CliRunner().invoke(
            watches_app,
            [
                "confirm",
                identifier,
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                "agent",
                "4241",
                "4242",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "require a thread ID" in result.output
        assert "Channel and DM IDs are unsupported" in result.output
        confirm.assert_awaited_once()


@pytest.mark.parametrize("as_json", [False, True])
def test_watch_list_shows_one_tenant_without_instructions_or_evidence(monkeypatch, as_json):
    from datetime import UTC, datetime, timedelta

    from daimon.core.stores.platform_names import KnownName
    from daimon.core.stores.watches import WatchActivity

    @asynccontextmanager
    async def session():
        yield object()

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=session)

    tenant = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        owner_external_id="456",
        platform="discord",
        parent_channel_id="11",
        thread_id="12",
        agent_id="agent",
        instructions="SECRET INSTRUCTIONS",
        baseline={"value": "EVIDENCE"},
        status="paused",
        source="poll",
        source_state={"tool": "read_channel", "arguments": {"channel_id": "SRC"}, "reason": "x"},
        expires_at=datetime.now(UTC) + timedelta(days=1),
        consecutive_errors=2,
        backoff_until=None,
    )
    listing = AsyncMock(return_value=[WatchActivity(row, 3, None)])  # type: ignore[arg-type]
    monkeypatch.setattr(watch_commands, "build_runtime", runtime)
    monkeypatch.setattr(watch_commands, "load_settings", lambda: None)
    monkeypatch.setattr(watch_commands, "get_tenant", AsyncMock(return_value=object()))
    monkeypatch.setattr(watch_commands, "list_tenant_watches", listing)
    monkeypatch.setattr(
        watch_commands, "get_user_names", AsyncMock(return_value={"456": KnownName("Ada")})
    )
    args = ["list", "--tenant", str(tenant), *(["--json"] if as_json else [])]
    result = CliRunner().invoke(watches_app, args)
    assert result.exit_code == 0, result.output
    assert listing.await_args.kwargs["tenant_id"] == tenant
    assert str(row.id) in result.output and "Ada" in result.output and "456" in result.output
    assert "paused" in result.output and "read_channel" in result.output
    for secret in ("SECRET", "EVIDENCE", "SRC"):
        assert secret not in result.output


def test_watch_list_refuses_unknown_tenant(monkeypatch):
    @asynccontextmanager
    async def session():
        yield object()

    @asynccontextmanager
    async def runtime(_settings):
        yield SimpleNamespace(sessionmaker=session)

    monkeypatch.setattr(watch_commands, "build_runtime", runtime)
    monkeypatch.setattr(watch_commands, "load_settings", lambda: None)
    monkeypatch.setattr(watch_commands, "get_tenant", AsyncMock(return_value=None))
    result = CliRunner().invoke(watches_app, ["list", "--tenant", str(uuid.uuid4())])
    assert result.exit_code == 2 and "No such tenant" in result.output


def _runtime() -> Callable[[object], AbstractAsyncContextManager[SimpleNamespace]]:
    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        yield object()

    @asynccontextmanager
    async def runtime(_settings: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(sessionmaker=session)

    return runtime


@pytest.mark.parametrize("as_json", [False, True])
def test_watch_notices_counts_distinct_messages_sent_without_notice_text(
    monkeypatch: pytest.MonkeyPatch, as_json: bool
) -> None:
    import json
    from datetime import UTC, datetime

    from daimon.core.stores.watches import NoticeReceipt

    watch, owner, now = uuid.uuid4(), uuid.uuid4(), datetime.now(UTC)

    def receipt(attempt: int, outcome: str, message: str | None) -> NoticeReceipt:
        settled = None if outcome == "interrupted" else now
        return NoticeReceipt(
            watch, owner, "discord", "expired", 1, attempt, outcome, now, settled, "900", message
        )

    # The resumed attempt found the interrupted one's message: still one message.
    rows = [receipt(1, "sent", "901"), receipt(2, "sent", "901"), receipt(3, "failed", None)]
    listing = AsyncMock(return_value=rows)
    monkeypatch.setattr(watch_commands, "build_runtime", _runtime())
    monkeypatch.setattr(watch_commands, "load_settings", lambda: None)
    monkeypatch.setattr(watch_commands, "list_notice_receipts", listing)
    args = ["notices", "--watch", str(watch), *(["--json"] if as_json else [])]
    result = CliRunner().invoke(watches_app, args)
    assert result.exit_code == 0, result.output
    assert listing.await_args is not None
    assert listing.await_args.kwargs["watch_id"] == watch
    if as_json:
        report = json.loads(result.output)
        assert report["sent"] == {"expired": 1}
        assert [r["outcome"] for r in report["receipts"]] == ["sent", "sent", "failed"]
        assert report["receipts"][0]["message_id"] == "901"
    else:
        assert "sent: expired=1" in result.output
        assert result.output.count("outcome=") == 3


def test_watch_notices_with_no_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch_commands, "build_runtime", _runtime())
    monkeypatch.setattr(watch_commands, "load_settings", lambda: None)
    monkeypatch.setattr(watch_commands, "list_notice_receipts", AsyncMock(return_value=[]))
    result = CliRunner().invoke(watches_app, ["notices", "--watch", str(uuid.uuid4())])
    assert result.exit_code == 0 and "No notice receipts." in result.output
    result = CliRunner().invoke(watches_app, ["notices", "--watch", str(uuid.uuid4()), "--json"])
    assert result.exit_code == 0 and '"sent": {}' in result.output
