"""`daimon mcp …` — operator primitives for the Phase 2 MCP adapter.

Subcommands:
  * mint-token         — signs a JWT for local debug / integration tests.
  * url                — prints DAIMON_MCP__PUBLIC_URL.
  * janitor            — find and optionally archive orphan daimon-mcp:* vaults.
  * sweep-credentials  — delete+recreate stale is_admin creds (defense-in-depth).

No `serve` subcommand; deployment invokes uvicorn against
`daimon.adapters.mcp.server:create_mcp_app` directly.
"""

from __future__ import annotations

import datetime as dt

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import Settings, load_settings
from daimon.core.errors import ConfigError
from daimon.core.mcp_auth import mint_jwt
from daimon.core.mcp_credential_sweep import sweep_stale_admin_credentials
from daimon.core.mcp_vault_janitor import archive_orphan_mcp_vaults
from daimon.core.stores.identity import get_or_create_cli_principal
from rich.console import Console
from rich.table import Table

mcp_app = typer.Typer(help="Operator commands for the daimon-mcp adapter.")


@mcp_app.command("url")
def mcp_url_command() -> None:
    settings = load_settings()
    console = Console(highlight=False)
    run_cli(mcp_url(settings=settings), console=console)


async def mcp_url(*, settings: Settings) -> None:
    if settings.mcp.public_url is None:
        raise ConfigError(
            "DAIMON_MCP__PUBLIC_URL is unset. Set it to the URL MA will dial, "
            "e.g. https://daimon-mcp.example.com/mcp"
        )
    typer.echo(str(settings.mcp.public_url))


@mcp_app.command("mint-token")
def mcp_mint_token_command(
    os_user: str | None = typer.Option(
        None,
        "--os-user",
        help="OS user to mint a token for. Defaults to settings.cli.local_user.",
    ),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await mint_token(rt=rt, os_user=os_user)

    run_cli(_with_defaults(), console=console)


async def mint_token(*, rt: CliRuntime, os_user: str | None) -> None:
    if rt.settings.mcp.jwt_secret is None:
        raise ConfigError(
            "DAIMON_MCP__JWT_SECRET is unset. Generate one with "
            "`python -c 'import secrets; print(secrets.token_hex(32))'`."
        )
    resolved_user = os_user or rt.settings.cli.local_user
    async with (
        rt.sessionmaker() as session,
        session.begin(),
    ):
        tenant_id = await discover_tenant(session)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=resolved_user
        )
    token = mint_jwt(
        account_id=principal.account_id,
        secret=rt.settings.mcp.jwt_secret.get_secret_value().encode(),
        now=dt.datetime.now(dt.UTC),
    )
    typer.echo(token)


@mcp_app.command(
    "janitor",
    help=(
        "Find (and optionally archive) orphan per-account daimon-mcp:* vaults — "
        "vaults whose account_id is not in the local accounts table."
    ),
)
def mcp_janitor_command(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually archive the orphans. Default is dry-run (read-only).",
    ),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await mcp_janitor(rt=rt, console=console, apply=apply)

    run_cli(_with_runtime(), console=console)


async def mcp_janitor(*, rt: CliRuntime, console: Console, apply: bool) -> None:
    report = await archive_orphan_mcp_vaults(
        rt.anthropic, session_factory=rt.sessionmaker, dry_run=not apply
    )
    table = Table(show_header=True, header_style="bold")
    for col in ("vault_id", "status"):
        table.add_column(col)
    for vid in report.orphan_vault_ids:
        status = "archived" if vid in report.archived_vault_ids else "orphan (dry-run)"
        table.add_row(vid, status)
    for vid in report.unparseable_vault_ids:
        table.add_row(vid, "unparseable display_name (left alone)")
    console.print(table)
    summary = (
        f"{len(report.archived_vault_ids)} archived, "
        f"{len(report.orphan_vault_ids) - len(report.archived_vault_ids)} pending, "
        f"{len(report.unparseable_vault_ids)} unparseable"
    )
    if not apply and report.orphan_vault_ids:
        console.print(f"[yellow]{summary} — re-run with --apply to archive[/yellow]")
    else:
        console.print(summary)


@mcp_app.command(
    "sweep-credentials",
    help=(
        "Delete+recreate stale is_admin=True daimon-mcp static_bearer credentials "
        "(defense-in-depth / dormant-cred cleanup). "
        "Matches ONLY the static_bearer at the current public_url — never Copilot or "
        "user-added external creds. "
        "Default is dry-run (read-only); pass --apply to mutate. "
        "NOTE: this sweep is NOT the fix for the #162 escalation — that is closed at "
        "the 88-03 gate. This sweep aligns the existing credential fleet with the "
        "post-88-03 no-is_admin invariant."
    ),
)
def mcp_sweep_credentials_command(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually delete+recreate the stale creds. Default is dry-run (read-only).",
    ),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await mcp_sweep_credentials(rt=rt, console=console, apply=apply)

    run_cli(_with_runtime(), console=console)


async def mcp_sweep_credentials(*, rt: CliRuntime, console: Console, apply: bool) -> None:
    """Async body for `daimon mcp sweep-credentials`.

    Reads jwt_secret + public_url from settings.mcp; raises ConfigError if
    either is unset. Calls sweep_stale_admin_credentials and renders a Rich
    table of (vault_id, cred status) + summary line.
    """
    if rt.settings.mcp.public_url is None:
        raise ConfigError(
            "DAIMON_MCP__PUBLIC_URL is unset. Set it to the current MCP endpoint URL, "
            "e.g. https://daimon-mcp.example.com/mcp"
        )
    if rt.settings.mcp.jwt_secret is None:
        raise ConfigError(
            "DAIMON_MCP__JWT_SECRET is unset. Generate one with "
            "`python -c 'import secrets; print(secrets.token_hex(32))'`."
        )

    public_url = str(rt.settings.mcp.public_url)
    jwt_secret = rt.settings.mcp.jwt_secret.get_secret_value().encode()

    report = await sweep_stale_admin_credentials(
        rt.anthropic,
        jwt_secret=jwt_secret,
        public_url=public_url,
        now=dt.datetime.now(dt.UTC),
        dry_run=not apply,
    )

    table = Table(show_header=True, header_style="bold")
    for col in ("vault_id", "old_cred_id", "status"):
        table.add_column(col)
    for vault_id, old_cred_id in report.swept_pairs:
        status = "recreated (is_admin removed)" if apply else "planned (dry-run)"
        table.add_row(vault_id, old_cred_id, status)
    for vid in report.unparseable_vault_ids:
        table.add_row(vid, "—", "unparseable display_name (left alone)")
    console.print(table)

    swept = len(report.swept_pairs)
    unparseable = len(report.unparseable_vault_ids)
    summary = f"{swept} swept, {unparseable} unparseable"
    if not apply and report.swept_pairs:
        console.print(f"[yellow]{summary} — re-run with --apply to delete+recreate[/yellow]")
    else:
        console.print(summary)
