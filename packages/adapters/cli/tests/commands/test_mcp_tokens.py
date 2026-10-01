"""`daimon mcp mint-operator-token | list-tokens | revoke-token` and the CLI token row."""

from __future__ import annotations

import datetime as dt
import io
import json
import uuid
from typing import cast

import jwt as pyjwt
import pytest
import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands.mcp import mint_token
from daimon.adapters.cli.commands.mcp_tokens import list_tokens, mint_operator_token, revoke_token
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.config import (
    AnthropicSettings,
    CLISettings,
    DatabaseSettings,
    McpSettings,
    Settings,
)
from daimon.core.errors import ConfigError, StoreError
from daimon.core.stores.accounts import set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.mcp_tokens import get_mcp_token
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import PostgresDsn, SecretStr
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SECRET = "a" * 32


def _rt(sessionmaker: async_sessionmaker[AsyncSession]) -> CliRuntime:
    rt = object.__new__(CliRuntime)
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        cli=CLISettings(local_user="alice"),
        mcp=McpSettings(jwt_secret=SecretStr(SECRET)),
    )
    object.__setattr__(rt, "settings", settings)
    object.__setattr__(rt, "anthropic", cast(AsyncAnthropic, object()))
    object.__setattr__(rt, "sessionmaker", sessionmaker)
    return rt


def _console() -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(file=out, width=400, highlight=False), out


async def _seed(
    sm: async_sessionmaker[AsyncSession], *, role: Role = Role.ADMIN, user: str | None = "u-1"
) -> tuple[uuid.UUID, uuid.UUID]:
    async with sm() as s, s.begin():
        tenant = await make_tenant(s, platform="discord")
        account = await make_account(s, tenant=tenant)
        await set_role(s, account.id, role)
        if user is not None:
            await make_platform_principal(
                s, platform="discord", external_id=user, tenant=tenant, account=account
            )
    return tenant.id, account.id


async def _mint(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    account: str,
    scopes: list[str],
    *,
    max_issued_usd: str | None = None,
) -> str:
    console, out = _console()
    await mint_operator_token(
        rt=_rt(sm),
        console=console,
        tenant=str(tenant_id),
        account=account,
        scopes=scopes,
        ttl_days=30,
        max_issued_usd=max_issued_usd,
        label="admin bot",
    )
    return out.getvalue().splitlines()[0]


@pytest.mark.asyncio
async def test_mint_operator_token_by_platform_user_registers_its_scopes(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, account_id = await _seed(schema_sessionmaker)

    token = await _mint(
        schema_sessionmaker, tenant_id, "u-1", ["promo:create"], max_issued_usd="250"
    )

    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    async with schema_sessionmaker() as s:
        row = await get_mcp_token(s, jti=uuid.UUID(claims["jti"]))
    assert claims["kind"] == "operator" and claims["sub"] == str(account_id)
    assert row is not None and (row.kind, row.scopes, str(row.max_issued_usd)) == (
        "operator",
        ("promo:create",),
        "250.00",
    ), "the row carries the scopes and the ceiling"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "user", "match"),
    [(Role.USER, "u-1", "not a server admin"), (Role.ADMIN, None, "has no discord user")],
)
async def test_mint_operator_token_requires_an_admin_with_a_platform_user(
    schema_sessionmaker: async_sessionmaker[AsyncSession], role: Role, user: str | None, match: str
) -> None:
    tenant_id, account_id = await _seed(schema_sessionmaker, role=role, user=user)
    with pytest.raises(ConfigError, match=match):
        await _mint(schema_sessionmaker, tenant_id, str(account_id), ["tenant:read"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scopes", "ceiling", "match"),
    [(["admin"], None, "unknown scope"), (["tenant:read"], "50", "promo:create")],
)
async def test_mint_operator_token_validates_terms_before_writing(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    scopes: list[str],
    ceiling: str | None,
    match: str,
) -> None:
    tenant_id, _account_id = await _seed(schema_sessionmaker)
    with pytest.raises(typer.BadParameter, match=match):
        await _mint(schema_sessionmaker, tenant_id, "u-1", scopes, max_issued_usd=ceiling)


@pytest.mark.asyncio
async def test_list_then_revoke_a_token(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _account_id = await _seed(schema_sessionmaker)
    token = await _mint(schema_sessionmaker, tenant_id, "u-1", ["tenant:read"])
    jti = uuid.UUID(pyjwt.decode(token, SECRET, algorithms=["HS256"])["jti"])
    rt = _rt(schema_sessionmaker)

    console, out = _console()
    await list_tokens(
        rt=rt, console=console, tenant=None, kind="operator", include_inactive=False, as_json=True
    )
    listed = json.loads(out.getvalue())
    await revoke_token(rt=rt, console=_console()[0], jti=jti)
    console, out = _console()
    await list_tokens(
        rt=rt, console=console, tenant=None, kind=None, include_inactive=False, as_json=True
    )

    assert [(row["jti"], row["scopes"]) for row in listed] == [(str(jti), ["tenant:read"])]
    assert json.loads(out.getvalue()) == [], "a revoked token leaves the default list"
    with pytest.raises(StoreError, match="no live token"):
        await revoke_token(rt=rt, console=_console()[0], jti=jti)


@pytest.mark.asyncio
async def test_mint_token_registers_an_expiring_cli_row(
    schema_sessionmaker: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with schema_sessionmaker() as s, s.begin():
        await make_tenant(s, platform="cli", workspace_id="local")

    await mint_token(rt=_rt(schema_sessionmaker), os_user="alice", ttl_days=7)

    token = capsys.readouterr().out.strip().splitlines()[-1]
    claims = pyjwt.decode(token, SECRET, algorithms=["HS256"])
    async with schema_sessionmaker() as s:
        row = await get_mcp_token(s, jti=uuid.UUID(claims["jti"]))
    assert row is not None and row.kind == "cli", "new CLI tokens are registered"
    assert row.expires_at is not None and row.expires_at - row.created_at == dt.timedelta(days=7)
