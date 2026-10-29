"""Operator import CLI is discoverable and rejects oversized exports before parsing."""

import uuid
from contextlib import asynccontextmanager
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
