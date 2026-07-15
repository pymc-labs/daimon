"""Flow test: agent create -> list -> archive lifecycle via CLI against real MA."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli import main as main_mod
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from packages.adapters.cli.tests.contract_flows.conftest import install_runtime

pytestmark = pytest.mark.contract


def test_agent_crud(
    tmp_path: Path,
    anthropic_client: AsyncAnthropic,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent create -> list -> archive lifecycle via CliRunner against real MA."""
    name = f"ct-agent-{uuid.uuid4().hex[:6]}"

    # Write a minimal agent spec YAML
    spec_path = tmp_path / "agent.yaml"
    spec_path.write_text(f"name: {name}\nmodel: claude-haiku-4-5\n")

    install_runtime(monkeypatch, anthropic=anthropic_client, sessionmaker=db_session_factory)
    runner = CliRunner()

    # create
    result = runner.invoke(main_mod.app, ["agents", "create", str(spec_path)])
    assert result.exit_code == 0, f"create failed: {result.output}"

    # list
    result = runner.invoke(main_mod.app, ["agents", "list"])
    assert result.exit_code == 0, f"list failed: {result.output}"
    assert name in result.output, f"created agent must appear in list, got: {result.output}"

    # archive
    result = runner.invoke(main_mod.app, ["agents", "archive", name, "--yes"])
    assert result.exit_code == 0, f"archive failed: {result.output}"
