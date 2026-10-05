"""Operator command for creating tenant GitHub connection invitations."""

from __future__ import annotations

import getpass
import uuid
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.core.config import load_settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.stores.github_connect import cli_account_id, mint_invitation
from rich.console import Console

github_app = typer.Typer(help="GitHub App connection commands.")


@github_app.command("connect-link")
def connect_link(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant", help="Destination workspace UUID.")],
) -> None:
    """Print a single-use connection invitation for a tenant admin."""
    settings = load_settings()
    config = settings.github_app
    root = settings.mcp.app_root_url
    if (
        root is None
        or config.app_id is None
        or config.app_slug is None
        or config.private_key is None
        or config.client_id is None
        or config.client_secret is None
        or not settings.crypto.keys
    ):
        raise typer.BadParameter("GitHub connection is not configured")
    console = Console(highlight=False)

    async def _run() -> None:
        engine = build_engine(str(settings.database.url))
        try:
            sessionmaker = build_session_factory(engine)
            async with sessionmaker.begin() as session:
                account_id = await cli_account_id(
                    session, tenant_id=tenant, os_user=getpass.getuser()
                )
                if account_id is None:
                    raise ValueError("current CLI user has no account in this workspace")
                token = await mint_invitation(
                    session,
                    tenant_id=tenant,
                    requester_account_id=account_id,
                    requester_label=getpass.getuser(),
                )
            console.print(f"{root}/oauth/github/connect/{token}")
        finally:
            await engine.dispose()

    run_cli(_run(), console=console)
