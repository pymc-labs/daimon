"""`daimon mcp mint-operator-token | list-tokens | revoke-token | set-token-scopes`.

An operator token lets an external integration call a scoped set of MCP
tools for one server admin. It is minted here only, never from chat, so a
``promo:create`` token (which issues credit any tenant can redeem) always
comes from someone with deployment access. Minting, revoking and narrowing
each write a security audit row naming the token's jti, never its value.
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
from daimon.core.mcp_auth import mint_operator_mcp_token, token_jti
from daimon.core.operator_tokens import (
    MAX_TTL_DAYS,
    OPERATOR_SCOPES,
    OperatorTokenError,
    parse_operator_scopes,
    validate_operator_terms,
    validate_scope_narrowing,
)
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import AccountIdentityRow, McpTokenKind, Role
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.mcp_tokens import (
    list_mcp_tokens,
    lock_mcp_token,
    revoke_mcp_token,
    update_mcp_token_scopes,
)
from daimon.core.stores.security_audit import append_event
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


async def _audit_token_change(
    session: AsyncSession,
    *,
    command: str,
    reason: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    kind: McpTokenKind,
    jti: uuid.UUID,
) -> None:
    """Record a CLI change to a registered token, in the same transaction as the change."""
    await append_event(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        agent_id=None,
        platform=None,
        platform_user_id=None,
        tool_name=f"cli/{command}",
        operation=None,
        outcome="allowed",
        reason=reason,
        token_kind=kind,
        token_jti=jti,
    )


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
        tenant_id = await discover_tenant(
            session, override=override, workspace_id=rt.settings.cli.workspace_id
        )
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
        await _audit_token_change(
            session,
            command="mint-operator-token",
            reason="token_minted",
            tenant_id=tenant_id,
            account_id=identity.account_id,
            kind="operator",
            jti=token_jti(token),
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
            tenant_id = await discover_tenant(
                session, override=override, workspace_id=rt.settings.cli.workspace_id
            )
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
        await _audit_token_change(
            session,
            command="revoke-token",
            reason="token_revoked",
            tenant_id=row.tenant_id,
            account_id=row.account_id,
            kind=row.kind,
            jti=row.jti,
        )
    console.print(f"revoked {row.kind} token {row.jti}: its next request gets a 401")


async def set_token_scopes(
    *, rt: CliRuntime, console: Console, jti: uuid.UUID, scopes: list[str]
) -> None:
    """Narrow a live operator token to ``scopes``, which must all be on it already."""
    try:
        requested = parse_operator_scopes(scopes)
    except OperatorTokenError as exc:
        raise typer.BadParameter(str(exc)) from exc
    async with rt.sessionmaker() as session, session.begin():
        current = await lock_mcp_token(session, jti=jti)
        if current is None or current.kind != "operator" or current.revoked_at is not None:
            raise StoreError(f"no live operator token {jti}")
        try:
            validate_scope_narrowing(current=current.scopes, requested=requested)
        except OperatorTokenError as exc:
            raise typer.BadParameter(str(exc)) from exc
        row = await update_mcp_token_scopes(session, jti=jti, scopes=requested)
        assert row is not None, "the row is locked and was live a statement ago"
        await _audit_token_change(
            session,
            command="set-token-scopes",
            reason="token_scopes_narrowed",
            tenant_id=row.tenant_id,
            account_id=row.account_id,
            kind=row.kind,
            jti=row.jti,
        )
    console.print(f"token {row.jti} now has {', '.join(row.scopes)}; its next request uses them")


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
        ttl_days: Annotated[
            int, typer.Option("--ttl-days", help=f"Days until it expires, at most {MAX_TTL_DAYS}.")
        ] = 30,
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

    @app.command("set-token-scopes")
    def set_token_scopes_command(  # pyright: ignore[reportUnusedFunction]
        jti: Annotated[uuid.UUID, typer.Option("--jti", help="The operator token's jti.")],
        scope: Annotated[
            list[str],
            typer.Option("--scope", help="Repeat per scope to keep; others are removed."),
        ],
    ) -> None:
        """Narrow an operator token's scopes. It can only remove scopes, never add."""
        settings = load_settings()
        console = Console(highlight=False)

        async def _go() -> None:
            async with build_runtime(settings) as rt:
                await set_token_scopes(rt=rt, console=console, jti=jti, scopes=scope)

        run_cli(_go(), console=console)

    @app.command("revoke-token")
    def revoke_token_command(jti: uuid.UUID) -> None:  # pyright: ignore[reportUnusedFunction]
        """Revoke a registered MCP token by its jti. It stops working on its next request."""
        settings = load_settings()
        console = Console(highlight=False)

        async def _go() -> None:
            async with build_runtime(settings) as rt:
                await revoke_token(rt=rt, console=console, jti=jti)

        run_cli(_go(), console=console)
