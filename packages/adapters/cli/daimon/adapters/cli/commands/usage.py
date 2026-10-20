"""Operator usage commands: content-free turn telemetry, and the backlog settle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.core.config import load_settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.context_prompt import DEFAULT_FRAGMENTS
from daimon.core.db import build_engine, build_session_factory
from daimon.core.stores.accounting_outbox import list_pending_usage
from daimon.core.stores.turn_usage import list_turn_usage, usage_by_channel
from daimon.core.usage_sweep import settle_backlog
from rich.console import Console

usage_app = typer.Typer(help="Content-free usage telemetry and pending billing observations.")


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
        engine = build_engine(
            str(settings.database.url),
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_timeout=settings.database.pool_timeout,
        )
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


@usage_app.command("settle-backlog")
def usage_settle_backlog_command(
    internal: Annotated[
        list[UUID] | None,
        typer.Option(help="An internal tenant whose backlog is debited; repeat for each."),
    ] = None,
    session: Annotated[
        list[str] | None,
        typer.Option(help="Only these MA session ids (default: every MCP agent-chat session)."),
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Debit and waive. Without it this is a dry run.")
    ] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Settle the calls made before the settle path was deployed.

    An internal tenant's backlog is debited; every other tenant's is waived
    (its would-be debit logged, nothing charged). A dry run by default, which
    writes nothing. Prints a receipt: the cutoff, the mode, and per tenant the
    sessions, calls, raw cost, would-be debit, and what was debited (internal,
    applied) or waived (external). `--json` prints it as one JSON object to
    keep as evidence; the command writes no files.
    """
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        engine = build_engine(
            str(settings.database.url),
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_timeout=settings.database.pool_timeout,
        )
        try:
            async with AsyncAnthropic(
                api_key=settings.anthropic.resolved_api_key(),
                base_url=str(settings.anthropic.base_url),
                max_retries=MA_MAX_RETRIES,
            ) as client:
                receipt = await settle_backlog(
                    client,
                    build_session_factory(engine),
                    internal=frozenset(internal or ()),
                    markup=settings.billing.markup,
                    session_ids=session or (),
                    apply=apply,
                )
        finally:
            await engine.dispose()
        if as_json:
            console.print(receipt.model_dump_json(), soft_wrap=True, highlight=False, markup=False)
            return
        console.print(f"cutoff: {receipt.cutoff}  mode: {receipt.mode}", markup=False)
        emit_rows(
            console,
            receipt.tenants,
            columns=(
                "tenant_id",
                "kind",
                "sessions",
                "calls",
                "cost_usd",
                "would_debit_usd",
                "debited_usd",
                "waived_usd",
            ),
            as_json=False,
        )

    asyncio.run(run())


@usage_app.command("pending")
def usage_pending_command(
    tenant_id: UUID,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    cursor: Annotated[
        str | None, typer.Option(help="Continue from the previous next_cursor.")
    ] = None,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List billing observations awaiting usage, price or infrastructure reconciliation."""
    settings = load_settings()
    console = Console(highlight=False)

    async def query() -> None:
        engine = build_engine(
            str(settings.database.url),
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_timeout=settings.database.pool_timeout,
        )
        try:
            async with build_session_factory(engine)() as session:
                try:
                    page = await list_pending_usage(
                        session, tenant_id=tenant_id, limit=limit, cursor=cursor
                    )
                except ValueError as error:
                    raise typer.BadParameter(str(error)) from error
            if as_json:
                console.print(page.model_dump_json(), soft_wrap=True, highlight=False, markup=False)
                return
            emit_rows(
                console,
                page.rows,
                columns=(
                    "binding_id",
                    "session_id",
                    "observation_id",
                    "revision",
                    "model",
                    "completeness",
                    "reason",
                    "observed_at",
                ),
                as_json=False,
            )
            console.print(
                f"truncated: {str(page.truncated).lower()}  invalid_rows: {page.invalid_rows}",
                markup=False,
            )
            if page.next_cursor is not None:
                console.print(f"next_cursor: {page.next_cursor}", markup=False)
        finally:
            await engine.dispose()

    asyncio.run(query())
