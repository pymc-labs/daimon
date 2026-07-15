"""CLI tests for `daimon mcp mint-token`, `daimon mcp url`, and `daimon mcp sweep-credentials`."""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from urllib.parse import urlparse

import httpx
import jwt as pyjwt
import pytest
import pytest_asyncio
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands.mcp import mcp_sweep_credentials, mcp_url, mint_token
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core._models import Base
from daimon.core.config import (
    AnthropicSettings,
    CLISettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic
from pydantic import HttpUrl, PostgresDsn, SecretStr
from rich.console import Console
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


def _build_settings(
    *,
    jwt_secret: str | None = None,
    public_url: str | None = None,
    local_user: str = "testuser",
) -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        cli=CLISettings(local_user=local_user),
        mcp=McpSettings(
            jwt_secret=SecretStr(jwt_secret) if jwt_secret is not None else None,
            public_url=HttpUrl(public_url) if public_url is not None else None,
        ),
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest_asyncio.fixture
async def schema_sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


def _make_rt(
    *,
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> CliRuntime:
    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", settings)
    object.__setattr__(rt, "anthropic", cast(AsyncAnthropic, object()))
    object.__setattr__(rt, "sessionmaker", sessionmaker)
    return cast(CliRuntime, rt)


@pytest.mark.asyncio
async def test_mcp_url_prints_public_url() -> None:
    """mcp_url echoes the public URL when configured."""
    settings = _build_settings(public_url="https://mcp.example.com/mcp")
    # Should not raise; output goes to typer.echo (stdout)
    await mcp_url(settings=settings)


@pytest.mark.asyncio
async def test_mcp_url_errors_when_unset() -> None:
    """mcp_url raises ConfigError when DAIMON_MCP__PUBLIC_URL is not set."""
    from daimon.core.errors import ConfigError

    settings = _build_settings(public_url=None)
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        await mcp_url(settings=settings)


async def _seed_tenant(sm: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sm() as s, s.begin():
        tenant = await make_tenant(s, platform="cli", workspace_id="local")
        return tenant.id


@pytest.mark.asyncio
async def test_mcp_mint_token_prints_verifiable_jwt(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """mint_token writes a verifiable HS256 JWT to stdout."""
    _tenant_id = await _seed_tenant(schema_sessionmaker)

    secret = "a" * 32
    settings = _build_settings(jwt_secret=secret, local_user="alice")
    rt = _make_rt(settings=settings, sessionmaker=schema_sessionmaker)

    await mint_token(rt=rt, os_user="alice")

    out = capsys.readouterr().out
    token = out.strip().splitlines()[-1]

    decoded = pyjwt.decode(token, secret, algorithms=["HS256"])
    assert "sub" in decoded, "JWT must contain a 'sub' claim"
    uuid.UUID(decoded["sub"])  # raises if not a valid UUID


@pytest.mark.asyncio
async def test_mcp_mint_token_requires_secret(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """mint_token raises ConfigError when DAIMON_MCP__JWT_SECRET is not set."""
    from daimon.core.errors import ConfigError

    settings = _build_settings(jwt_secret=None)
    rt = _make_rt(settings=settings, sessionmaker=schema_sessionmaker)

    with pytest.raises(ConfigError, match="JWT_SECRET"):
        await mint_token(rt=rt, os_user=None)


def test_mcp_url_via_runner_prints_url(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typer-level smoke: `daimon mcp url` reaches mcp_url and echoes the URL."""
    from daimon.adapters.cli.commands import mcp as mcp_cmd

    monkeypatch.setattr(
        mcp_cmd, "load_settings", lambda: _build_settings(public_url="https://mcp.example.com/mcp")
    )
    result = runner.invoke(main_mod.app, ["mcp", "url"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert "https://mcp.example.com/mcp" in result.stdout


def test_mcp_url_via_runner_errors_when_unset(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typer-level smoke: `daimon mcp url` exits non-zero when URL is unset."""
    from daimon.adapters.cli.commands import mcp as mcp_cmd

    monkeypatch.setattr(mcp_cmd, "load_settings", lambda: _build_settings(public_url=None))
    result = runner.invoke(main_mod.app, ["mcp", "url"])
    combined = result.stdout + (result.stderr or "")
    assert result.exit_code != 0
    assert "PUBLIC_URL" in combined


# ---------------------------------------------------------------------------
# sweep-credentials tests
# ---------------------------------------------------------------------------

_PUBLIC_URL = "https://mcp.example.com/mcp"
_COPILOT_URL = "https://api.githubcopilot.com/mcp"
_JWT_SECRET = "s" * 32


def _vault_wire(vault_id: str, display_name: str) -> dict[str, Any]:
    """Serialized BetaManagedAgentsVault for transport handler responses."""
    return BetaManagedAgentsVault(
        id=vault_id,
        type="vault",
        display_name=display_name,
        metadata={},
        archived_at=None,
        created_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
    ).model_dump(mode="json")


def _cred_wire(
    *,
    cred_id: str,
    vault_id: str,
    mcp_server_url: str,
) -> dict[str, Any]:
    """Minimal static_bearer credential wire shape."""
    return {
        "id": cred_id,
        "type": "vault_credential",
        "vault_id": vault_id,
        "metadata": {},
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        "auth": {"type": "static_bearer", "mcp_server_url": mcp_server_url},
    }


def _make_sweep_rt(
    *,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    jwt_secret: str | None = _JWT_SECRET,
    public_url: str | None = _PUBLIC_URL,
) -> CliRuntime:
    """Build a CliRuntime stub for sweep-credentials tests.

    Uses build_fake_anthropic with a transport-level handler for vault operations.
    session_factory is not used by sweep_stale_admin_credentials so a dummy is fine.
    """
    settings = _build_settings(jwt_secret=jwt_secret, public_url=public_url)
    anthropic = build_fake_anthropic(handler if handler is not None else _noop_handler)
    # sweep_stale_admin_credentials does not use sessionmaker — pass a dummy.
    dummy_sessionmaker = cast(
        "async_sessionmaker[AsyncSession]",
        object(),
    )
    rt = object.__new__(CliRuntime)
    object.__setattr__(rt, "settings", settings)
    object.__setattr__(rt, "anthropic", anthropic)
    object.__setattr__(rt, "sessionmaker", dummy_sessionmaker)
    return cast(CliRuntime, rt)


def _noop_handler(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": [], "has_more": False})


@pytest.mark.asyncio
async def test_sweep_credentials_dry_run_lists_target_without_writes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """mcp_sweep_credentials in dry_run mode: exit 0, output lists planned target,
    NO delete/create hits the transport (key safety assertion)."""
    account_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{uuid.uuid4()}"

    mutating_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={"data": [_vault_wire("vlt_sweep", display)], "has_more": False},
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_sweep/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_stale",
                            vault_id="vlt_sweep",
                            mcp_server_url=_PUBLIC_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        mutating_calls.append(f"{req.method} {req.url.path}")
        return httpx.Response(200, json={})

    rt = _make_sweep_rt(handler=handler)
    console = Console(highlight=False)
    # dry_run=True is the default; apply=False maps to dry_run=True.
    await mcp_sweep_credentials(rt=rt, console=console, apply=False)

    assert mutating_calls == [], f"dry_run must not issue any DELETE or POST; got: {mutating_calls}"
    out = capsys.readouterr().out
    assert "vlt_sweep" in out or "vcrd_stale" in out, (
        "dry_run output must mention the planned target vault or cred"
    )


@pytest.mark.asyncio
async def test_sweep_credentials_apply_issues_delete_and_create(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """mcp_sweep_credentials with apply=True: DELETE+POST fired for the public_url cred."""
    account_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{uuid.uuid4()}"

    deleted_ids: list[str] = []
    created_bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={"data": [_vault_wire("vlt_apply", display)], "has_more": False},
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_apply/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_old",
                            vault_id="vlt_apply",
                            mcp_server_url=_PUBLIC_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        if req.method == "DELETE" and "/v1/vaults/vlt_apply/credentials/" in req.url.path:
            cred_id = req.url.path.rsplit("/", 1)[-1]
            deleted_ids.append(cred_id)
            return httpx.Response(200, json={"id": cred_id, "deleted": True})
        if req.method == "POST" and req.url.path == "/v1/vaults/vlt_apply/credentials":
            body: dict[str, Any] = json.loads(req.content)
            created_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "vcrd_new",
                    "type": "vault_credential",
                    "vault_id": "vlt_apply",
                    "metadata": {},
                    "created_at": "2026-06-29T00:00:00Z",
                    "updated_at": "2026-06-29T00:00:00Z",
                    "auth": {"type": "static_bearer", "mcp_server_url": _PUBLIC_URL},
                },
            )
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    rt = _make_sweep_rt(handler=handler)
    console = Console(highlight=False)
    await mcp_sweep_credentials(rt=rt, console=console, apply=True)

    assert deleted_ids == ["vcrd_old"], "apply must DELETE the stale cred"
    assert len(created_bodies) == 1, "apply must POST one fresh cred"
    token: str = created_bodies[0]["auth"]["token"]
    claims: dict[str, Any] = pyjwt.decode(token, _JWT_SECRET, algorithms=["HS256"])
    assert "is_admin" not in claims, "recreated token must not carry is_admin"


@pytest.mark.asyncio
async def test_sweep_credentials_raises_config_error_when_public_url_unset() -> None:
    """mcp_sweep_credentials raises ConfigError when settings.mcp.public_url is None."""
    from daimon.core.errors import ConfigError

    rt = _make_sweep_rt(public_url=None)
    console = Console(highlight=False)
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        await mcp_sweep_credentials(rt=rt, console=console, apply=False)


@pytest.mark.asyncio
async def test_sweep_credentials_raises_config_error_when_jwt_secret_unset() -> None:
    """mcp_sweep_credentials raises ConfigError when settings.mcp.jwt_secret is None."""
    from daimon.core.errors import ConfigError

    rt = _make_sweep_rt(jwt_secret=None)
    console = Console(highlight=False)
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        await mcp_sweep_credentials(rt=rt, console=console, apply=False)


def test_mcp_sweep_credentials_via_runner_dry_run(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typer-level smoke: `daimon mcp sweep-credentials` exits 0 (dry-run, no --apply)."""
    from daimon.adapters.cli.commands import mcp as mcp_cmd

    account_id = uuid.uuid4()
    display = f"daimon-mcp:{account_id}:{uuid.uuid4()}"
    mutating_calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={"data": [_vault_wire("vlt_runner", display)], "has_more": False},
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_runner/credentials":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _cred_wire(
                            cred_id="vcrd_stale",
                            vault_id="vlt_runner",
                            mcp_server_url=_PUBLIC_URL,
                        ),
                    ],
                    "has_more": False,
                },
            )
        mutating_calls.append(f"{req.method} {req.url.path}")
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        mcp_cmd,
        "load_settings",
        lambda: _build_settings(jwt_secret=_JWT_SECRET, public_url=_PUBLIC_URL),
    )
    # Patch build_runtime to use our fake anthropic.
    import contextlib

    @contextlib.asynccontextmanager
    async def _fake_build_runtime(settings: Settings) -> AsyncIterator[CliRuntime]:  # type: ignore[override]
        rt = _make_sweep_rt(handler=handler)
        yield rt

    monkeypatch.setattr(mcp_cmd, "build_runtime", _fake_build_runtime)

    result = runner.invoke(main_mod.app, ["mcp", "sweep-credentials"])
    assert result.exit_code == 0, result.stdout + (result.stderr or "")
    assert mutating_calls == [], (
        f"dry_run (no --apply) must not issue any writes; got: {mutating_calls}"
    )
