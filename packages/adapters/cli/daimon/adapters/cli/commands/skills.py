"""daimon skills … sub-app."""

from __future__ import annotations

from typing import Annotated

import httpx
import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION, YES_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import find_skill_by_display_title, list_skills_lenient
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import StoreError
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma import delete_skill_and_versions
from daimon.core.skill_sync import PATMissingError, SyncReport, sync_agent_skills
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.specs import SkillRepo
from daimon.core.stores.identity import get_or_create_cli_principal
from rich.console import Console
from rich.table import Table

skills_app = typer.Typer(help="Skills: sync, list, get, delete.")


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@skills_app.command("sync")
def skills_sync_command(
    url: str,
    branch: Annotated[str, typer.Option("--branch")] = "main",
    path: Annotated[str, typer.Option("--path")] = "",
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await sync_skills(rt, console, url=url, branch=branch, path=path)

    run_cli(_with_defaults(), console=console)


async def sync_skills(
    rt: CliRuntime,
    console: Console,
    *,
    url: str,
    branch: str,
    path: str,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. Fetch + sync below.

    async def _run(http: httpx.AsyncClient) -> list[ResourceOutcome]:
        return await run_skill_sync(
            rt.anthropic, http, url=url, branch=branch, path=path, tenant_id=tenant_id
        )

    if http_client is not None:
        outcomes = await _run(http_client)
    else:
        async with httpx.AsyncClient(timeout=30.0) as http:
            outcomes = await _run(http)

    table = Table(show_header=True, header_style="bold")
    for col in ("name", "action", "anthropic_id", "error"):
        table.add_column(col)
    for o in outcomes:
        table.add_row(o.name, o.action.value, o.anthropic_id or "", o.error or "")
    console.print(table)


# ---------------------------------------------------------------------------
# sync-agent  (multi-repo PAT-authenticated sync per agent)
# ---------------------------------------------------------------------------


def _parse_repo_arg(raw: str) -> SkillRepo:
    """Parse a `--repo` argument: 'URL[@branch][#path][?split]'.

    Examples:
        https://github.com/owner/repo
        https://github.com/owner/repo@main
        https://github.com/owner/repo@dev#skills
        https://github.com/owner/repo?split
        https://github.com/owner/repo@main?split
    """
    url = raw
    branch = "main"
    path = ""
    split = False
    if "?split" in url:
        split = True
        url = url.replace("?split", "")
    if "#" in url:
        url, path = url.split("#", 1)
    if "@" in url and not url.endswith(".git"):
        url, branch = url.rsplit("@", 1)
    return SkillRepo(url=url, branch=branch, path=path, split=split)


@skills_app.command("sync-agent")
def skills_sync_agent_command(
    agent: Annotated[str, typer.Argument(help="MA agent name (workspace-unique)")],
    repo: Annotated[
        list[str],
        typer.Option(
            "--repo",
            help=(
                "Repository spec, repeatable. Format: URL with optional @branch, "
                "#path, ?split suffixes. Example: --repo "
                "https://github.com/owner/repo@main --repo "
                "https://github.com/owner/repo2?split"
            ),
        ),
    ] = [],  # noqa: B006 -- Typer requires mutable default for list options
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    if not repo:
        console.print("[red]No repositories provided. Pass at least one --repo URL.[/red]")
        raise typer.Exit(code=2)

    repos = [_parse_repo_arg(r) for r in repo]

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await sync_agent(rt, console, agent_name=agent, repos=repos)

    run_cli(_with_defaults(), console=console)


async def sync_agent(
    rt: CliRuntime,
    console: Console,
    *,
    agent_name: str,
    repos: list[SkillRepo],
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Implementation seam.

    `http_client` is an injection seam for tests: if None (production), the impl
    constructs its own `httpx.AsyncClient` with the production timeout; if
    provided, the impl uses the caller's client (lets tests pass a
    `MockTransport`-backed client without monkey-patching `httpx`).
    """
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )

    if not rt.settings.crypto.keys:
        console.print(
            "[red]settings.crypto.keys is empty -- cannot decrypt GitHub PAT. "
            "Configure DAIMON_CRYPTO__KEYS and re-bind the PAT via the agent-setup "
            "repo-auth panel.[/red]"
        )
        raise typer.Exit(code=3)
    fernet = build_multifernet(tuple(k.get_secret_value() for k in rt.settings.crypto.keys))

    async def _run(http: httpx.AsyncClient) -> SyncReport:
        return await sync_agent_skills(
            principal_id=principal.account_id,
            tenant_id=tenant_id,
            agent_name=agent_name,
            repos=repos,
            sessionmaker=rt.sessionmaker,
            fernet=fernet,
            http_client=http,
            anthropic_client=rt.anthropic,
        )

    try:
        if http_client is not None:
            report = await _run(http_client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as http:
                report = await _run(http)
    except PATMissingError as err:
        console.print(
            "[red]No GitHub PAT bound for principal. Bind a PAT via the "
            f"agent-setup repo-auth panel first. ({err})[/red]"
        )
        raise typer.Exit(code=4) from err

    summary = Table(
        title=f"Skill sync for agent '{agent_name}'",
        show_header=True,
        header_style="bold",
    )
    summary.add_column("metric")
    summary.add_column("value", justify="right")
    summary.add_row("synced (new)", str(report.synced))
    summary.add_row("updated (new version)", str(report.updated))
    summary.add_row("deleted (orphan)", str(report.deleted))
    summary.add_row("skipped repos", str(len(report.skipped_repos)))
    summary.add_row("failed uploads", str(len(report.failed_uploads)))
    console.print(summary)

    if report.skipped_repos or report.failed_uploads:
        details = Table(show_header=True, header_style="bold")
        details.add_column("kind")
        details.add_column("identifier")
        details.add_column("reason")
        for url, reason in report.skipped_repos:
            details.add_row("skipped-repo", url, reason)
        for name, reason in report.failed_uploads:
            details.add_row("failed-upload", name, reason)
        console.print(details)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@skills_app.command("list")
def skills_list_command(
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await list_skills(rt, console, as_json=as_json)

    run_cli(_with_defaults(), console=console)


async def list_skills(rt: CliRuntime, console: Console, *, as_json: bool) -> None:
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    all_rows, truncated = await list_skills_lenient(rt.anthropic)
    if truncated:
        console.print(
            "[yellow]Warning: skill list is truncated at the MA page limit — "
            "some skills may not appear.[/yellow]"
        )
    # Show own tenant's skills (bare names) plus anthropic built-ins.
    visible = [
        sk
        for sk in all_rows
        if sk.source == "anthropic"
        or (
            sk.display_title is not None
            and strip_tenant_prefix(tenant_id=tenant_id, display_title=sk.display_title) is not None
        )
    ]
    cols = ("display_title", "id", "source", "created_at")
    emit_rows(console, visible, columns=cols, as_json=as_json)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@skills_app.command("get")
def skills_get_command(
    name: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await get_skill(rt, console, name=name, as_json=as_json)

    run_cli(_with_defaults(), console=console)


async def get_skill(rt: CliRuntime, console: Console, *, name: str, as_json: bool) -> None:
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name=name)
    skill = await find_skill_by_display_title(rt.anthropic, canonical, on_truncation="degrade")
    if skill is None:
        raise StoreError(f"no skill named {name!r} in your account.")
    version_count = 0
    async for _ in rt.anthropic.beta.skills.versions.list(skill.id):
        version_count += 1
    cols = ("display_title", "id", "source", "created_at")
    emit_rows(console, [skill], columns=cols, as_json=as_json)
    if not as_json:
        console.print(f"Versions: {version_count}")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@skills_app.command("delete")
def skills_delete_command(
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await delete_skill(rt, console, name=name, yes=yes)

    run_cli(_with_defaults(), console=console)


async def delete_skill(rt: CliRuntime, console: Console, *, name: str, yes: bool) -> None:
    confirm_or_abort(console, f"delete skill {name!r}?", yes=yes)
    async with rt.sessionmaker() as session, session.begin():
        tenant_id = await discover_tenant(session)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    # Session closed. MA calls below.
    canonical = tenant_scoped_display_title(tenant_id=tenant_id, name=name)
    skill = await find_skill_by_display_title(rt.anthropic, canonical, on_truncation="degrade")
    if skill is None:
        raise StoreError(f"no skill named {name!r} in your account.")
    await delete_skill_and_versions(rt.anthropic, skill.id)
    console.print(f"[green]✓ deleted skill {name!r}[/green]")


# Register backfill command on skills_app (defined above, so no circular import).
import daimon.adapters.cli.commands.skills_backfill as _reg  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
