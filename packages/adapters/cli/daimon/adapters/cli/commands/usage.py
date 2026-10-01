"""Operator queries for content-free turn usage; no upstream API calls."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import typer
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.core.config import load_settings
from daimon.core.context_prompt import DEFAULT_FRAGMENTS
from daimon.core.db import build_engine, build_session_factory
from daimon.core.stores.turn_usage import list_turn_usage, usage_by_channel
from rich.console import Console

usage_app = typer.Typer(help="Content-free turn telemetry (measurement only).")


@usage_app.command("turns")
def usage_turns_command(
    tenant_id: UUID,
    days: Annotated[int, typer.Option(min=1, max=3650)] = 7,
    channel: Annotated[str | None, typer.Option(help="Filter by channel ID.")] = None,
    origin: Annotated[str | None, typer.Option(help="chat, routine, relay or handoff")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    summary: Annotated[bool, typer.Option(help="Group by platform, channel and origin.")] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Query one tenant's recent outcomes and usage, or aggregate by channel."""
    if origin is not None and origin not in DEFAULT_FRAGMENTS:
        raise typer.BadParameter("origin must be chat, routine, relay or handoff")
    settings = load_settings()
    console = Console(highlight=False)

    async def query() -> None:
        engine = build_engine(str(settings.database.url))
        try:
            sm = build_session_factory(engine)
            async with sm() as session:
                since = datetime.now(UTC) - timedelta(days=days)
                if summary:
                    rows = await usage_by_channel(
                        session, tenant_id=tenant_id, since=since, channel_id=channel, origin=origin
                    )
                    emit_rows(
                        console,
                        rows,
                        columns=(
                            "platform",
                            "channel_id",
                            "origin",
                            "turns",
                            "measured_turns",
                            "input_tokens",
                            "output_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                            "cost_usd",
                            "known_cost_usd",
                            "unpriced_calls",
                        ),
                        as_json=as_json,
                    )
                else:
                    turns = await list_turn_usage(
                        session,
                        tenant_id=tenant_id,
                        since=since,
                        channel_id=channel,
                        origin=origin,
                        limit=limit,
                    )
                    emit_rows(
                        console,
                        turns,
                        columns=(
                            "id",
                            "started_at",
                            "channel_id",
                            "origin",
                            "reason",
                            "model_calls",
                            "input_tokens",
                            "output_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                            "cost_usd",
                            "billing_posture",
                        ),
                        as_json=as_json,
                    )
        finally:
            await engine.dispose()

    asyncio.run(query())
