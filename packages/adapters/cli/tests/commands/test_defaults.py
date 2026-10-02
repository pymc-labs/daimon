import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import defaults as cmd_mod
from daimon.adapters.cli.commands.defaults import _format_report_json, _format_report_table
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.defaults.provisioning import archive_tenant
from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.tenants import set_provision_status
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner


def test_format_report_json_emits_kind_bucket_lists() -> None:
    report = ApplyReport()
    report.add(
        ResourceOutcome(kind="agent", name="coder", action=Action.CREATED, anthropic_id="ag_1")
    )
    report.add(ResourceOutcome(kind="environment", name="default", action=Action.SKIPPED))
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False)
    _format_report_json(console, report)
    parsed = json.loads(buf.getvalue())
    assert parsed["agents"][0]["name"] == "coder"
    assert parsed["environments"][0]["action"] == "skipped"
    assert "refusal" not in parsed["agents"][0], "an unset refusal leaves the JSON unchanged"


def test_format_report_table_shows_summary_footer() -> None:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="agent", name="x", action=Action.CREATED))
    report.add(ResourceOutcome(kind="agent", name="y", action=Action.FAILED, error="oops"))
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)
    _format_report_table(console, report)
    out = buf.getvalue()
    assert "created" in out and "failed" in out and "x" in out and "y" in out


def test_format_report_json_includes_system_config_bucket() -> None:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="system_config", name="system", action=Action.UPDATED))
    buf = StringIO()
    console = Console(file=buf, highlight=False, force_terminal=False, width=200)
    _format_report_json(console, report)
    payload = json.loads(buf.getvalue())
    assert payload["system_config"] == [
        {
            "kind": "system_config",
            "name": "system",
            "action": "updated",
            "anthropic_id": None,
            "error": None,
        }
    ]


def test_format_report_table_renders_system_config_rows() -> None:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="system_config", name="system", action=Action.UPDATED))
    buf = StringIO()
    console = Console(file=buf, highlight=False, force_terminal=False, width=200)
    _format_report_table(console, report)
    out = buf.getvalue()
    assert "system_config" in out
    assert "system" in out
    assert "updated" in out


# ---------------------------------------------------------------------------
# `defaults verify` — walks every ready tenant, fails on divergence (behavioral,
# via CliRunner + a real per-schema test database)
# ---------------------------------------------------------------------------


class _McpSettings:
    public_url = None


class _FakeSettings:
    mcp = _McpSettings()


def _install_defaults_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", cast(Settings, _FakeSettings()))
    object.__setattr__(rt, "anthropic", build_stub_anthropic())
    object.__setattr__(rt, "sessionmaker", sessionmaker)
    object.__setattr__(rt, "deployment_default", DeploymentDefault())
    object.__setattr__(rt, "resolver_cache", new_resolver_cache())

    @asynccontextmanager
    async def fake_build_runtime(_settings: Settings) -> AsyncIterator[CliRuntime]:
        yield rt

    monkeypatch.setattr(cmd_mod, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(cmd_mod, "load_settings", lambda: cast(Settings, _FakeSettings()))


def _install_verify_tenant_defaults(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reports: dict[uuid.UUID, ApplyReport | Exception],
    calls: list[uuid.UUID],
) -> None:
    """Fakes `verify_tenant_defaults` at the command module's import site.

    The dry-run reconcile itself (real MA transport, real fingerprint math) is
    covered by `packages/core/tests/defaults/test_provisioning.py`; this module
    tests the CLI's per-tenant walk/aggregation/exit-code behavior, so faking
    the core entry point (like `smoke.py`'s tests fake `run_smoke_check`) keeps
    these tests about the command, not about MA transport plumbing.
    """

    async def fake_verify_tenant_defaults(
        client: Any,
        session_factory: Any,
        defaults_root: Path,
        *,
        tenant_id: uuid.UUID,
        public_url: str | None = None,
    ) -> ApplyReport:
        calls.append(tenant_id)
        outcome = reports[tenant_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cmd_mod, "verify_tenant_defaults", fake_verify_tenant_defaults)


def _in_sync_report() -> ApplyReport:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="agent", name="daimon", action=Action.SKIPPED))
    return report


def _diverged_report(*, name: str = "daimon") -> ApplyReport:
    report = ApplyReport()
    report.add(ResourceOutcome(kind="agent", name=name, action=Action.CREATED))
    return report


def _unverifiable_report() -> ApplyReport:
    report = ApplyReport()
    report.add(
        ResourceOutcome(kind="skill", name="<sweep>", action=Action.FAILED, error="upstream outage")
    )
    return report


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(main_mod.app, ["defaults", "verify", *args])


def test_defaults_verify_two_ready_tenants_in_sync_exits_0_and_prints_both(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with schema_sessionmaker() as s, s.begin():
            a = await make_tenant(s, platform="discord", workspace_id="guild-a")
            b = await make_tenant(s, platform="slack", workspace_id="team-b")
            return a.id, b.id

    tenant_a, tenant_b = asyncio.run(seed())

    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(
        monkeypatch,
        reports={tenant_a: _in_sync_report(), tenant_b: _in_sync_report()},
        calls=calls,
    )

    result = _invoke()

    assert result.exit_code == 0, result.stdout
    assert "discord:guild-a" in result.stdout
    assert "slack:team-b" in result.stdout
    assert set(calls) == {tenant_a, tenant_b}


def test_defaults_verify_diverged_tenant_exits_nonzero_and_names_resource(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            t = await make_tenant(s, platform="discord", workspace_id="guild-drift")
            return t.id

    tenant_id = asyncio.run(seed())

    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(
        monkeypatch, reports={tenant_id: _diverged_report(name="daimon")}, calls=calls
    )

    result = _invoke()

    assert result.exit_code != 0, result.stdout
    assert "daimon" in result.stdout, (
        f"expected the diverged resource name in the output, got: {result.stdout!r}"
    )


def test_defaults_verify_pending_tenant_counted_as_awaiting_reseed_not_nonzero(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            t = await make_tenant(s, platform="discord", workspace_id="guild-pending")
            return t.id

    tenant_id = asyncio.run(seed())
    asyncio.run(set_provision_status(schema_sessionmaker, tenant_id=tenant_id, status="pending"))

    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(monkeypatch, reports={}, calls=calls)

    result = _invoke()

    assert result.exit_code == 0, result.stdout
    assert calls == [], "a pending tenant must never reach verify_tenant_defaults"
    assert "1" in result.stdout and "re-seed" in result.stdout, (
        f"expected the awaiting-re-seed count in the output, got: {result.stdout!r}"
    )


def test_defaults_verify_archived_tenant_not_visited(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            t = await make_tenant(s, platform="discord", workspace_id="guild-archived")
            return t.id

    tenant_id = asyncio.run(seed())
    asyncio.run(
        archive_tenant(schema_sessionmaker, tenant_id=tenant_id, now=dt.datetime.now(dt.UTC))
    )

    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(monkeypatch, reports={}, calls=calls)

    result = _invoke()

    assert result.exit_code == 0, result.stdout
    assert calls == [], "an archived tenant must make zero provider requests"


def test_defaults_verify_cli_local_tenant_not_visited(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `cli:local` row every deployment bootstraps via `defaults apply` is
    reconciled with account_id=None, but `verify_tenant_defaults` always derives
    a non-None account id — so its fingerprint can never match and it must be
    excluded from the walk entirely, not evaluated and reported as diverged.
    """

    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            t = await make_tenant(s, platform="cli", workspace_id="local")
            return t.id

    asyncio.run(seed())

    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(monkeypatch, reports={}, calls=calls)

    result = _invoke()

    assert result.exit_code == 0, result.stdout
    assert calls == [], "cli:local must never reach verify_tenant_defaults"


def test_defaults_verify_provider_read_failure_exits_nonzero(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            t = await make_tenant(s, platform="discord", workspace_id="guild-unverifiable")
            return t.id

    tenant_id = asyncio.run(seed())

    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(
        monkeypatch, reports={tenant_id: _unverifiable_report()}, calls=calls
    )

    result = _invoke()

    assert result.exit_code != 0, result.stdout


def test_defaults_verify_json_output_parses_and_carries_classifications(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with schema_sessionmaker() as s, s.begin():
            a = await make_tenant(s, platform="discord", workspace_id="guild-json-sync")
            b = await make_tenant(s, platform="discord", workspace_id="guild-json-drift")
            return a.id, b.id

    tenant_a, tenant_b = asyncio.run(seed())

    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(
        monkeypatch,
        reports={tenant_a: _in_sync_report(), tenant_b: _diverged_report(name="coder")},
        calls=calls,
    )

    result = _invoke("--json")

    assert result.exit_code != 0, result.stdout
    payload = json.loads(result.stdout)
    statuses = {t["external_id"]: t["status"] for t in payload["tenants"]}
    assert statuses["guild-json-sync"] == "in_sync"
    assert statuses["guild-json-drift"] == "diverged"
    diverged_entry = next(t for t in payload["tenants"] if t["external_id"] == "guild-json-drift")
    assert diverged_entry["changed"] == [{"kind": "agent", "name": "coder"}]
