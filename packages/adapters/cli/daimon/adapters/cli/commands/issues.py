"""Operator view of a workspace's failures and warnings, grouped into issues."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core import ops_overview as ops
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.errors import ConfigError
from daimon.core.ops_catalog import OpsArea, OpsSeverity
from daimon.core.ops_overview import IssueAction, OpsPeriod
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.tenants import get_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

issues_app = typer.Typer(help="Read and resolve a workspace's failures and warnings.")

_COLUMNS = ("severity", "title", "area", "events", "people", "state", "last_seen", "fingerprint")


def _window(text: str) -> timedelta:
    try:
        return ops.parse_window(text)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


async def _agent_names(rt: CliRuntime, tenant: uuid.UUID) -> dict[str, str]:
    try:
        agents = await list_agents_by_tenant(
            rt.anthropic, tenant_id=tenant, records=rt.sessionmaker
        )
        return {a.id: a.name for a in agents}
    except Exception as exc:
        # Boundary: the read exists to show an outage, so a failing agent listing must not stop
        # it; issues then name agents from the routines and watches seen.
        log.warning("cli.ops_agents_unlisted", error_class=type(exc).__name__)
        return {}


def _print_json(console: Console, payload: object) -> None:
    console.print(json.dumps(payload), soft_wrap=True, highlight=False, markup=False)


@issues_app.command("summary")
def summary_command(
    tenant: uuid.UUID,
    period: Annotated[OpsPeriod, typer.Option(help="1h, 24h, 7d or 30d.")] = "24h",
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Answers, failures, status and what needs attention."""
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt, rt.sessionmaker() as session:
            overview = await ops.load_ops_overview(
                session,
                tenant_id=tenant,
                now=datetime.now(UTC),
                period=period,
                markup=settings.billing.markup,
                agent_names=await _agent_names(rt, tenant),
            )
        if as_json:
            _print_json(console, overview.model_dump(mode="json"))
            return
        console.print(
            f"Status {overview.status}: {overview.failed} of {overview.answers} answers failed "
            f"({overview.failed_rate:.1%}), {overview.dm_failures} direct message failures, "
            f"{overview.routines_failing} of {overview.routines_total} routines failing."
        )
        emit_rows(console, overview.attention, as_json=False, columns=_COLUMNS)

    run_cli(run(), console=console)


@issues_app.command("list")
def list_command(
    tenant: uuid.UUID,
    since: Annotated[str, typer.Option(help="How far back to look, like 15m, 24h or 7d.")] = "24h",
    area: Annotated[OpsArea | None, typer.Option()] = None,
    severity: Annotated[OpsSeverity | None, typer.Option()] = None,
    active: Annotated[bool, typer.Option("--active", help="Only open issues seen in 24h.")] = False,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Issues seen in the window, newest first."""
    window = _window(since)
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt, rt.sessionmaker() as session:
            found = await ops.load_issues(
                session,
                tenant_id=tenant,
                now=datetime.now(UTC),
                status="active" if active else "all",
                area=area,
                severity=severity,
                window=window,
                agent_names=await _agent_names(rt, tenant),
            )
        emit_rows(console, found[:limit], as_json=as_json, columns=_COLUMNS)

    run_cli(run(), console=console)


@issues_app.command("show")
def show_command(
    tenant: uuid.UUID,
    fingerprint: str,
    events: Annotated[int, typer.Option(min=0, max=200, help="How many events to show.")] = 50,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """One issue with its people and latest events."""
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt, rt.sessionmaker() as session:
            detail = await ops.load_issue(
                session,
                tenant_id=tenant,
                fingerprint=fingerprint,
                now=datetime.now(UTC),
                agent_names=await _agent_names(rt, tenant),
            )
            if detail is None:
                raise typer.BadParameter("no such issue in this workspace")
            rows = await ops.load_issue_events(
                session, tenant_id=tenant, fingerprint=fingerprint, before=None, limit=events
            )
        shown = [event for _, event in rows]
        if as_json:
            _print_json(
                console,
                {
                    **detail.model_dump(mode="json"),
                    "events": [e.model_dump(mode="json") for e in shown],
                },
            )
            return
        console.print(
            f"{detail.title}: {detail.reason_label or 'no reason'}, {detail.events} events."
        )
        emit_rows(
            console,
            shown,
            as_json=False,
            columns=("at", "count", "person", "where_label", "reason_label", "note"),
        )

    run_cli(run(), console=console)


ACCOUNT_OPTION = typer.Option(
    "--account",
    help="The workspace member making the change: an account id or their platform user id. "
    "Without it the audit trail names Daimon, via the CLI.",
)


async def _operator_account(
    session: AsyncSession, tenant: uuid.UUID, account: str | None
) -> uuid.UUID | None:
    if account is None:
        return None
    found = await get_tenant(session, tenant)
    if found is None:
        raise ConfigError(f"no workspace {tenant}")
    try:
        account_id = uuid.UUID(account)
    except ValueError:
        principal = await find_platform_principal(
            session, tenant_id=tenant, platform=found.platform, external_id=account
        )
        account_id = principal.account_id if principal else None
    identity = account_id and await get_account_with_tenant(session, account_id=account_id)
    if not identity or identity.tenant_id != tenant:
        raise ConfigError(f"no account {account!r} in workspace {tenant}")
    return identity.account_id


def _change(action: IssueAction, tenant: uuid.UUID, fingerprint: str, account: str | None) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def run() -> None:
        async with build_runtime(settings) as rt:
            names = await _agent_names(rt, tenant)
            async with rt.sessionmaker() as session, session.begin():
                changed = await ops.change_issue_state(
                    session,
                    tenant_id=tenant,
                    fingerprint=fingerprint,
                    action=action,
                    by_account_id=await _operator_account(session, tenant, account),
                    now=datetime.now(UTC),
                    agent_names=names,
                    via="via the CLI",
                )
        if changed is None:
            raise typer.BadParameter("no such issue in this workspace")
        console.print(f"{changed.title}: now {changed.state}.")

    run_cli(run(), console=console)


@issues_app.command("resolve")
def resolve_command(
    tenant: uuid.UUID, fingerprint: str, account: Annotated[str | None, ACCOUNT_OPTION] = None
) -> None:
    """Hide an issue until a newer event arrives."""
    _change("resolve", tenant, fingerprint, account)


@issues_app.command("mute")
def mute_command(
    tenant: uuid.UUID, fingerprint: str, account: Annotated[str | None, ACCOUNT_OPTION] = None
) -> None:
    """Hide an issue until it is reopened."""
    _change("mute", tenant, fingerprint, account)


@issues_app.command("reopen")
def reopen_command(
    tenant: uuid.UUID, fingerprint: str, account: Annotated[str | None, ACCOUNT_OPTION] = None
) -> None:
    """Make a resolved or muted issue open again."""
    _change("reopen", tenant, fingerprint, account)
