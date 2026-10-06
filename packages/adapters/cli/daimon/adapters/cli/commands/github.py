"""Operator command for creating tenant GitHub connection invitations."""

from __future__ import annotations

import getpass
import uuid
from typing import Annotated

import httpx
import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli.errors import run_cli
from daimon.core.config import load_settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.github_app_session import revoke_session_tokens
from daimon.core.github_credentials import build_multifernet
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import (
    activate_agent,
    deactivate_agent,
    get_agent_mode,
    list_agent_grants,
    remove_grant,
    stage_grant,
)
from daimon.core.stores.github_connect import cli_account_id, mint_invitation
from daimon.core.stores.thread_sessions import list_live_sessions_for_agent, mark_dead
from rich.console import Console

github_app = typer.Typer(help="GitHub App connection commands.")
grants_app = typer.Typer(help="Stage and activate agent GitHub grants.")
github_app.add_typer(grants_app, name="grants")


def _run_grant_command(
    tenant: uuid.UUID,
    action: str,
    agent: uuid.UUID,
    *,
    repo: int | None = None,
    baseline: str = "none",
    ceiling: str = "read",
    working: bool = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _run() -> None:
        engine = build_engine(str(settings.database.url))
        try:
            sessionmaker = build_session_factory(engine)
            async with sessionmaker.begin() as session:
                account_id = await cli_account_id(
                    session, tenant_id=tenant, os_user=getpass.getuser()
                )
                account = (
                    await get_account_with_tenant(session, account_id=account_id)
                    if account_id is not None
                    else None
                )
                if (
                    account is None
                    or account.tenant_id != tenant
                    or account.role is not Role.ADMIN
                    or account.is_external
                ):
                    raise ValueError("current CLI user is not a workspace admin")
                if action == "stage":
                    if (
                        repo is None
                        or baseline not in ("none", "read", "write")
                        or ceiling not in ("read", "write")
                    ):
                        raise ValueError("provide a repository ID and valid access levels")
                    result = await stage_grant(
                        session,
                        tenant_id=tenant,
                        agent_id=agent,
                        repo_id=repo,
                        baseline_access=baseline,
                        ceiling_access=ceiling,
                        granted_by_account_id=account_id,
                        is_working_repo=working,
                    )
                    console.print(
                        f"{'staged' if result.staged else 'updated'} {result.repo_id} "
                        f"({result.baseline_access}/{result.ceiling_access})"
                    )
                elif action == "remove":
                    if repo is None:
                        raise ValueError("provide a repository ID")
                    removed = await remove_grant(
                        session,
                        tenant_id=tenant,
                        agent_id=agent,
                        repo_id=repo,
                        changed_by_account_id=account_id,
                    )
                    console.print("removed" if removed else "no grant found")
                elif action == "activate":
                    await activate_agent(
                        session,
                        tenant_id=tenant,
                        agent_id=agent,
                        changed_by_account_id=account_id,
                    )
                    console.print("app mode active")
                elif action == "deactivate":
                    await deactivate_agent(
                        session,
                        tenant_id=tenant,
                        agent_id=agent,
                        changed_by_account_id=account_id,
                    )
                    console.print("legacy mode active")
                else:
                    rows = await list_agent_grants(session, tenant_id=tenant, agent_id=agent)
                    for row in rows:
                        console.print(
                            f"{row.repo_id} {row.baseline_access}/{row.ceiling_access} "
                            f"{'staged' if row.staged else 'live'}"
                        )
            async with sessionmaker() as session:
                app_mode = await get_agent_mode(session, tenant_id=tenant, agent_id=agent) == "app"
            if action in ("activate", "deactivate") or (app_mode and action in ("stage", "remove")):
                async with sessionmaker() as session:
                    live = await list_live_sessions_for_agent(
                        session, tenant_id=tenant, agent_id=agent
                    )
                if live:
                    keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
                    fernet = build_multifernet(keys) if keys else None
                    async with (
                        AsyncAnthropic(
                            api_key=settings.anthropic.api_key.get_secret_value(),
                            base_url=str(settings.anthropic.base_url),
                        ) as anthropic,
                        httpx.AsyncClient() as github,
                    ):
                        archived_ids: set[str] = set()
                        for mapped in live:
                            if mapped.ma_session_id not in archived_ids:
                                await anthropic.beta.sessions.archive(mapped.ma_session_id)
                                if mapped.effective_config is not None:
                                    vault_id = mapped.effective_config.vault_id
                                    if mapped.effective_config.github_mode == "app":
                                        if fernet is None:
                                            raise ValueError(
                                                "App session revocation requires encryption"
                                            )
                                        await revoke_session_tokens(
                                            sessionmaker,
                                            github,
                                            session_id=mapped.ma_session_id,
                                            fernet=fernet,
                                        )
                                        if vault_id is not None:
                                            await anthropic.beta.vaults.archive(vault_id)
                                archived_ids.add(mapped.ma_session_id)
                            async with sessionmaker.begin() as session:
                                await mark_dead(session, id=mapped.id)
        finally:
            await engine.dispose()

    run_cli(_run(), console=console)


@grants_app.command("stage")
def grants_stage(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent: Annotated[uuid.UUID, typer.Option("--agent")],
    repo: Annotated[int, typer.Option("--repo")],
    baseline: Annotated[str, typer.Option("--baseline")] = "none",
    ceiling: Annotated[str, typer.Option("--ceiling")] = "read",
    working: Annotated[bool, typer.Option("--working")] = False,
) -> None:
    _run_grant_command(
        tenant, "stage", agent, repo=repo, baseline=baseline, ceiling=ceiling, working=working
    )


@grants_app.command("list")
def grants_list(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent: Annotated[uuid.UUID, typer.Option("--agent")],
) -> None:
    _run_grant_command(tenant, "list", agent)


@grants_app.command("remove")
def grants_remove(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent: Annotated[uuid.UUID, typer.Option("--agent")],
    repo: Annotated[int, typer.Option("--repo")],
) -> None:
    _run_grant_command(tenant, "remove", agent, repo=repo)


@grants_app.command("activate")
def grants_activate(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent: Annotated[uuid.UUID, typer.Option("--agent")],
) -> None:
    _run_grant_command(tenant, "activate", agent)


@grants_app.command("deactivate")
def grants_deactivate(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent: Annotated[uuid.UUID, typer.Option("--agent")],
) -> None:
    _run_grant_command(tenant, "deactivate", agent)


@github_app.command("connect-link")
def connect_link(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant", help="Destination workspace UUID.")],
) -> None:
    """Print a single-use connection invitation for a tenant admin."""
    settings = load_settings()
    config = settings.github_app
    root = settings.mcp.app_root_url
    if (
        root is None
        or config.app_id is None
        or config.app_slug is None
        or config.private_key is None
        or config.client_id is None
        or config.client_secret is None
        or not settings.crypto.keys
    ):
        raise typer.BadParameter("GitHub connection is not configured")
    console = Console(highlight=False)

    async def _run() -> None:
        engine = build_engine(str(settings.database.url))
        try:
            sessionmaker = build_session_factory(engine)
            async with sessionmaker.begin() as session:
                account_id = await cli_account_id(
                    session, tenant_id=tenant, os_user=getpass.getuser()
                )
                if account_id is None:
                    raise ValueError("current CLI user has no account in this workspace")
                token = await mint_invitation(
                    session,
                    tenant_id=tenant,
                    requester_account_id=account_id,
                    requester_label=getpass.getuser(),
                )
            console.print(f"{root}/oauth/github/connect/{token}")
        finally:
            await engine.dispose()

    run_cli(_run(), console=console)
