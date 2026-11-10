import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import typer
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import defaults as cmd_mod
from daimon.adapters.cli.commands.defaults import (
    _format_report_json,
    _format_report_table,
    defaults_apply,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
from daimon.core.defaults.provisioning import archive_tenant
from daimon.core.defaults.report import Action, ApplyReport, ResourceOutcome
from daimon.core.errors import DefaultsError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Platform
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.core.stores.tenants import set_provision_status
from daimon.testing.factories import make_routine, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec
from mux.profiles.selfhosted import LOOP
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    build_fake_anthropic,
    build_stub_anthropic,
    combine_handlers,
    list_response,
    make_archive_agent_handler,
    make_fake_ma_handler,
)
from rich.console import Console
from sqlalchemy import text
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


class _NoAnthropicSettings:
    """Just enough shape for `defaults_apply`'s OpenAI-only skip branch."""

    class anthropic:  # mirrors the real nested-settings shape
        api_key = None


def test_defaults_apply_when_no_key_then_skips_anthropic_and_renames_selfhosted(
    tmp_path: Path, schema_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "default.yaml").write_text(
        "name: default\nmodel: claude-sonnet-4-6\nsystem: You are Daimon.\n"
    )

    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="discord", workspace_id="guild-sh")
            await make_routine(s, tenant=tenant, agent_name="daimon")
        await SelfHostedAgents(schema_sessionmaker).create(
            Scope(
                tenant_id=str(tenant.id),
                account_id="operator",
                principal_id="cli",
                authorization_id="cli-deployment-operator",
            ),
            AgentSpec(name="daimon", model=ModelRef(provider=LOOP.provider, id="gpt-6.1-sol")),
            key="seed",
        )
        return tenant.id

    tenant_id = asyncio.run(seed())
    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", cast(Settings, _NoAnthropicSettings()))
    object.__setattr__(rt, "sessionmaker", schema_sessionmaker)
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=120)

    asyncio.run(
        defaults_apply(rt=rt, console=console, dry_run=False, as_json=False, defaults_root=tmp_path)
    )

    out = buf.getvalue()
    assert "no DAIMON_ANTHROPIC__API_KEY configured" in out, out
    assert "renamed" in out, f"the report lists the self-hosted rename: {out}"

    async def names() -> list[str]:
        async with schema_sessionmaker() as s:
            rows = await s.execute(
                text(
                    "SELECT name FROM selfhosted_agents WHERE tenant_id = :t"
                    " UNION ALL SELECT agent_name FROM routines WHERE tenant_id = :t"
                ),
                {"t": tenant_id},
            )
            return list(rows.scalars())

    assert asyncio.run(names()) == ["default", "default"], (
        "the record and its references move without Anthropic credentials"
    )


class _KeyedSettings:
    """Just enough shape for `defaults_apply` when an Anthropic key is configured."""

    class anthropic:  # mirrors the real nested-settings shape
        api_key = "sk-test"

    class mcp:
        public_url = None


_CLI_LOCAL = derive_tenant_uuid(platform="cli", workspace_id="local")


def _keyed_runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    state: FakeMAState,
    *,
    reject_models: bool = False,
) -> CliRuntime:
    agents = combine_handlers(make_archive_agent_handler(), make_fake_ma_handler(state))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/environments":
            return list_response([])
        if reject_models and request.method == "POST" and request.url.path == "/v1/agents":
            message = "model.id: claude-sonnet-4-6 is not supported"
            return httpx.Response(400, json={"type": "error", "error": {"message": message}})
        return agents(request)

    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", cast(Settings, _KeyedSettings()))
    object.__setattr__(rt, "anthropic", build_fake_anthropic(handler))
    object.__setattr__(rt, "sessionmaker", sessionmaker)
    return rt


def _default_tree(root: Path) -> Path:
    (root / "agents").mkdir()
    (root / "agents" / "default.yaml").write_text(
        "name: default\nmodel: claude-sonnet-4-6\nsystem: You are Daimon.\n"
    )
    return root


async def _seed_legacy_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    platform: Platform,
    workspace_id: str,
    *names: str,
) -> uuid.UUID:
    """A tenant with a routine on `daimon` and live self-hosted records `names`."""
    async with sessionmaker() as s, s.begin():
        tenant = await make_tenant(s, platform=platform, workspace_id=workspace_id)
        await make_routine(s, tenant=tenant, agent_name="daimon")
    agents = SelfHostedAgents(sessionmaker)
    for name in names:
        await agents.create(
            Scope(
                tenant_id=str(tenant.id),
                account_id="operator",
                principal_id="cli",
                authorization_id="cli-deployment-operator",
            ),
            AgentSpec(name=name, model=ModelRef(provider=LOOP.provider, id="gpt-6.1-sol")),
            key=f"{workspace_id}-{name}",
        )
    return tenant.id


async def _agent_outcomes(
    rt: CliRuntime, root: Path, *, dry_run: bool, expect_exit: bool = False
) -> list[dict[str, str]]:
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, highlight=False, width=200)
    call = defaults_apply(rt=rt, console=console, dry_run=dry_run, as_json=True, defaults_root=root)
    if expect_exit:
        with pytest.raises(typer.Exit):
            await call
    else:
        await call
    return json.loads(buf.getvalue())["agents"]


def test_defaults_apply_when_cli_local_rename_blocked_then_reports_one_failure(
    tmp_path: Path, schema_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async def run() -> list[dict[str, str]]:
        await _seed_legacy_tenant(schema_sessionmaker, "cli", "local", "daimon", "default")
        state = FakeMAState()
        agent = ma_agent(
            id="ag_daimon",
            name="daimon",
            tenant_id=_CLI_LOCAL,
            metadata={MA_METADATA_KEY_MANAGED: "true"},
        )
        state.agents["ag_daimon"] = agent.model_dump(mode="json")
        rt = _keyed_runtime(schema_sessionmaker, state)
        return await _agent_outcomes(rt, _default_tree(tmp_path), dry_run=False, expect_exit=True)

    outcomes = asyncio.run(run())

    failed = [o for o in outcomes if o["action"] == "failed"]
    assert len(failed) == 1, (
        f"cli:local is renamed once, by apply, not again by the pre-pass: {outcomes}"
    )


def test_defaults_apply_when_dry_run_then_each_tenant_reports_one_rename(
    tmp_path: Path, schema_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async def run() -> list[dict[str, str]]:
        await _seed_legacy_tenant(schema_sessionmaker, "cli", "local", "daimon")
        await _seed_legacy_tenant(schema_sessionmaker, "discord", "guild-sh", "daimon")
        rt = _keyed_runtime(schema_sessionmaker, FakeMAState())
        return await _agent_outcomes(rt, _default_tree(tmp_path), dry_run=True)

    outcomes = asyncio.run(run())

    renamed = [o for o in outcomes if o["action"] == "renamed"]
    assert len(renamed) == 2, f"one rename for cli:local and one for the other tenant: {outcomes}"


def test_defaults_apply_when_preflight_fails_then_selfhosted_records_are_untouched(
    tmp_path: Path, schema_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async def run() -> list[str]:
        await _seed_legacy_tenant(schema_sessionmaker, "cli", "local", "daimon")
        other = await _seed_legacy_tenant(schema_sessionmaker, "discord", "guild-sh", "daimon")
        rt = _keyed_runtime(schema_sessionmaker, FakeMAState(), reject_models=True)
        with pytest.raises(DefaultsError, match="pre-flight failed"):
            await defaults_apply(
                rt=rt,
                console=Console(file=StringIO()),
                dry_run=False,
                as_json=False,
                defaults_root=_default_tree(tmp_path),
            )
        async with schema_sessionmaker() as s:
            rows = await s.execute(
                text(
                    "SELECT name FROM selfhosted_agents UNION ALL"
                    " SELECT agent_name FROM routines ORDER BY 1"
                )
            )
            assert other, "the second tenant exists"
            return list(rows.scalars())

    assert asyncio.run(run()) == ["daimon"] * 4, "no record or reference moved before the abort"


def test_defaults_apply_when_agent_names_unknown_skill_then_selfhosted_records_are_untouched(
    tmp_path: Path, schema_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    root = _default_tree(tmp_path)
    (root / "agents" / "default.yaml").write_text(
        "name: default\nmodel: claude-sonnet-4-6\nsystem: You are Daimon.\n"
        "skills:\n  - type: custom\n    skill_id: missing\n"
    )

    async def run() -> list[str]:
        await _seed_legacy_tenant(schema_sessionmaker, "discord", "guild-sh", "daimon")
        rt = object.__new__(CliRuntime)
        object.__setattr__(rt, "settings", cast(Settings, _NoAnthropicSettings()))
        object.__setattr__(rt, "sessionmaker", schema_sessionmaker)
        with pytest.raises(DefaultsError, match="unknown skill"):
            await defaults_apply(
                rt=rt,
                console=Console(file=StringIO()),
                dry_run=False,
                as_json=False,
                defaults_root=root,
            )
        async with schema_sessionmaker() as s:
            rows = await s.execute(
                text("SELECT name FROM selfhosted_agents UNION ALL SELECT agent_name FROM routines")
            )
            return list(rows.scalars())

    assert asyncio.run(run()) == ["daimon", "daimon"], "a bad tree fails before any rename"


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


def test_defaults_verify_designated_qa_drift_warns_but_checks_all_tenants(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with schema_sessionmaker() as s, s.begin():
            qa = await make_tenant(s, platform="discord", workspace_id="qa-guild")
            other = await make_tenant(s, platform="discord", workspace_id="other-guild")
            return qa.id, other.id

    qa_id, other_id = asyncio.run(seed())
    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(
        monkeypatch,
        reports={qa_id: _diverged_report(name="qa-library"), other_id: _in_sync_report()},
        calls=calls,
    )

    result = _invoke("--warn-tenant", "discord:qa-guild", "--json")

    assert result.exit_code == 0, result.stdout
    assert set(calls) == {qa_id, other_id}
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    qa = next(t for t in payload["tenants"] if t["external_id"] == "qa-guild")
    assert qa["status"] == "diverged"
    assert qa["warning"] is True
    assert qa["changed"] == [{"kind": "agent", "name": "qa-library"}]


@pytest.mark.parametrize("other_report", [_diverged_report(), _unverifiable_report()])
def test_defaults_verify_designated_qa_does_not_hide_other_failures(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    other_report: ApplyReport,
) -> None:
    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with schema_sessionmaker() as s, s.begin():
            qa = await make_tenant(s, platform="discord", workspace_id="qa-guild")
            other = await make_tenant(s, platform="discord", workspace_id="other-guild")
            return qa.id, other.id

    qa_id, other_id = asyncio.run(seed())
    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(
        monkeypatch,
        reports={qa_id: _diverged_report(), other_id: other_report},
        calls=calls,
    )

    result = _invoke("--warn-tenant", "discord:qa-guild", "--json")

    assert result.exit_code == 1, result.stdout
    assert set(calls) == {qa_id, other_id}
    assert json.loads(result.stdout)["ok"] is False


def test_defaults_verify_designated_qa_unverifiable_warns(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            qa = await make_tenant(s, platform="discord", workspace_id="qa-guild")
            return qa.id

    qa_id = asyncio.run(seed())
    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(
        monkeypatch, reports={qa_id: _unverifiable_report()}, calls=calls
    )

    result = _invoke("--warn-tenant", "discord:qa-guild", "--json")

    assert result.exit_code == 0, result.stdout
    assert calls == [qa_id]
    assert json.loads(result.stdout)["tenants"][0]["warning"] is True


@pytest.mark.parametrize("selector", ["qa-guild", "discord:missing-guild", "discord:"])
def test_defaults_verify_invalid_qa_selector_fails_before_provider_reads(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            qa = await make_tenant(s, platform="discord", workspace_id="qa-guild")
            return qa.id

    qa_id = asyncio.run(seed())
    _install_defaults_runtime(monkeypatch, sessionmaker=schema_sessionmaker)
    calls: list[uuid.UUID] = []
    _install_verify_tenant_defaults(monkeypatch, reports={qa_id: _in_sync_report()}, calls=calls)

    result = _invoke("--warn-tenant", selector)

    assert result.exit_code == 2, result.stdout
    assert "Invalid --warn-tenant" in result.stdout
    assert calls == []


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
