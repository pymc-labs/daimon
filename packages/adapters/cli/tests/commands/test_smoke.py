"""`daimon smoke` Typer command."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, cast

import anthropic
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import smoke as cmd_mod
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import Settings
from daimon.core.ma_resolver import ResolverCache
from daimon.core.scope import DeploymentDefault
from daimon.core.smoke import SmokeCheckError, SmokeResult
from daimon.core.tool_safety import ToolSafetyPolicy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner


class _McpSettings:
    public_url = None


class _BillingSettings:
    markup = Decimal("2.0")


class _FakeSettings:
    mcp = _McpSettings()
    billing = _BillingSettings()
    tool_safety = ToolSafetyPolicy(enabled=True)


def _make_rt(
    *, agent_name: str | None = "daimon", environment_name: str | None = "default"
) -> CliRuntime:
    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", cast(Settings, _FakeSettings()))
    object.__setattr__(rt, "anthropic", cast(AsyncAnthropic, object()))
    object.__setattr__(rt, "sessionmaker", cast(async_sessionmaker[AsyncSession], object()))
    object.__setattr__(
        rt,
        "deployment_default",
        DeploymentDefault(agent_name=agent_name, environment_name=environment_name),
    )
    object.__setattr__(rt, "resolver_cache", cast(ResolverCache, object()))
    return rt


def _smoke_result() -> SmokeResult:
    return SmokeResult(
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        agent_id="agent_abc",
        environment_id="env_abc",
        topped_up=False,
        usage_event_count=1,
        tail="done",
    )


def _install_runtime(monkeypatch: pytest.MonkeyPatch, *, rt: CliRuntime) -> None:
    @asynccontextmanager
    async def fake_build_runtime(_settings: Settings) -> AsyncIterator[CliRuntime]:
        yield rt

    monkeypatch.setattr(cmd_mod, "load_settings", lambda: cast(Settings, _FakeSettings()))
    monkeypatch.setattr(cmd_mod, "build_runtime", fake_build_runtime)


def _install_run_smoke_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    captured: dict[str, Any],
    outcome: SmokeResult | Exception,
    stages: tuple[str, ...] = (),
) -> None:
    async def fake_run_smoke_check(**kwargs: Any) -> SmokeResult:
        captured.update(kwargs)
        on_stage = cast(Callable[[str], None], kwargs["on_stage"])
        for stage in stages:
            on_stage(stage)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cmd_mod, "run_smoke_check", fake_run_smoke_check)


def _invoke(args: list[str]) -> Any:
    runner = CliRunner()
    return runner.invoke(main_mod.app, ["smoke", *args])


def test_smoke_exits_0_and_prints_stages_when_check_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runtime(monkeypatch, rt=_make_rt())
    captured: dict[str, Any] = {}
    _install_run_smoke_check(
        monkeypatch,
        captured=captured,
        outcome=_smoke_result(),
        stages=("provisioning smoke tenant", "smoke turn complete"),
    )

    result = _invoke([])

    assert result.exit_code == 0, f"expected success, got {result.exit_code}; {result.stdout!r}"
    assert "provisioning smoke tenant" in result.stdout, (
        f"stage line missing from stdout: {result.stdout!r}"
    )
    assert "smoke turn complete" in result.stdout, (
        f"stage line missing from stdout: {result.stdout!r}"
    )


def test_smoke_exits_1_when_check_raises_smoke_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runtime(monkeypatch, rt=_make_rt())
    captured: dict[str, Any] = {}
    _install_run_smoke_check(
        monkeypatch,
        captured=captured,
        outcome=SmokeCheckError("no usage_events row was written"),
    )

    result = _invoke([])

    assert result.exit_code == 1, f"expected failure, got {result.exit_code}; {result.stdout!r}"
    assert "no usage_events row was written" in result.stdout, (
        f"error message missing from stdout: {result.stdout!r}"
    )


def test_smoke_exits_1_when_upstream_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runtime(monkeypatch, rt=_make_rt())

    class _FakeReq:
        method = "POST"
        url = "https://x"

    captured: dict[str, Any] = {}
    _install_run_smoke_check(
        monkeypatch,
        captured=captured,
        outcome=anthropic.APIConnectionError(request=cast(Any, _FakeReq())),
    )

    result = _invoke([])

    assert result.exit_code == 1, f"expected failure, got {result.exit_code}; {result.stdout!r}"


def test_smoke_passes_deploy_sha_as_topup_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runtime(monkeypatch, rt=_make_rt())
    captured: dict[str, Any] = {}
    _install_run_smoke_check(monkeypatch, captured=captured, outcome=_smoke_result())

    deploy_sha = "a" * 40
    result = _invoke(["--deploy-sha", deploy_sha])

    assert result.exit_code == 0, result.stdout
    assert captured["topup_key"] == deploy_sha, (
        f"expected topup_key to be exactly the deploy sha, got {captured['topup_key']!r}"
    )


def test_smoke_derives_topup_key_when_deploy_sha_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runtime(monkeypatch, rt=_make_rt())
    captured: dict[str, Any] = {}
    _install_run_smoke_check(monkeypatch, captured=captured, outcome=_smoke_result())

    result = _invoke([])

    assert result.exit_code == 0, result.stdout
    topup_key = captured["topup_key"]
    assert topup_key, "expected a non-empty derived topup_key"
    assert topup_key != "None", (
        f"derived topup_key must not be the literal string 'None', got {topup_key!r}"
    )


def test_smoke_forwards_timeout_and_deployment_default_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runtime(
        monkeypatch,
        rt=_make_rt(agent_name="custom-agent", environment_name="custom-env"),
    )
    captured: dict[str, Any] = {}
    _install_run_smoke_check(monkeypatch, captured=captured, outcome=_smoke_result())

    result = _invoke(["--timeout", "42.5"])

    assert result.exit_code == 0, result.stdout
    assert captured["timeout_s"] == 42.5, f"timeout not forwarded, got {captured['timeout_s']!r}"
    assert captured["agent_tag"] == "custom-agent", (
        f"agent tag not derived from deployment_default, got {captured['agent_tag']!r}"
    )
    assert captured["environment_tag"] == "custom-env", (
        f"environment tag not derived from deployment_default, got {captured['environment_tag']!r}"
    )


def test_smoke_defaults_tags_when_deployment_default_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runtime(monkeypatch, rt=_make_rt(agent_name=None, environment_name=None))
    captured: dict[str, Any] = {}
    _install_run_smoke_check(monkeypatch, captured=captured, outcome=_smoke_result())

    result = _invoke([])

    assert result.exit_code == 0, result.stdout
    assert captured["agent_tag"] == "daimon", captured["agent_tag"]
    assert captured["environment_tag"] == "default", captured["environment_tag"]


def test_smoke_runs_under_the_deployments_tool_safety_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design test 6: the CLI passes settings.tool_safety, never a default."""
    _install_runtime(monkeypatch, rt=_make_rt())
    captured: dict[str, Any] = {}
    _install_run_smoke_check(monkeypatch, captured=captured, outcome=_smoke_result())
    result = _invoke([])
    assert result.exit_code == 0, result.stdout
    assert captured["tool_safety"] == ToolSafetyPolicy(enabled=True)
