"""Operator checks for encryption at rest of agent keys."""

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.stores.agent_files import (
    count_plaintext_agent_files,
    count_undecryptable_agent_files,
    encrypt_plaintext_agent_files,
)
from rich.console import Console

crypto_app = typer.Typer(help="Verify and enforce encryption of stored agent keys.")


@crypto_app.command("verify")
def crypto_verify_command() -> None:
    """Fail unless DAIMON_CRYPTO__KEYS is set and every agent key is encrypted and readable.

    Prints counts per tenant only, never key names or values.
    """
    settings = load_settings()
    console = Console(highlight=False)
    failed = False

    async def run() -> None:
        nonlocal failed
        if not settings.crypto.keys:
            console.print("[red]✗ DAIMON_CRYPTO__KEYS is not set.[/red]")
            failed = True
        async with build_runtime(settings) as rt, rt.sessionmaker() as session:
            counts = await count_plaintext_agent_files(session)
            unreadable = await count_undecryptable_agent_files(session)
        total = sum(counts.values())
        if total:
            failed = True
            console.print(
                f"[red]✗ {total} agent key(s) stored in plaintext across "
                f"{len(counts)} tenant(s).[/red] Run `daimon crypto encrypt-plaintext`."
            )
            for tenant_id, count in sorted(counts.items(), key=lambda item: str(item[0])):
                console.print(f"  {tenant_id}: {count}")
        unreadable_total = sum(unreadable.values())
        if unreadable_total:
            failed = True
            console.print(
                f"[red]✗ {unreadable_total} encrypted agent key(s) can't be decrypted with "
                f"DAIMON_CRYPTO__KEYS across {len(unreadable)} tenant(s).[/red] "
                "Restore the retired key to the list."
            )
            for tenant_id, count in sorted(unreadable.items(), key=lambda item: str(item[0])):
                console.print(f"  {tenant_id}: {count}")
        if not failed:
            console.print(
                "✓ Encryption keys set; every agent key is encrypted and can be decrypted."
            )

    run_cli(run(), console=console)
    if failed:
        raise typer.Exit(code=1)


@crypto_app.command("encrypt-plaintext")
def crypto_encrypt_plaintext_command() -> None:
    """Encrypt every plaintext agent key in place with the first DAIMON_CRYPTO__KEYS key."""
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt, rt.sessionmaker() as session, session.begin():
            count = await encrypt_plaintext_agent_files(session)
        console.print(f"Encrypted {count} agent key(s).")

    run_cli(run(), console=console)
