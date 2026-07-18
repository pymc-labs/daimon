"""Routines admin CLI commands.

Currently exposes the one-shot `backfill-agent-names` migration bridge for
Between migration 0011 (nullable `agent_name`) and the
forthcoming NOT NULL flip, this command walks every routine whose
`agent_name IS NULL`, retrieves the agent from MA, and writes the daimon-tag
into the row.

Routine CRUD itself is exposed through the MCP server (`/agent-setup` + chat),
not the CLI.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import structlog
import typer
from anthropic import APIStatusError
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.stores.routines import (
    list_routines_missing_agent_name,
    update_routine,
)
from rich.console import Console

_log = structlog.get_logger(__name__)

routines_app = typer.Typer(help="Routines admin tasks.")

_FALLBACK_NAME = "daimon"


@routines_app.command("backfill-agent-names")
def backfill_agent_names_command(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Populate ``routines.agent_name`` from MA agent metadata.

    Idempotent — skips rows where ``agent_name`` is already non-NULL.
    Falls back to ``"daimon"`` when MA returns 404 OR the agent is archived
    OR the agent's metadata lacks ``daimon_name`` (only one
    daimon-tagged agent per tenant today).

    Non-404 ``APIStatusError`` (5xx, network) propagates; the operator re-runs
    after MA recovers.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await run_backfill_agent_names(rt=rt, console=console, dry_run=dry_run)

    asyncio.run(_with_runtime())


async def run_backfill_agent_names(
    *,
    rt: CliRuntime,
    console: Console,
    dry_run: bool,
) -> None:
    """Behavior body, callable by tests with an injected CliRuntime.

    Mirrors the `defaults_apply(rt=...)` split in `commands/defaults.py` so
    tests don't need to round-trip through `asyncio.run` + `load_settings`.
    """
    async with rt.sessionmaker() as session:
        rows = await list_routines_missing_agent_name(session)

    from_metadata = 0
    fallback = 0

    for row in rows:
        resolved: str
        try:
            agent = await rt.anthropic.beta.agents.retrieve(row.agent_id)
        except APIStatusError as err:
            if err.status_code == 404:
                resolved = _FALLBACK_NAME
                fallback += 1
            else:
                raise
        else:
            tag = agent.metadata.get(MA_METADATA_KEY_NAME)
            if agent.archived_at is not None or tag is None:
                resolved = _FALLBACK_NAME
                fallback += 1
            else:
                resolved = tag
                from_metadata += 1

        if dry_run:
            _log.info(
                "backfill.dry_run",
                routine_id=str(row.id),
                agent_id=row.agent_id,
                resolved_agent_name=resolved,
            )
            continue

        # Per-row tx — canonical async sessionmaker pattern.
        async with rt.sessionmaker() as s, s.begin():
            await update_routine(s, row.id, agent_name=resolved)

    console.print(
        f"Backfilled {len(rows)} routine(s) "
        f"({from_metadata} from metadata, {fallback} fallback to {_FALLBACK_NAME!r})."
        + (" [dry-run, no writes]" if dry_run else "")
    )
