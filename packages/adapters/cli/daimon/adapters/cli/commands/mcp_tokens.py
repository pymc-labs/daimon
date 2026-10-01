"""`daimon mcp mint-operator-token | list-tokens | revoke-token`: registered MCP tokens.

An operator token lets an external integration call a scoped set of MCP
tools for one server admin. It is minted here only, never from chat, so a
``promo:create`` token (which issues credit any tenant can redeem) always
comes from someone with deployment access.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import TenantSelector, discover_tenant, resolve_tenant_override
from daimon.core.config import load_settings
from daimon.core.errors import ConfigError, StoreError
from daimon.core.mcp_auth import mint_operator_mcp_token
from daimon.core.operator_tokens import (
    OPERATOR_SCOPES,
    OperatorTokenError,
    parse_operator_scopes,
    validate_operator_terms,
)
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, McpTokenKind, Role
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.mcp_tokens import list_mcp_tokens, revoke_mcp_token
from daimon.core.stores.tenants import get_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession


def _secret(rt: CliRuntime) -> bytes:
    if rt.settings.mcp.jwt_secret is None:
        raise ConfigError(
            "DAIMON_MCP__JWT_SECRET is unset. Generate one with "
            "`python -c 'import secrets; print(secrets.token_hex(32))'`."
        )
    return rt.settings.mcp.jwt_secret.get_secret_value().encode()


async def _resolve_admin_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account: str
) -> AccountIdentityRow:
    """The account an operator token acts for: an account uuid or the platform user id."""
    tenant = await get_tenant(session, tenant_id)
    assert tenant is not None, "discover_tenant checked the tenant exists"
    try:
        account_id = uuid.UUID(account)
    except ValueError:
        principal = await find_platform_principal(
            session, tenant_id=tenant_id, platform=tenant.platform, external_id=account
        )
        if principal is None:
            raise ConfigError(
                f"no {tenant.platform} user {account!r} in tenant {tenant_id}"
            ) from None
        account_id = principal.account_id
    identity = await get_account_with_tenant(session, account_id=account_id)
    if identity is None or identity.tenant_id != tenant_id:
        raise ConfigError(f"no account {account!r} in tenant {tenant_id}")
    if identity.role is not Role.ADMIN:
        raise ConfigError(
            f"{account} is not a server admin here. Admin status is stored from their "
            "latest message, so a new admin needs to talk to daimon once first."
        )
    if identity.platform_user_id is None:
        raise ConfigError(f"{account} has no {tenant.platform} user, so it cannot hold one")
    return identity


async def mint_operator_token(
    *,
    rt: CliRuntime,
    console: Console,
    tenant: str,
    account: str,
    scopes: list[str],
    ttl_days: int,
    max_issued_usd: str | None,
    label: str | None,
) -> None:
    try:
        ceiling = Decimal(max_issued_usd) if max_issued_usd is not None else None
    except InvalidOperation as exc:
        raise typer.BadParameter("--max-issued-usd must be a dollar amount") from exc
    try:
        parsed = parse_operator_scopes(scopes)
        validate_operator_terms(scopes=parsed, ttl_days=ttl_days, max_issued_usd=ceiling)
    except OperatorTokenError as exc:
        raise typer.BadParameter(str(exc)) from exc
    secret = _secret(rt)
    now = dt.datetime.now(dt.UTC)
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, TenantSelector(tenant_id=tenant))
        tenant_id = await discover_tenant(session, override=override)
        identity = await _resolve_admin_account(session, tenant_id=tenant_id, account=account)
        token = await mint_operator_mcp_token(
            session,
            account_id=identity.account_id,
            tenant_id=tenant_id,
            scopes=parsed,
            label=label,
            secret=secret,
            now=now,
            ttl_days=ttl_days,
            max_issued_usd=ceiling,
        )
    console.print(token, soft_wrap=True, highlight=False, markup=False)
    expires = (now + dt.timedelta(days=ttl_days)).isoformat()
    console.print(f"scopes: {', '.join(sorted(parsed))}; expires {expires}", markup=False)
    console.print("This is the one time the token is shown. Revoke it with revoke-token.")


async def list_tokens(
    *,
    rt: CliRuntime,
    console: Console,
    tenant: str | None,
    kind: McpTokenKind | None,
    include_inactive: bool,
    as_json: bool,
) -> None:
    async with rt.sessionmaker() as session:
        tenant_id = None
        if tenant is not None:
            override = await resolve_tenant_override(session, TenantSelector(tenant_id=tenant))
            tenant_id = await discover_tenant(session, override=override)
        rows = await list_mcp_tokens(
            session,
            now=dt.datetime.now(dt.UTC),
            tenant_id=tenant_id,
            kind=kind,
            include_inactive=include_inactive,
        )
    emit_rows(
        console,
        rows,
        columns=(
            "jti",
            "kind",
            "tenant_id",
            "account_id",
            "scopes",
            "label",
            "created_at",
            "expires_at",
            "revoked_at",
            "issued_usd",
            "max_issued_usd",
        ),
        as_json=as_json,
    )


async def revoke_token(*, rt: CliRuntime, console: Console, jti: uuid.UUID) -> None:
    async with rt.sessionmaker() as session, session.begin():
        row = await revoke_mcp_token(session, jti=jti, now=dt.datetime.now(dt.UTC))
    if row is None:
        raise StoreError(f"no live token {jti}")
    console.print(f"revoked {row.kind} token {row.jti}: its next request gets a 401")


_KINDS: dict[str, McpTokenKind] = {"agent": "agent", "operator": "operator", "cli": "cli"}


def register_token_commands(app: typer.Typer) -> None:
    @app.command("mint-operator-token")
    def mint_operator_token_command(  # pyright: ignore[reportUnusedFunction]
        tenant: Annotated[str, typer.Option("--tenant", help="Tenant uuid.")],
        account: Annotated[
            str,
            typer.Option(
                "--account", help="The server admin it acts for: account uuid or platform user id."
            ),
        ],
        scope: Annotated[
            list[str],
            typer.Option("--scope", help=f"Repeat per scope: {', '.join(OPERATOR_SCOPES)}."),
        ],
        ttl_days: Annotated[int, typer.Option("--ttl-days", help="Days until it expires.")] = 30,
        max_issued_usd: Annotated[
            str | None,
            typer.Option(
                "--max-issued-usd",
                help="With promo:create: total promo credit this token may issue.",
            ),
        ] = None,
        label: Annotated[str | None, typer.Option("--label", help="Note stored with it.")] = None,
    ) -> None:
        """Mint a scoped operator token for an external integration."""
        settings = load_settings()
        console = Console(highlight=False)

        async def _go() -> None:
            async with build_runtime(settings) as rt:
                await mint_operator_token(
                    rt=rt,
                    console=console,
                    tenant=tenant,
                    account=account,
                    scopes=scope,
                    ttl_days=ttl_days,
                    max_issued_usd=max_issued_usd,
                    label=label,
                )

        run_cli(_go(), console=console)

    @app.command("list-tokens")
    def list_tokens_command(  # pyright: ignore[reportUnusedFunction]
        tenant: Annotated[str | None, typer.Option("--tenant", help="Only this tenant.")] = None,
        kind: Annotated[str | None, typer.Option("--kind", help="agent, operator or cli.")] = None,
        include_inactive: Annotated[
            bool, typer.Option("--all", help="Include revoked and expired tokens.")
        ] = False,
        as_json: Annotated[bool, JSON_OPTION] = False,
    ) -> None:
        """List registered MCP tokens (agent keys, operator and CLI tokens)."""
        if kind is not None and kind not in _KINDS:
            raise typer.BadParameter("--kind must be agent, operator or cli")
        settings = load_settings()
        console = Console(highlight=False)

        async def _go() -> None:
            async with build_runtime(settings) as rt:
                await list_tokens(
                    rt=rt,
                    console=console,
                    tenant=tenant,
                    kind=_KINDS[kind] if kind is not None else None,
                    include_inactive=include_inactive,
                    as_json=as_json,
                )

        run_cli(_go(), console=console)

    @app.command("revoke-token")
    def revoke_token_command(jti: uuid.UUID) -> None:  # pyright: ignore[reportUnusedFunction]
        """Revoke a registered MCP token by its jti."""
        settings = load_settings()
        console = Console(highlight=False)

        async def _go() -> None:
            async with build_runtime(settings) as rt:
                await revoke_token(rt=rt, console=console, jti=jti)

        run_cli(_go(), console=console)
