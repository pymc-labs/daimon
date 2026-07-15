"""Behavioral CLI tests for the adapter-edge error boundary.

Invokes the real `main.app` Typer program with CliRunner against a real
Postgres (schema-per-test) and an SDK faked at the httpx transport layer
(per guideline:testing). Asserts user-visible stdout shape and exit codes.

Sync tests — each Typer command owns its own `asyncio.run` loop; our setup
blocks use their own `asyncio.run`. NullPool keeps asyncpg connections
bound to the loop that opened them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.beta_managed_agents_model_config import (
    BetaManagedAgentsModelConfig,
)
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import agents as agents_cmd
from daimon.adapters.cli.commands import config as config_cmd
from daimon.adapters.cli.commands import environments as environments_cmd
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core._models import Base
from daimon.core.config import Settings
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_stub_anthropic
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner


def _require_test_dsn() -> str:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL") or os.environ.get("DAIMON_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("DAIMON_DATABASE__TEST_URL must be set to run these tests.")
    if "test" not in urlparse(url).path:
        raise RuntimeError("Refusing to run destructive fixtures against a non-test DB.")
    return url


def _settings() -> Settings:
    class _Cli:
        local_user = "testuser"

    class _Settings:
        cli = _Cli()

    return cast(Settings, _Settings())


def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    modules: tuple[object, ...],
) -> None:
    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", _settings())
    object.__setattr__(rt, "anthropic", anthropic)
    object.__setattr__(rt, "sessionmaker", sessionmaker)

    @asynccontextmanager
    async def fake_build_runtime(_settings: Settings) -> AsyncIterator[CliRuntime]:
        yield rt

    for mod in modules:
        monkeypatch.setattr(mod, "build_runtime", fake_build_runtime)
        monkeypatch.setattr(mod, "load_settings", _settings)


def _agent_json(*, agent_id: str, name: str, version: int = 1) -> dict[str, object]:
    import datetime as dt

    now = dt.datetime(2026, 4, 22, tzinfo=dt.UTC)
    return BetaManagedAgentsAgent(
        id=agent_id,
        type="agent",
        name=name,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed=None),
        metadata={},
        description=None,
        archived_at=None,
        created_at=now,
        updated_at=now,
        version=version,
        mcp_servers=[],
        skills=[],
        tools=[],
        system="you are helpful",
    ).model_dump(mode="json")


def _write_agent_spec(tmp_path: Path, name: str) -> Path:
    spec = tmp_path / f"{name}.yaml"
    spec.write_text(
        "\n".join(
            [
                f"name: {name}",
                "model: claude-sonnet-4-6",
                "system: hi",
            ]
        )
    )
    return spec


@pytest_asyncio.fixture
async def schema_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh schema + a sessionmaker bound to it."""
    dsn = _require_test_dsn()
    schema = f"test_{uuid.uuid4().hex}"
    engine = create_async_engine(dsn, poolclass=NullPool)

    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn2 = await conn.execution_options(schema_translate_map={None: schema})
        await conn2.run_sync(Base.metadata.create_all)
        await conn.commit()

    sessionmaker = async_sessionmaker(
        engine.execution_options(schema_translate_map={None: schema}),
        expire_on_commit=False,
        class_=AsyncSession,
    )
    try:
        yield sessionmaker
    finally:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await conn.commit()
        await engine.dispose()


def test_agents_create_api_conflict_exits_1_without_traceback(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MA-direct: duplicate agent create returns 409 from the API.
    Must exit 1 with a clean upstream error line, no Rich traceback."""

    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="cli", workspace_id="local")
            await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
            return tenant.id

    _tenant_id = asyncio.run(seed())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/agents"):
            return httpx.Response(
                409,
                json={"error": {"type": "conflict", "message": "agent already exists"}},
            )
        raise AssertionError(f"unexpected: {request.method} {request.url}")

    _install_runtime(
        monkeypatch,
        anthropic=build_stub_anthropic(handler),
        sessionmaker=schema_sessionmaker,
        modules=(agents_cmd,),
    )

    spec_path = _write_agent_spec(tmp_path, "dup")
    result = CliRunner().invoke(main_mod.app, ["agents", "create", str(spec_path)])

    assert result.exit_code == 1, (
        f"duplicate create must exit 1, got {result.exit_code}; stdout={result.stdout!r}"
    )
    combined = result.stdout + (result.stderr or "")
    assert "Traceback" not in combined, f"no Rich traceback should appear; got {combined!r}"
    assert "upstream:" in combined, f"expected upstream prefix in output, got {combined!r}"


def test_agents_update_missing_row_exits_1_with_no_agent_message(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="cli", workspace_id="local")
            await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
            return tenant.id

    _tenant_id = asyncio.run(seed())

    _install_runtime(
        monkeypatch,
        anthropic=build_stub_anthropic(),
        sessionmaker=schema_sessionmaker,
        modules=(agents_cmd,),
    )

    spec_path = _write_agent_spec(tmp_path, "nope")
    result = CliRunner().invoke(main_mod.app, ["agents", "update", "nope", str(spec_path)])

    combined = result.stdout + (result.stderr or "")
    assert result.exit_code == 1, combined
    assert "no agent named 'nope'" in combined, combined
    assert "Traceback" not in combined, combined


def test_environments_archive_missing_row_exits_1_with_no_environment_message(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="cli", workspace_id="local")
            await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
            return tenant.id

    _tenant_id = asyncio.run(seed())

    _install_runtime(
        monkeypatch,
        anthropic=build_stub_anthropic(),
        sessionmaker=schema_sessionmaker,
        modules=(environments_cmd,),
    )

    result = CliRunner().invoke(main_mod.app, ["environments", "archive", "nope", "--yes"])

    combined = result.stdout + (result.stderr or "")
    assert result.exit_code == 1, combined
    assert "no environment named 'nope'" in combined, combined
    assert "Traceback" not in combined, combined


def test_agents_create_api_error_surfaces_with_upstream_prefix(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="cli", workspace_id="local")
            await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
            return tenant.id

    _tenant_id = asyncio.run(seed())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/agents"):
            return httpx.Response(500, json={"error": {"type": "api_error", "message": "boom"}})
        raise AssertionError(f"unexpected: {request.method} {request.url}")

    _install_runtime(
        monkeypatch,
        anthropic=build_stub_anthropic(handler),
        sessionmaker=schema_sessionmaker,
        modules=(agents_cmd,),
    )

    spec_path = _write_agent_spec(tmp_path, "whatever")
    result = CliRunner().invoke(main_mod.app, ["agents", "create", str(spec_path)])

    combined = result.stdout + (result.stderr or "")
    assert result.exit_code == 1, combined
    assert "upstream:" in combined, combined
    assert "Traceback" not in combined, combined


def test_config_set_unknown_key_exits_2_usage_error(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runtime(
        monkeypatch,
        anthropic=build_stub_anthropic(),
        sessionmaker=schema_sessionmaker,
        modules=(config_cmd,),
    )

    result = CliRunner().invoke(main_mod.app, ["config", "set", "unknown_key=foo"])

    assert result.exit_code == 2, (
        f"usage error (unknown key) must exit 2 per Typer convention, "
        f"got {result.exit_code}; stdout={result.stdout!r}; stderr={result.stderr!r}"
    )


def test_agents_create_missing_yaml_exits_1_without_traceback(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: `agents create` on a missing path must exit 1 with a clean
    ✗ line, not a Rich traceback. The spec-loading failure is mapped to
    SpecError (a DaimonError) so run_cli catches it at the adapter edge."""

    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="cli", workspace_id="local")
            await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
            return tenant.id

    _tenant_id = asyncio.run(seed())

    _install_runtime(
        monkeypatch,
        anthropic=build_stub_anthropic(),
        sessionmaker=schema_sessionmaker,
        modules=(agents_cmd,),
    )

    missing = tmp_path / f"does-not-exist-{uuid.uuid4().hex}.yaml"
    result = CliRunner().invoke(main_mod.app, ["agents", "create", str(missing)])

    combined = result.stdout + (result.stderr or "")
    combined_flat = combined.replace("\n", "")
    assert result.exit_code == 1, combined
    assert "✗" in combined, f"missing ✗ prefix in output: {combined!r}"
    assert str(missing) in combined_flat, f"output must name the missing path: {combined!r}"
    assert "Traceback" not in combined, f"no Rich traceback expected: {combined!r}"


def test_agents_create_invalid_spec_exits_1_without_traceback(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Regression: `agents create` on a spec with an unknown field must exit 1
    with a clean ✗ line naming the bad field."""

    async def seed() -> uuid.UUID:
        async with schema_sessionmaker() as s, s.begin():
            tenant = await make_tenant(s, platform="cli", workspace_id="local")
            await get_or_create_cli_principal(s, tenant_id=tenant.id, os_user="testuser")
            return tenant.id

    _tenant_id = asyncio.run(seed())

    _install_runtime(
        monkeypatch,
        anthropic=build_stub_anthropic(),
        sessionmaker=schema_sessionmaker,
        modules=(agents_cmd,),
    )

    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nmodel: claude-haiku-4-5\nbad_field: xxx\n")
    result = CliRunner().invoke(main_mod.app, ["agents", "create", str(bad)])

    combined = result.stdout + (result.stderr or "")
    assert result.exit_code == 1, combined
    assert "✗" in combined, combined
    assert "bad_field" in combined, (
        f"output must name the rejected key so authors can fix typos: {combined!r}"
    )
    assert "Traceback" not in combined, combined
