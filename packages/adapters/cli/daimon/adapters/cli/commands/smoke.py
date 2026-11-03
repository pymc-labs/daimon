"""`daimon smoke` — post-deploy pipeline verification."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.runtime import build_runtime
from daimon.core.config import load_settings
from daimon.core.smoke import SMOKE_TIMEOUT_S, run_smoke_check
from rich.console import Console


def smoke_command(
    deploy_sha: Annotated[
        str | None,
        typer.Option(
            "--deploy-sha",
            help="CI-supplied full commit SHA, used to derive the top-up idempotency key.",
        ),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Seconds to bound the smoke turn (D-04)."),
    ] = SMOKE_TIMEOUT_S,
    defaults_root: Annotated[
        Path,
        typer.Option(
            "--defaults-root",
            help="Defaults tree to reconcile the smoke tenant against.",
        ),
    ] = Path("defaults"),
) -> None:
    """Provision and reconcile the dedicated smoke tenant, run one billed
    headless turn, and exit nonzero unless a `usage_events` row was written.

    This is the post-deploy pipeline verification (RS-01): it proves the
    deployed environment can actually run and bill a turn, not a capability
    test of any particular agent behavior.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_smoke() -> None:
        async with build_runtime(settings) as rt:
            agent_tag = rt.deployment_default.agent_name or "daimon"
            environment_tag = rt.deployment_default.environment_name or "default"

            # The ledger idempotency key must be unique per top-up opportunity —
            # a constant fallback would mean a drained tenant could never be
            # re-credited on a second manual run, since insert_entry is
            # ON CONFLICT DO NOTHING.
            topup_key = deploy_sha or dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")

            def on_stage(stage: str) -> None:
                console.print(f"smoke: {stage}")

            result = await run_smoke_check(
                session_factory=rt.sessionmaker,
                anthropic=rt.anthropic,
                defaults_root=defaults_root,
                agent_tag=agent_tag,
                environment_tag=environment_tag,
                resolver_cache=rt.resolver_cache,
                topup_key=topup_key,
                markup=rt.settings.billing.markup,
                tool_safety=rt.settings.tool_safety,
                timeout_s=timeout,
                on_stage=on_stage,
            )
            console.print(
                "smoke: OK "
                f"agent_id={result.agent_id} environment_id={result.environment_id} "
                f"usage_event_count={result.usage_event_count}"
            )

    run_cli(_with_smoke(), console=console)
