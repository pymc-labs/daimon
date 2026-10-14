"""`daimon memory` — read-only view of an agent's MA memory store.

`personal-report` scans every agent's SHARED store for entries that look
personal (`daimon.core.memory_personal_scan`) and prints, per agent, a count
and a few redacted examples. Like the rest of this group it only reads: it
never moves, edits or deletes a memory.
"""

from __future__ import annotations

from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.memory_personal_scan import scan_shared_memory
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import list_memory_entries, read_memory, retrieve_memory_store
from daimon.core.stores.agent_memory_stores import get_memory_store_id, list_memory_stores
from daimon.core.stores.domain import Platform
from pydantic import BaseModel
from rich.console import Console

memory_app = typer.Typer(help="Inspect an agent's persistent memory (read-only)")

_VALID_PLATFORMS = ("discord", "cli", "slack", "teams")


def _validate_platform(value: str) -> Platform:
    """Validate a raw CLI `--platform` value before it reaches `derive_tenant_uuid`.

    Mirrors `daimon.adapters.cli.commands.tenants._validate_platform` so an
    unrecognized platform (e.g. a typo) fails with a clear usage error instead
    of silently deriving a wrong tenant UUID.
    """
    if value in _VALID_PLATFORMS:
        return value  # type: ignore[return-value]
    raise typer.BadParameter(
        f"unsupported platform {value!r}; valid: {', '.join(_VALID_PLATFORMS)}"
    )


class _MemoryPathRow(BaseModel):
    """Report row for `memory list` output."""

    path: str


async def _resolve_store_id(
    rt: CliRuntime, *, platform: str, workspace: str, agent: str
) -> str | None:
    validated_platform = _validate_platform(platform)
    tenant_id = derive_tenant_uuid(platform=validated_platform, workspace_id=workspace)
    ma_agent = await find_agent_by_daimon_tag(rt.anthropic, tenant_id=tenant_id, name=agent)
    if ma_agent is None:
        raise DaimonError(f"agent {agent!r} not found for {platform}/{workspace}")
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
    async with rt.sessionmaker() as session:
        return await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_uuid)


async def memory_list_impl(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str,
    workspace: str,
    agent: str,
    as_json: bool,
) -> None:
    store_id = await _resolve_store_id(rt, platform=platform, workspace=workspace, agent=agent)
    if store_id is None:
        console.print("No memories yet.")
        return
    rows: list[_MemoryPathRow] = []
    scope = resource_scope(
        tenant_id=str(
            derive_tenant_uuid(platform=_validate_platform(platform), workspace_id=workspace)
        )
    )
    async for item in list_memory_entries(rt.anthropic, store_id, path_prefix="/", scope=scope):
        if item.type == "memory":
            rows.append(_MemoryPathRow(path=item.path))
    if not rows:
        console.print("No memories yet.")
        return
    rows.sort(key=lambda r: r.path)
    emit_rows(
        console,
        rows,
        columns=("path",),
        as_json=as_json,
    )


async def memory_show_impl(
    *,
    rt: CliRuntime,
    console: Console,
    path: str,
    platform: str,
    workspace: str,
    agent: str,
) -> None:
    store_id = await _resolve_store_id(rt, platform=platform, workspace=workspace, agent=agent)
    if store_id is None:
        raise DaimonError("agent has no memory store")
    scope = resource_scope(
        tenant_id=str(
            derive_tenant_uuid(platform=_validate_platform(platform), workspace_id=workspace)
        )
    )
    async for item in list_memory_entries(rt.anthropic, store_id, path_prefix="/", scope=scope):
        if item.type == "memory" and item.path == path:
            mem = await read_memory(rt.anthropic, store_id, item.id, scope=scope)
            # untrusted agent-written text — never interpret as Rich markup
            console.print(mem.content or "", markup=False, highlight=False)
            return
    raise DaimonError(f"no memory at {path!r}")


@memory_app.command("list")
def memory_list_command(
    platform: str = typer.Option(..., help="Platform (discord|slack|teams)"),
    workspace: str = typer.Option(..., help="Workspace/guild external id"),
    agent: str = typer.Option(..., help="Agent name"),
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """List an agent's memory file paths."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await memory_list_impl(
                rt=rt,
                console=console,
                platform=platform,
                workspace=workspace,
                agent=agent,
                as_json=as_json,
            )

    run_cli(_with_defaults(), console=console)


@memory_app.command("show")
def memory_show_command(
    path: str = typer.Argument(..., help="Memory file path, e.g. /notes/a.md"),
    platform: str = typer.Option(..., help="Platform (discord|slack|teams)"),
    workspace: str = typer.Option(..., help="Workspace/guild external id"),
    agent: str = typer.Option(..., help="Agent name"),
) -> None:
    """Print one memory file's content."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await memory_show_impl(
                rt=rt,
                console=console,
                path=path,
                platform=platform,
                workspace=workspace,
                agent=agent,
            )

    run_cli(_with_defaults(), console=console)


class _PersonalReportRow(BaseModel):
    """Report row for `memory personal-report`: counts and redacted examples only."""

    tenant_id: str
    agent: str
    memories: int
    looks_personal: int
    examples: str


async def memory_personal_report_impl(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str | None,
    workspace: str | None,
    max_examples: int,
    as_json: bool,
) -> None:
    """Scan shared stores for personal-looking entries. Reads only."""
    if (platform is None) != (workspace is None):
        raise typer.BadParameter("--platform and --workspace go together")
    tenant_id = (
        derive_tenant_uuid(platform=_validate_platform(platform), workspace_id=workspace)
        if platform is not None and workspace is not None
        else None
    )
    async with rt.sessionmaker() as session:
        bound = await list_memory_stores(session, tenant_id=tenant_id)
    rows: list[_PersonalReportRow] = []
    for store_tenant, agent_id, store_id in bound:
        scope = resource_scope(tenant_id=str(store_tenant))
        store = await retrieve_memory_store(rt.anthropic, store_id, scope=scope)
        scan = await scan_shared_memory(
            rt.anthropic, store_id, max_examples=max_examples, scope=scope
        )
        rows.append(
            _PersonalReportRow(
                tenant_id=str(store_tenant),
                agent=store.name or str(agent_id),
                memories=scan.memories,
                looks_personal=scan.flagged,
                examples=" | ".join(scan.examples),
            )
        )
    if not rows:
        console.print("No shared memory stores.")
        return
    emit_rows(
        console,
        rows,
        columns=("tenant_id", "agent", "memories", "looks_personal", "examples"),
        as_json=as_json,
    )


@memory_app.command("personal-report")
def memory_personal_report_command(
    platform: str | None = typer.Option(None, help="Only this platform (with --workspace)"),
    workspace: str | None = typer.Option(None, help="Only this workspace/guild external id"),
    max_examples: int = typer.Option(3, help="Redacted examples per agent"),
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    """Count shared-memory entries that look personal, per agent (read-only, redacted)."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await memory_personal_report_impl(
                rt=rt,
                console=console,
                platform=platform,
                workspace=workspace,
                max_examples=max_examples,
                as_json=as_json,
            )

    run_cli(_with_defaults(), console=console)
