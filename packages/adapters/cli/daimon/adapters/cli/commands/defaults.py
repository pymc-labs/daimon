from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.defaults.apply import apply_defaults
from daimon.core.defaults.report import ApplyReport, ResourceOutcome
from rich.console import Console
from rich.table import Table

defaults_app = typer.Typer(help="System defaults reconciliation.")


def _outcome_dict(o: ResourceOutcome) -> dict[str, str | None]:
    d = dataclasses.asdict(o)
    d["action"] = o.action.value
    return d


def _format_report_json(console: Console, report: ApplyReport) -> None:
    payload = {
        "agents": [_outcome_dict(o) for o in report.agents],
        "environments": [_outcome_dict(o) for o in report.environments],
        "skills": [_outcome_dict(o) for o in report.skills],
        "system_config": [_outcome_dict(o) for o in report.system_config],
    }
    console.print(json.dumps(payload), soft_wrap=True, highlight=False, markup=False)


def _format_report_table(console: Console, report: ApplyReport) -> None:
    table = Table(show_header=True, header_style="bold")
    for column in ("kind", "name", "action", "anthropic_id", "error"):
        table.add_column(column)
    for bucket in (report.agents, report.environments, report.skills, report.system_config):
        for o in bucket:
            table.add_row(o.kind, o.name, o.action.value, o.anthropic_id or "", o.error or "")
    console.print(table)
    counts: dict[str, int] = {}
    for o in (
        *report.agents,
        *report.environments,
        *report.skills,
        *report.system_config,
    ):
        counts[o.action.value] = counts.get(o.action.value, 0) + 1
    console.print("  ".join(f"{n} {k}" for k, n in sorted(counts.items())))


@defaults_app.command("apply")
def defaults_apply_command(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
    defaults_root: Annotated[Path, typer.Option("--defaults-root")] = Path("defaults"),
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await defaults_apply(
                rt=rt,
                console=console,
                dry_run=dry_run,
                as_json=as_json,
                defaults_root=defaults_root,
            )

    asyncio.run(_with_defaults())


async def defaults_apply(
    *,
    rt: CliRuntime,
    console: Console,
    dry_run: bool,
    as_json: bool,
    defaults_root: Path,
) -> None:
    public_url = str(rt.settings.mcp.public_url) if rt.settings.mcp.public_url is not None else None
    report = await apply_defaults(
        rt.sessionmaker,
        rt.anthropic,
        defaults_root,
        dry_run=dry_run,
        public_url=public_url,
    )
    if as_json:
        _format_report_json(console, report)
    else:
        _format_report_table(console, report)
    if report.is_failure():
        raise typer.Exit(code=1)
