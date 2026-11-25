"""Set up the data store, or repair it after a restore."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

import typer
from daimon.adapters.cli.commands.data_evals import register
from daimon.core.config import load_settings
from daimon.core.ports.gate import data_sources_on

data_app = typer.Typer(help="The data store (the data_sources feature).")
register(data_app)


@data_app.command("init")
def init_command(
    repair: bool = typer.Option(
        False, "--repair", help="Also put back view ownership and grants lost in a restore."
    ),
    admin_url: str | None = typer.Option(
        None,
        "--admin-url",
        help=(
            "An administrator's database URL, to make the roles and extensions the app's "
            "own user cannot; the store stays the app user's."
        ),
    ),
) -> None:
    """Create the store's schemas, roles and tables; safe to run again."""
    settings = load_settings()
    if not data_sources_on(settings):
        typer.echo("Data sources are off (DAIMON_FEATURES__DATA_SOURCES). Nothing to do.")
        return
    from daimon.data.init import init_data

    outcome = init_data(
        str(settings.database.url), settings.data, repair=repair, admin_url=admin_url
    )
    if outcome.missing:
        typer.echo(f"The data store needs {'; '.join(outcome.missing)}.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Data store ready; applied {len(outcome.applied)} new file(s).")


@data_app.command("access-check")
def access_check_command(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant", help="The workspace's id.")],
    source: Annotated[uuid.UUID, typer.Option("--source", help="The source's id.")],
    record: Annotated[
        str | None, typer.Option("--record", help="A record's id in that source.")
    ] = None,
    account: Annotated[
        uuid.UUID | None,
        typer.Option("--account", help="Also say whether this account can read the record now."),
    ] = None,
) -> None:
    """Show who can read a source or a record, and why."""
    settings = load_settings()
    if not data_sources_on(settings):
        typer.echo("Data sources are off (DAIMON_FEATURES__DATA_SOURCES). Nothing to do.")
        return

    async def run() -> list[str]:
        from daimon.data import access_check, pool
        from daimon.data.schema import Schemas

        async with pool.connection(settings) as conn:
            return await access_check.check(
                conn,
                Schemas.of(settings.data),
                tenant,
                source,
                record=record,
                account=account,
            )

    for line in asyncio.run(run()):
        typer.echo(line)
