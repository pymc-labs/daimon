"""Deployment operator configuration for native tenant lead intake."""

from pathlib import Path
from uuid import UUID

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.github_credentials import build_multifernet
from daimon.core.lead_intake import IntakeConfig
from daimon.core.mcp_auth import read_private_credential_bytes
from daimon.core.stores.lead_intake import IntakeStore
from pydantic import TypeAdapter
from rich.console import Console

leads_app = typer.Typer(help="Configure tenant lead intake and encrypted integration credentials.")


@leads_app.command("configure")
def configure(tenant_id: UUID, config_file: Path, credentials_file: Path) -> None:
    """Load tenant configuration and credentials from private operator-owned JSON files.

    Starts in capture unless the config explicitly names active or crm_paused.
    Reuse the revision for a mode-only cutover; increment it for destination or
    connection changes. Never paste integration credentials into chat.
    """
    settings = load_settings()
    console = Console(highlight=False)
    # Private-file validation precedes parsing; errors never print field values.
    try:
        config = IntakeConfig.model_validate_json(read_private_credential_bytes(config_file))
        raw = TypeAdapter(dict[str, str]).validate_json(
            read_private_credential_bytes(credentials_file), strict=True
        )
        if "hubspot_access_token" not in raw or not (
            "gmail_access_token" in raw or "gmail_refresh_token" in raw
        ):
            raise ValueError("missing credentials")
    except (ValueError, OSError):
        console.print("Invalid private intake configuration or credential file.")
        raise typer.Exit(2) from None

    async def run() -> None:
        async with build_runtime(settings) as runtime:
            fernet = build_multifernet(
                tuple(key.get_secret_value() for key in settings.crypto.keys)
            )
            store = IntakeStore(runtime.sessionmaker, fernet, tenant_id)
            await store.configure(config, raw)
        console.print(f"Lead intake configuration saved for {tenant_id}: {config.mode}.")

    run_cli(run(), console=console)


@leads_app.command("adopt-config")
def adopt_config(tenant_id: UUID, intake_id: UUID, revision: int) -> None:
    """Explicitly adopt an operator-reviewed configuration revision for unfinished work.

    This records the old/new revision in encrypted audit and performs no
    provider writes. Review the connections before invoking it. A revision with
    another sales channel or guild is refused: a lead keeps the readers of the
    channel it was captured for.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as runtime:
            fernet = build_multifernet(
                tuple(key.get_secret_value() for key in settings.crypto.keys)
            )
            store = IntakeStore(runtime.sessionmaker, fernet, tenant_id)
            await store.adopt(intake_id, revision)
        console.print(
            f"Intake {intake_id} adopted configuration revision {revision}; resume separately."
        )

    run_cli(run(), console=console)
