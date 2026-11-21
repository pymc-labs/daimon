"""Workflows operator CLI: list and show them, move their files, look after runs, and move
routines onto workflows and back.

Every command acts in one `--tenant` as the deployment operator, who holds a workspace
admin's rights there, through the same service calls the web app makes. `import` acts as
the `--owner` it names, who owns the new draft. Each refuses while `workflows` is Off.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.errors import UserFacingError
from daimon.core.stores.accounts import get_account
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.routines import list_routines_for_tenant
from daimon.core.stores.tenants import get_tenant
from daimon.core.workflows import convert, exchange, run_views, service
from daimon.core.workflows.access import Who, who_of
from daimon.core.workflows.service import Env, NewWorkflow, WorkflowError
from pydantic import BaseModel
from rich.console import Console

workflows_app = typer.Typer(help="Workflows: list, show, move files, look after runs, convert.")

Tenant = Annotated[uuid.UUID, typer.Option("--tenant", help="The workspace's tenant uuid.")]
Version = Annotated[
    int | None, typer.Option("--version", help="A published version; default is the draft.")
]


class _RunLine(BaseModel):
    id: str
    workflow: str
    number: int
    status: str
    label: str
    started_at: str
    cost_usd: float


async def operator_env(
    rt: CliRuntime, tenant_id: uuid.UUID, *, owner: uuid.UUID | None = None
) -> Env:
    """The env a command acts in: the operator as a workspace admin, or `owner` as
    themselves. Refuses an unknown tenant, an owner not on it, and workflows Off."""
    async with rt.sessionmaker.begin() as session:
        if await get_tenant(session, tenant_id) is None:
            raise UserFacingError(f"No tenant {tenant_id}. See `daimon tenants list`.")
        if owner is None:
            principal = await get_or_create_cli_principal(
                session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
            )
            who: Who | None = Who(tenant_id, principal.account_id, admin=True)
        else:
            account = await get_account(session, owner)
            on_tenant = account is not None and account.tenant_id == tenant_id
            who = await who_of(session, tenant_id, owner) if on_tenant else None
    if who is None:
        raise UserFacingError(f"Account {owner} is not an active member of this tenant.")
    env = Env(sm=rt.sessionmaker, settings=rt.settings, who=who, now=datetime.now(UTC))
    await _say(service.ensure_on(env))
    return env


async def _say[T](call: Awaitable[T]) -> T:
    """The service's refusal, in its own words, as the CLI's error."""
    try:
        return await call
    except WorkflowError as err:
        raise UserFacingError(str(err)) from err


async def list_workflows(
    rt: CliRuntime, console: Console, tenant_id: uuid.UUID, *, as_json: bool
) -> None:
    env = await operator_env(rt, tenant_id)
    rows = await _say(service.list_workflows(env))
    columns = ("id", "name", "status", "live_version", "next_run", "spend_usd")
    emit_rows(console, rows, columns=columns, as_json=as_json)


async def show_workflow(
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    version: int | None,
) -> None:
    env = await operator_env(rt, tenant_id)
    workflow = await _say(service.get(env, workflow_id))
    text, _ = await _say(service.export(env, workflow_id, "yaml", version))
    live = f"v{workflow.live_version}" if workflow.live_version else "none"
    runs_as = workflow.run_as.name if workflow.run_as else "nobody yet"
    console.print(
        f"{workflow.name} ({workflow.status}). Live version: {live}. Runs as {runs_as}. "
        f"Owner: {workflow.owner.name}.",
        markup=False,
        highlight=False,
    )
    console.print(text, markup=False, highlight=False, soft_wrap=True)


async def export_workflow(
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID,
    *,
    version: int | None,
    fmt: exchange.Format,
    out: Path | None,
) -> None:
    env = await operator_env(rt, tenant_id)
    text, name = await _say(service.export(env, workflow_id, fmt, version))
    if out is None:
        console.print(text, markup=False, highlight=False, soft_wrap=True, end="")
        return
    target = out / name if out.is_dir() else out
    target.write_text(text, encoding="utf-8")
    console.print(f"Wrote {target}.", markup=False, highlight=False)


async def import_workflow(
    rt: CliRuntime, console: Console, tenant_id: uuid.UUID, path: Path, owner: uuid.UUID
) -> uuid.UUID:
    env = await operator_env(rt, tenant_id, owner=owner)
    body = NewWorkflow.model_validate({"from": "import", "yaml": path.read_text(encoding="utf-8")})
    made = await _say(service.create(env, body, rt.settings.defaults_root))
    draft = await _say(service.get_draft(env, uuid.UUID(made.id)))
    errors = len(draft.validation.errors)
    console.print(
        f"Imported {made.name!r} as a draft, {made.id}. "
        + (f"It has {errors} problem(s) to fix before publishing." if errors else "It validates."),
        markup=False,
        highlight=False,
    )
    return uuid.UUID(made.id)


async def list_runs(
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    workflow_id: uuid.UUID | None,
    *,
    limit: int,
    as_json: bool,
) -> None:
    env = await operator_env(rt, tenant_id)
    found = await _say(service.list_runs(env, workflow_id, limit=limit))
    rows = [
        _RunLine(
            id=r.id,
            workflow=r.workflow.get("name", ""),
            number=r.number,
            status=r.status + (" (test)" if r.test else ""),
            label=r.label or "",
            started_at=r.started_at,
            cost_usd=r.cost_usd,
        )
        for r in found
    ]
    emit_rows(console, rows, columns=tuple(_RunLine.model_fields), as_json=as_json)


async def control_run(
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    action: Callable[[Env, uuid.UUID], Awaitable[run_views.RunSummary]],
) -> None:
    """Cancel or redrive one run; prints where the run is now."""
    env = await operator_env(rt, tenant_id)
    run = await _say(action(env, run_id))
    console.print(f"Run {run.number}: {run.status}.", markup=False, highlight=False)


async def verify_run(
    rt: CliRuntime, console: Console, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> bool:
    env = await operator_env(rt, tenant_id)
    checked = await _say(service.verify_run(env, run_id))
    if checked.ok:
        console.print(f"The ledger holds: {checked.events} events, none changed.", highlight=False)
    else:
        console.print(f"The ledger does not verify ({checked.events} events).", style="red")
    return checked.ok


async def from_routine(
    rt: CliRuntime,
    console: Console,
    tenant_id: uuid.UUID,
    routine_ids: list[uuid.UUID],
    *,
    apply: bool,
) -> list[uuid.UUID]:
    """Preview each routine's workflow (every routine when none is named), or, with `apply`,
    move each named one. Returns the workflows made."""
    if apply and not routine_ids:
        raise UserFacingError("Name each routine to move with --routine. Nothing was changed.")
    env = await operator_env(rt, tenant_id)
    if not routine_ids:
        async with rt.sessionmaker() as session:
            routine_ids = [
                r.id for r in await list_routines_for_tenant(session, tenant_id=tenant_id)
            ]
    made: list[uuid.UUID] = []
    for routine_id in routine_ids:
        if apply:
            made.append(await _say(convert.from_routine(env, routine_id)))
            console.print(f"Routine {routine_id} moved to workflow {made[-1]}.", highlight=False)
            continue
        try:
            plan = await convert.preview(env, routine_id)
        except WorkflowError as err:
            console.print(f"Routine {routine_id} stays: {err}", markup=False, highlight=False)
            continue
        row = plan.routine
        console.print(
            f"Routine {routine_id} would move"
            + (" paused" if plan.paused else "")
            + f". Next fire {row.next_fire_at}, last slot {row.last_fired_slot}.",
            markup=False,
            highlight=False,
        )
        console.print(exchange.dump(plan.spec, "yaml"), markup=False, highlight=False)
    if not apply:
        console.print("Preview only. Add --apply and --routine to move.", highlight=False)
    return made


async def to_routine(
    rt: CliRuntime, console: Console, tenant_id: uuid.UUID, workflow_id: uuid.UUID
) -> uuid.UUID:
    env = await operator_env(rt, tenant_id)
    routine_id = await _say(convert.to_routine(env, workflow_id))
    console.print(
        f"Routine {routine_id} has its schedule back; the workflow is paused.", highlight=False
    )
    return routine_id


def _run(make: Callable[[CliRuntime, Console], Awaitable[object]]) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt:
            await make(rt, console)

    run_cli(run(), console=console)


@workflows_app.command("list")
def list_command(tenant: Tenant, as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    """The tenant's workflows: status, live version, next run and spend this month."""
    _run(lambda rt, c: list_workflows(rt, c, tenant, as_json=as_json))


@workflows_app.command("show")
def show_command(workflow: uuid.UUID, tenant: Tenant, version: Version = None) -> None:
    """One workflow's status and who it runs as, then its draft (or a version) as YAML."""
    _run(lambda rt, c: show_workflow(rt, c, tenant, workflow, version))


@workflows_app.command("export")
def export_command(
    workflow: uuid.UUID,
    tenant: Tenant,
    version: Version = None,
    fmt: Annotated[exchange.Format, typer.Option("--format")] = "yaml",
    out: Annotated[
        Path | None, typer.Option("--out", help="A file, or a folder for the usual file name.")
    ] = None,
) -> None:
    """Write the draft (or a version) as a YAML or JSON file, or to the screen."""
    _run(lambda rt, c: export_workflow(rt, c, tenant, workflow, version=version, fmt=fmt, out=out))


@workflows_app.command("import")
def import_command(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    tenant: Tenant,
    owner: Annotated[uuid.UUID, typer.Option("--owner", help="The account that owns the draft.")],
) -> None:
    """Create a private draft from a workflow file; its owner publishes it in the web app."""
    _run(lambda rt, c: import_workflow(rt, c, tenant, path, owner))


@workflows_app.command("runs")
def runs_command(
    tenant: Tenant,
    workflow: Annotated[uuid.UUID | None, typer.Option("--workflow")] = None,
    limit: Annotated[int, typer.Option(min=1, max=500)] = 50,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """The newest runs, of one workflow or all of them."""
    _run(lambda rt, c: list_runs(rt, c, tenant, workflow, limit=limit, as_json=as_json))


@workflows_app.command("cancel")
def cancel_command(run: uuid.UUID, tenant: Tenant) -> None:
    """Cancel a run that has not ended, with any runs it called."""
    _run(lambda rt, c: control_run(rt, c, tenant, run, service.cancel))


@workflows_app.command("redrive")
def redrive_command(run: uuid.UUID, tenant: Tenant) -> None:
    """Retry a failed run from the step that failed."""
    _run(lambda rt, c: control_run(rt, c, tenant, run, service.redrive))


@workflows_app.command("verify")
def verify_command(run: uuid.UUID, tenant: Tenant) -> None:
    """Recompute a run's ledger chain; exits 1 when it does not verify."""
    held: list[bool] = []

    async def check(rt: CliRuntime, console: Console) -> None:
        held.append(await verify_run(rt, console, tenant, run))

    _run(check)
    if held != [True]:
        raise typer.Exit(code=1)


@workflows_app.command("from-routine")
def from_routine_command(
    tenant: Tenant,
    routine: Annotated[
        list[uuid.UUID] | None, typer.Option("--routine", help="A routine to move; repeatable.")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Move them; default is a preview.")
    ] = False,
) -> None:
    """Preview, then move, routines onto one-step workflows, keeping their schedule cursor."""
    _run(lambda rt, c: from_routine(rt, c, tenant, list(routine or []), apply=apply))


@workflows_app.command("to-routine")
def to_routine_command(workflow: uuid.UUID, tenant: Tenant) -> None:
    """Give a moved routine its schedule back and pause its workflow."""
    _run(lambda rt, c: to_routine(rt, c, tenant, workflow))
