"""`daimon data eval` (search quality on a gold set) and `daimon data probe` (read-only checks)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, cast
from uuid import UUID

import typer
from daimon.core.config import load_settings
from daimon.core.ports.data_metering import SessionMeter
from daimon.core.ports.gate import data_sources_on
from daimon.core.pricing import side_call_rates


def _price(model: str, units: int) -> float:
    """USD for `units` input tokens of a model; 0 when the rate table does not price it."""
    try:
        return side_call_rates(model).input * units / 1_000_000
    except KeyError:
        return 0.0


def eval_command(
    gold: Annotated[
        list[Path],
        typer.Argument(exists=True, dir_okay=False, help="Gold files (JSON or YAML)."),
    ],
    out: Annotated[Path | None, typer.Option("--out", help="Write the JSON report here.")] = None,
    max_usd: Annotated[
        float, typer.Option("--max-usd", help="Refuse above this estimate; stop at this spend.")
    ] = 1.0,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Run only the first N questions.")
    ] = None,
) -> None:
    """Grade search on gold questions: recall and nDCG per arm, on what each caller can read."""
    settings = load_settings()
    if not data_sources_on(settings):
        typer.echo("Data sources are off (DAIMON_FEATURES__DATA_SOURCES). Nothing to do.")
        return
    from daimon.data import pool
    from daimon.data.evals import gold as golds
    from daimon.data.evals.run import Budget, estimate, evaluate
    from daimon.data.metering import Meter
    from daimon.data.schema import Schemas

    questions = [q for path in gold for q in golds.load(path).questions][:limit]
    cost = estimate(questions, settings.data, _price)
    typer.echo(f"{len(questions)} questions; estimated spend up to ${cost:.6f}; cap ${max_usd:.2f}")
    if cost > max_usd:
        typer.echo("Refused: the estimate is over --max-usd.", err=True)
        raise typer.Exit(1)

    async def run() -> dict[str, object]:
        meter = SessionMeter.of(settings)
        try:
            async with pool.connection(settings) as conn:
                return await evaluate(
                    conn,
                    Schemas.of(settings.data),
                    settings.data,
                    golds.GoldSet(questions=questions),
                    meter=cast(Meter, meter),
                    budget=Budget(max_usd, _price),
                    estimated=cost,
                )
        finally:
            await meter.close()
            await pool.close_pools()

    report = asyncio.run(run())
    if out is not None:
        out.write_text(json.dumps(report, indent=2, default=str) + "\n", "utf-8")
    typer.echo(report["text"])


def probe_command(
    name: Annotated[str, typer.Argument(help="grants, scopes, person, health or drift.")],
    tenant: Annotated[UUID, typer.Option("--tenant", help="The tenant to probe.")],
    account: Annotated[
        UUID | None, typer.Option("--account", help="The person (probe `person`).")
    ] = None,
) -> None:
    """Read-only checks of the store: grants, scopes, what a person reads, source health, drift."""
    settings = load_settings()
    if not data_sources_on(settings):
        typer.echo("Data sources are off (DAIMON_FEATURES__DATA_SOURCES). Nothing to do.")
        return
    from daimon.data import pool, probes
    from daimon.data.schema import Schemas

    async def run() -> dict[str, object]:
        try:
            async with pool.connection(settings) as conn:
                return await probes.run(name, conn, Schemas.of(settings.data), tenant, account)
        finally:
            await pool.close_pools()

    try:
        report = asyncio.run(run())
    except ValueError as err:
        raise typer.BadParameter(str(err)) from err
    typer.echo(json.dumps(report, indent=2, default=str))


def register(app: typer.Typer) -> None:
    """Add the two commands to the `data` group."""
    app.command("eval")(eval_command)
    app.command("probe")(probe_command)
