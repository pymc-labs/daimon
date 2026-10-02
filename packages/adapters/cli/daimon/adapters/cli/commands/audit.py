"""Operator-only audit exports and retention maintenance."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.stores.security_audit import list_events, prune_events
from rich.console import Console

audit_app = typer.Typer(help="Read or expire tenant security audit events.")


@audit_app.command("list")
def audit_list_command(
    tenant: uuid.UUID,
    since: Annotated[
        str | None, typer.Option(help="Inclusive ISO-8601 timestamp with timezone.")
    ] = None,
    account: Annotated[
        uuid.UUID | None, typer.Option(help="Restrict a privacy export to this account.")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    parsed_since: datetime | None = None
    if since is not None:
        try:
            parsed_since = datetime.fromisoformat(since)
        except ValueError as exc:
            raise typer.BadParameter("--since must be an ISO-8601 timestamp") from exc
        if parsed_since.utcoffset() is None:
            raise typer.BadParameter("--since must include a timezone")
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt, rt.sessionmaker() as session:
            rows = await list_events(
                session,
                tenant_id=tenant,
                since=parsed_since,
                account_id=account,
                limit=limit,
                offset=offset,
            )
            emit_rows(
                console,
                rows,
                as_json=as_json,
                columns=(
                    "occurred_at",
                    "account_id",
                    "platform_user_id",
                    "agent_id",
                    "tool_name",
                    "operation",
                    "outcome",
                    "reason",
                    "token_kind",
                    "scope",
                ),
            )

    run_cli(run(), console=console)


@audit_app.command("prune")
def audit_prune_command(tenant: uuid.UUID) -> None:
    """Expire this tenant's events using security_audit_retention_days (default 90)."""
    settings = load_settings()
    console = Console(highlight=False)
    days = settings.security_audit_retention_days
    if days == 0:
        console.print("Audit retention is indefinite; no events removed.")
        return
    cutoff = datetime.now(UTC) - timedelta(days=days)

    async def run() -> None:
        async with build_runtime(settings) as rt, rt.sessionmaker() as session, session.begin():
            count = await prune_events(session, tenant_id=tenant, older_than=cutoff)
        console.print(f"Removed {count} expired audit events.")

    run_cli(run(), console=console)
