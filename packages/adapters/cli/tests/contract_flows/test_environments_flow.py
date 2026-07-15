"""Flow test: environment create -> delete lifecycle via CLI against real MA."""

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


def test_environment_crud(
    tmp_path: Path,
    anthropic_client: AsyncAnthropic,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment create -> delete lifecycle via CliRunner against real MA.

    The delete command handles 409 (active sessions) by falling back to archive
    internally, so exit code 0 covers both delete and archive paths.
    """
    name = f"contract-test-env-{uuid.uuid4().hex[:8]}"

    # Write a minimal environment spec YAML
    spec_path = tmp_path / "env.yaml"
    spec_path.write_text(f"name: {name}\nconfig:\n  type: cloud\n")

    install_runtime(monkeypatch, anthropic=anthropic_client, sessionmaker=db_session_factory)
    runner = CliRunner()

    # create
    result = runner.invoke(main_mod.app, ["environments", "create", str(spec_path)])
    assert result.exit_code == 0, f"create failed: {result.output}"

    # delete (CLI handles 409->archive fallback internally)
    result = runner.invoke(main_mod.app, ["environments", "delete", name, "--yes"])
    assert result.exit_code == 0, f"delete failed: {result.output}"
