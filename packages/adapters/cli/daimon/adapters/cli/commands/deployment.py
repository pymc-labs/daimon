"""daimon deployment ... sub-app: this install's own id."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.stores.deployment_identity import reset_deployment_id
from rich.console import Console

deployment_app = typer.Typer(help="This install's own id.")


def _run(command: Callable[[CliRuntime, Console], Awaitable[None]]) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await command(rt, console)

    run_cli(_with_runtime(), console=console)


@deployment_app.command("reset-id")
def deployment_reset_id_command(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation.")] = False,
) -> None:
    """Give a restored copy its own id, so it stops sharing this install's names.

    Run it on a staging copy restored from production before it serves a turn.
    Hosted OpenAI environments named under the old id are no longer adopted, so
    each tenant gets a new one on its next GPT turn. Restart running servers.
    """

    async def _go(rt: CliRuntime, console: Console) -> None:
        await deployment_reset_id(rt=rt, console=console, confirmed=yes)

    _run(_go)


async def deployment_reset_id(*, rt: CliRuntime, console: Console, confirmed: bool) -> None:
    if not confirmed and not typer.confirm(
        "Replace this install's id? Its hosted GPT environments are not reused."
    ):
        raise typer.Abort()
    async with rt.sessionmaker.begin() as session:
        fresh = await reset_deployment_id(session)
    console.print(f"New deployment id: {fresh}")
    console.print("Restart running servers so they read it.")
