"""Operator-only context provider configuration for the provider hook."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.authz import Subject
from daimon.core.config import load_settings
from daimon.core.context_provider import ProviderConfig
from daimon.core.stores.context_providers import set_provider

context_provider_app = typer.Typer(help="Pre-turn context provider (operator only).")


@context_provider_app.command("set")
def set_command(
    config_file: Annotated[
        Path, typer.Argument(help="ProviderConfig JSON; secrets use bearer_env.")
    ],
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    channel: Annotated[str, typer.Option("--channel")] = "",
    agent: Annotated[str, typer.Option("--agent")] = "",
) -> None:
    """Set a tenant default or channel/agent override as the deployment operator.

    Like routines sandbox-tools, access to this local CLI is operator authority.
    No admin flag or actor identity is accepted from agent-supplied JSON.
    """
    config = ProviderConfig.model_validate_json(config_file.read_text())

    async def run() -> None:
        async with build_runtime(load_settings()) as rt, rt.sessionmaker.begin() as session:
            await set_provider(
                session,
                tenant_id=tenant,
                config=config,
                actor=Subject(is_admin=True),
                set_by="cli:deployment-operator",
                channel_id=channel,
                agent_id=agent,
            )
        typer.echo("Context provider setting saved.")

    asyncio.run(run())
