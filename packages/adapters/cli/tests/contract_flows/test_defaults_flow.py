"""Flow test: defaults apply idempotency against real MA API + real Postgres."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli import main as main_mod
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from packages.adapters.cli.tests.contract_flows.conftest import (
    install_runtime,
    require_flow_prerequisites,
)

pytestmark = pytest.mark.contract


def _write_defaults_tree(path: Path) -> None:
    """Write a minimal defaults tree with one agent and one environment."""
    agents_dir = path / "agents"
    agents_dir.mkdir()
    (agents_dir / "flow-test-agent.yaml").write_text(
        "name: flow-test-agent\nmodel: claude-haiku-4-5\n"
    )

    environments_dir = path / "environments"
    environments_dir.mkdir()
    (environments_dir / "flow-test-env.yaml").write_text(
        "name: flow-test-env\nconfig:\n  type: cloud\n"
    )


async def _count_ma_resources(api_key: str) -> dict[str, int]:
    """Count agents and environments in the MA workspace."""
    client = AsyncAnthropic(api_key=api_key)
    agent_count = 0
    async for _ in client.beta.agents.list():
        agent_count += 1
    env_count = 0
    async for _ in client.beta.environments.list():
        env_count += 1
    return {"agents": agent_count, "environments": env_count}


def test_defaults_apply_is_idempotent(
    tmp_path: Path,
    anthropic_client: AsyncAnthropic,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running defaults apply twice must not create duplicates."""
    _write_defaults_tree(tmp_path)
    install_runtime(monkeypatch, anthropic=anthropic_client, sessionmaker=db_session_factory)

    runner = CliRunner()

    result1 = runner.invoke(main_mod.app, ["defaults", "apply", "--defaults-root", str(tmp_path)])
    assert result1.exit_code == 0, f"first apply failed: {result1.output}"

    api_key, _url = require_flow_prerequisites()
    count_after_first = asyncio.run(_count_ma_resources(api_key))

    result2 = runner.invoke(main_mod.app, ["defaults", "apply", "--defaults-root", str(tmp_path)])
    assert result2.exit_code == 0, f"second apply failed: {result2.output}"

    count_after_second = asyncio.run(_count_ma_resources(api_key))

    assert count_after_first == count_after_second, (
        f"second apply must not create duplicates: "
        f"after first={count_after_first}, after second={count_after_second}"
    )
