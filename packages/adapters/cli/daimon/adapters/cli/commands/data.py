"""Set up the data store, or repair it after a restore."""

from __future__ import annotations

import typer
from daimon.core.config import load_settings
from daimon.core.ports.gate import data_sources_on

data_app = typer.Typer(help="The data store (the data_sources feature).")


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
