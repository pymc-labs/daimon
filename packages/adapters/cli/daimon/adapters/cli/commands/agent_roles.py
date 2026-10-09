"""Operator cleanup for bot-managed Discord agent roles before rollback."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Literal

import httpx
import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.errors import DaimonError, StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.discord_agent_roles import delete_role_if_matches, list_roles
from daimon.core.stores.tenants import get_tenant, list_tenants_by_platform
from pydantic import BaseModel
from rich.console import Console

roles_app = typer.Typer(help="Bot-managed Discord agent roles.")
_DISCORD_API = "https://discord.com/api/v10"


class PurgeRoleResult(BaseModel):
    workspace_id: str
    agent_name: str
    role_id: str
    status: Literal["would_delete", "deleted", "already_absent", "failed"]


@roles_app.command("purge")
def roles_purge_command(
    platform: Annotated[str | None, typer.Argument(help="Platform (discord).")] = None,
    workspace_id: Annotated[str | None, typer.Argument(help="Discord server ID.")] = None,
    all_workspaces: Annotated[bool, typer.Option("--all", help="All Discord servers.")] = False,
    apply: Annotated[bool, typer.Option("--apply", help="Delete roles and clear rows.")] = False,
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _run() -> None:
        async with build_runtime(settings) as rt:
            await roles_purge(
                rt=rt,
                console=console,
                platform=platform,
                workspace_id=workspace_id,
                all_workspaces=all_workspaces,
                apply=apply,
                as_json=as_json,
            )

    run_cli(_run(), console=console)


async def _delete_discord_role(
    http: httpx.AsyncClient, *, workspace_id: str, role_id: str
) -> Literal["deleted", "already_absent"]:
    if not workspace_id.isdigit() or not role_id.isdigit():
        raise StoreError("recorded Discord server and role IDs must be numeric")
    for attempt in range(4):
        response = await http.delete(f"/guilds/{workspace_id}/roles/{role_id}")
        if response.status_code == 404:
            return "already_absent"
        if response.is_success:
            return "deleted"
        if response.status_code == 429 and attempt < 3:
            try:
                delay = float(response.headers.get("Retry-After", "1"))
            except ValueError:
                delay = 1.0
            await asyncio.sleep(min(max(delay, 0.0), 30.0))
            continue
        raise DaimonError(
            f"Discord returned HTTP {response.status_code} deleting role {role_id} "
            f"in server {workspace_id}"
        )
    raise AssertionError("unreachable")


async def roles_purge(
    *,
    rt: CliRuntime,
    console: Console,
    platform: str | None,
    workspace_id: str | None,
    all_workspaces: bool,
    apply: bool,
    as_json: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Delete only stored Discord role IDs, then clear their rows one by one."""
    if all_workspaces:
        if platform is not None or workspace_id is not None:
            raise typer.BadParameter("use --all or PLATFORM WORKSPACE_ID, not both")
        tenants = await list_tenants_by_platform(rt.sessionmaker, platform="discord")
        scopes = [(tenant.id, tenant.external_id) for tenant in tenants]
    else:
        if platform is None or workspace_id is None:
            raise typer.BadParameter("give discord WORKSPACE_ID or --all")
        if platform != "discord":
            raise typer.BadParameter("only discord has managed agent roles")
        if not workspace_id.isdigit():
            raise typer.BadParameter("Discord WORKSPACE_ID must be numeric")
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=workspace_id)
        async with rt.sessionmaker() as session:
            if await get_tenant(session, tenant_id) is None:
                raise StoreError(f"no Discord server {workspace_id} in this deployment")
        scopes = [(tenant_id, workspace_id)]

    recorded: list[tuple[uuid.UUID, str, str, str, str]] = []
    async with rt.sessionmaker() as session:
        for tenant_id, guild_id in scopes:
            for row in await list_roles(session, tenant_id=tenant_id):
                recorded.append((tenant_id, guild_id, row.ma_agent_id, row.role_id, row.agent_name))

    if apply and recorded and rt.settings.discord is None:
        raise StoreError("DAIMON_DISCORD__BOT_TOKEN is required to purge recorded roles")
    results: list[PurgeRoleResult] = []
    failures = 0
    if apply and recorded:
        assert rt.settings.discord is not None
        async with httpx.AsyncClient(
            base_url=_DISCORD_API,
            headers={"Authorization": f"Bot {rt.settings.discord.bot_token.get_secret_value()}"},
            timeout=10.0,
            transport=transport,
        ) as http:
            for tenant_id, guild_id, ma_agent_id, role_id, agent_name in recorded:
                try:
                    status: Literal[
                        "deleted", "already_absent", "failed"
                    ] = await _delete_discord_role(http, workspace_id=guild_id, role_id=role_id)
                    async with rt.sessionmaker.begin() as session:
                        await delete_role_if_matches(
                            session,
                            tenant_id=tenant_id,
                            ma_agent_id=ma_agent_id,
                            role_id=role_id,
                        )
                except (DaimonError, httpx.HTTPError):
                    status = "failed"
                    failures += 1
                results.append(
                    PurgeRoleResult(
                        workspace_id=guild_id,
                        agent_name=agent_name,
                        role_id=role_id,
                        status=status,
                    )
                )
    else:
        results = [
            PurgeRoleResult(
                workspace_id=guild_id,
                agent_name=agent_name,
                role_id=role_id,
                status="would_delete",
            )
            for _, guild_id, _, role_id, agent_name in recorded
        ]
    emit_rows(
        console,
        results,
        columns=("workspace_id", "agent_name", "role_id", "status"),
        as_json=as_json,
    )
    if failures:
        raise StoreError(f"{failures} Discord role deletes failed; their rows were kept for retry")
