"""daimon skills … sub-app."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

import httpx
import typer
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import GUILD_OPTION, JSON_OPTION, TENANT_OPTION, YES_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import (
    TenantSelector,
    discover_tenant,
    resolve_tenant_display,
    resolve_tenant_override,
)
from daimon.core.agent_pins import pin_refusal
from daimon.core.authz import Place, Subject
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import (
    find_agent_by_daimon_tag,
    find_skill_by_display_title,
    list_skills_lenient,
)
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ACCOUNT,
    MA_METADATA_KEY_MANAGED,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)
from daimon.core.defaults.report import ResourceOutcome
from daimon.core.errors import StoreError
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma import delete_skill_and_versions
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.skill_sync import PATMissingError, SyncReport, sync_agent_skills
from daimon.core.skills.add import (
    add_agent_skill,
    fetch_repo_skill,
    read_local_skill,
    repo_origin,
)
from daimon.core.skills.ingest import SkillBundle
from daimon.core.skills.pipeline import run_skill_sync
from daimon.core.specs import SkillRepo
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.seeded_skills import list_seeded_skill_names
from rich.console import Console
from rich.table import Table

skills_app = typer.Typer(help="Skills: sync, add, list, get, delete.")


@skills_app.callback()
def skills_callback(
    ctx: typer.Context,
    tenant: Annotated[str | None, TENANT_OPTION] = None,
    guild: Annotated[str | None, GUILD_OPTION] = None,
) -> None:
    ctx.obj = TenantSelector(tenant_id=tenant, guild_id=guild)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@skills_app.command("sync")
def skills_sync_command(
    ctx: typer.Context,
    url: str,
    branch: Annotated[str, typer.Option("--branch")] = "main",
    path: Annotated[str, typer.Option("--path")] = "",
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await sync_skills(rt, console, url=url, branch=branch, path=path, selector=selector)

    run_cli(_with_defaults(), console=console)


async def sync_skills(
    rt: CliRuntime,
    console: Console,
    *,
    url: str,
    branch: str,
    path: str,
    http_client: httpx.AsyncClient | None = None,
    selector: TenantSelector | None = None,
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        seeded_skill_names = await list_seeded_skill_names(session, tenant_id=tenant_id)
    # Session closed. Fetch + sync below.

    async def _run(http: httpx.AsyncClient) -> list[ResourceOutcome]:
        return await run_skill_sync(
            rt.anthropic,
            http,
            url=url,
            branch=branch,
            path=path,
            tenant_id=tenant_id,
            seeded_skill_names=seeded_skill_names,
            # The operator owns the library.
            is_admin=True,
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
    ctx: typer.Context,
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
    selector = ctx.obj

    if not repo:
        console.print("[red]No repositories provided. Pass at least one --repo URL.[/red]")
        raise typer.Exit(code=2)

    repos = [_parse_repo_arg(r) for r in repo]

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await sync_agent(rt, console, agent_name=agent, repos=repos, selector=selector)

    run_cli(_with_defaults(), console=console)


async def sync_agent(
    rt: CliRuntime,
    console: Console,
    *,
    agent_name: str,
    repos: list[SkillRepo],
    http_client: httpx.AsyncClient | None = None,
    selector: TenantSelector | None = None,
) -> None:
    """Implementation seam.

    `http_client` is an injection seam for tests: if None (production), the impl
    constructs its own `httpx.AsyncClient` with the production timeout; if
    provided, the impl uses the caller's client (lets tests pass a
    `MockTransport`-backed client without monkey-patching `httpx`).
    """
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )

    if not rt.settings.crypto.keys:
        console.print(
            "[red]settings.crypto.keys is empty -- cannot decrypt GitHub PAT. "
            "Configure DAIMON_CRYPTO__KEYS and re-bind the PAT with "
            "request_repo_binding in chat.[/red]"
        )
        raise typer.Exit(code=3)
    fernet = build_multifernet(tuple(k.get_secret_value() for k in rt.settings.crypto.keys))
    github_fallback_pat = (
        rt.settings.github.fallback_pat.get_secret_value()
        if rt.settings.github.fallback_pat is not None
        else None
    )

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
            github_fallback_pat=github_fallback_pat,
        )

    try:
        if http_client is not None:
            report = await _run(http_client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as http:
                report = await _run(http)
    except PATMissingError as err:
        console.print(
            "[red]No GitHub PAT bound for principal. Bind one with "
            f"request_repo_binding in chat first. ({err})[/red]"
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
    ctx: typer.Context,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await list_skills(rt, console, as_json=as_json, selector=selector)

    run_cli(_with_defaults(), console=console)


async def list_skills(
    rt: CliRuntime, console: Console, *, as_json: bool, selector: TenantSelector | None = None
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
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
    ctx: typer.Context,
    name: str,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await get_skill(rt, console, name=name, as_json=as_json, selector=selector)

    run_cli(_with_defaults(), console=console)


async def get_skill(
    rt: CliRuntime,
    console: Console,
    *,
    name: str,
    as_json: bool,
    selector: TenantSelector | None = None,
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
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
    ctx: typer.Context,
    name: str,
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await delete_skill(rt, console, name=name, yes=yes, selector=selector)

    run_cli(_with_defaults(), console=console)


async def delete_skill(
    rt: CliRuntime,
    console: Console,
    *,
    name: str,
    yes: bool,
    selector: TenantSelector | None = None,
) -> None:
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        tenant_label = await resolve_tenant_display(session, tenant_id)
        confirm_or_abort(console, f"delete skill {name!r} in {tenant_label}?", yes=yes)
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


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@skills_app.command("add")
def skills_add_command(
    ctx: typer.Context,
    source: Annotated[
        str,
        typer.Argument(help="A skill folder, a SKILL.md or .zip file, or a GitHub repo URL."),
    ],
    agent: Annotated[str, typer.Option("--agent", help="The agent to add the skill to.")],
    branch: Annotated[str, typer.Option("--branch", help="Repo URL only.")] = "main",
    path: Annotated[str, typer.Option("--path", help="Repo URL only: the skill's folder.")] = "",
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    """Add one skill to one agent as its own skill; adding it again updates it."""
    settings = load_settings()
    console = Console(highlight=False)
    selector = ctx.obj

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await add_skill(
                rt,
                console,
                agent_name=agent,
                source=source,
                branch=branch,
                path=path,
                yes=yes,
                selector=selector,
            )

    run_cli(_with_defaults(), console=console)


def _is_repo_url(source: str) -> bool:
    return source.startswith(("https://", "http://"))


async def _skill_add_refusal(
    rt: CliRuntime, *, tenant_id: uuid.UUID, agent: BetaManagedAgentsAgent
) -> str | None:
    """Why the CLI may not add a skill to `agent`, else None.

    The CLI acts as a server admin, so it follows the admin path of the same
    rules the chat tool and the panels apply: a built-in agent never takes a
    skill (its skills come from defaults), and the pin rule is asked rather
    than assumed.
    """
    managed = agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true"
    target = TargetFacts(is_daimon_managed=managed, is_reachable_in_tenant=False)
    if (
        decide_operation("skill_add", is_admin=True, target=target) != "allow"
        or agent.metadata.get(MA_METADATA_KEY_ACCOUNT) is None
    ):
        return (
            f"'{agent.name}' is a built-in agent; its skills come from defaults. "
            "Fork it with `daimon agents fork` and add the skill to the copy."
        )

    async def admin() -> Subject:
        return Subject(is_admin=True)

    async def this_agent() -> BetaManagedAgentsAgent:
        return agent

    async with rt.sessionmaker() as session:
        return await pin_refusal(
            session, tenant_id=tenant_id, load_subject=admin, load_agent=this_agent, place=Place()
        )


async def _load_skill(
    rt: CliRuntime, http: httpx.AsyncClient, *, source: str, branch: str, path: str
) -> tuple[SkillBundle, str]:
    """The checked skill and a short origin for the ledger."""
    if not _is_repo_url(source):
        local = Path(source).expanduser()
        return await read_local_skill(local), f"cli {local.name}"
    # Public repos only: the deployment's fallback token is for repo bindings.
    bundle = await fetch_repo_skill(
        http,
        url=source,
        branch=branch,
        path=path,
        token=None,
        max_tarball_bytes=rt.settings.github.max_tarball_bytes,
        max_tarball_decompressed_bytes=rt.settings.github.max_tarball_decompressed_bytes,
    )
    return bundle, repo_origin(source, path=path, branch=branch)


async def add_skill(
    rt: CliRuntime,
    console: Console,
    *,
    agent_name: str,
    source: str,
    branch: str = "main",
    path: str = "",
    yes: bool,
    selector: TenantSelector | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Preview the skill, confirm, then upload it agent-scoped and attach it.

    `http_client` is a test seam for the repo fetch; None opens a client.
    """
    if not _is_repo_url(source) and (branch != "main" or path):
        raise StoreError("--branch and --path apply to a repo URL only.")
    async with rt.sessionmaker() as session, session.begin():
        override = await resolve_tenant_override(session, selector)
        tenant_id = await discover_tenant(session, override=override)
        tenant_label = await resolve_tenant_display(session, tenant_id)
        principal = await get_or_create_cli_principal(
            session, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
    agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=agent_name)
    if agent is None:
        raise StoreError(f"no agent named {agent_name!r} in {tenant_label}.")

    async def recheck(fresh: BetaManagedAgentsAgent) -> None:
        refusal = await _skill_add_refusal(rt, tenant_id=tenant_id, agent=fresh)
        if refusal is not None:
            raise StoreError(f"{refusal} Nothing was changed.")

    await recheck(agent)
    if http_client is not None:
        bundle, origin = await _load_skill(rt, http_client, source=source, branch=branch, path=path)
    else:
        async with httpx.AsyncClient(timeout=30.0) as http:
            bundle, origin = await _load_skill(rt, http, source=source, branch=branch, path=path)
    preview = bundle.preview
    console.print(f"[bold]{preview.name}[/bold]: {preview.description}")
    console.print(f"files: {', '.join(preview.files)} ({preview.total_bytes} bytes)")
    if preview.scripts:
        console.print(f"[yellow]runnable: {', '.join(preview.scripts)}[/yellow]")
    confirm_or_abort(
        console, f"add skill {preview.name!r} to {agent_name!r} in {tenant_label}?", yes=yes
    )
    added = await add_agent_skill(
        rt.anthropic,
        rt.sessionmaker,
        tenant_id=tenant_id,
        agent=agent,
        agent_name=agent_name,
        bundle=bundle,
        origin=origin,
        added_by_account_id=principal.account_id,
        recheck=recheck,
    )
    done = {
        "created": f"added skill {preview.name!r} to {agent_name!r}",
        "updated": f"updated skill {preview.name!r} on {agent_name!r}",
        "unchanged": f"{agent_name!r} already had this {preview.name!r}",
    }[added.action]
    console.print(f"[green]✓ {done}[/green]")


# Register backfill command on skills_app (defined above, so no circular import).
import daimon.adapters.cli.commands.skills_backfill as _reg  # noqa: E402, F401  # pyright: ignore[reportUnusedImport]
