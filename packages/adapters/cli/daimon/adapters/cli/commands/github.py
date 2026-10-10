"""Operator command for creating tenant GitHub connection invitations."""

from __future__ import annotations

import getpass
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli.errors import run_cli
from daimon.core.config import load_settings
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.github_app_session import (
    archive_app_vault,
    effective_repo_urls,
    revoke_session_tokens,
    rotate_live_app_tokens,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.session_mutation import session_mutation_fence
from daimon.core.session_snapshot import SessionSnapshot
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
from daimon.core.stores.github_connect import (
    activate_pending_agent,
    admin_account_for_platform_user,
    cli_account_id,
    mint_invitation,
)
from daimon.core.stores.security_audit import append_event
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import (
    get_thread_session_by_id,
    list_live_sessions_for_agent,
    mark_dead,
    record_app_token_refresh,
)
from rich.console import Console

github_app = typer.Typer(help="GitHub App connection commands.")
grants_app = typer.Typer(help="Stage and activate agent GitHub grants.")
github_app.add_typer(grants_app, name="grants")


@github_app.command("finish-update")
def finish_update(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent: Annotated[uuid.UUID, typer.Option("--agent")],
) -> None:
    """Finish an operator-issued saved-key update after repo confirmation."""
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
                if account_id is None:
                    raise ValueError("current CLI user is not a workspace admin")
                finished = await activate_pending_agent(
                    session, tenant_id=tenant, agent_id=agent, account_id=account_id
                )
                if not finished:
                    raise ValueError("No operator-issued GitHub update is waiting for this agent")
            console.print("GitHub update finished. Open chats restart on the next turn.")
        finally:
            await engine.dispose()

    run_cli(_run(), console=console)


async def _require_tenant_agent(
    anthropic: AsyncAnthropic, *, tenant_id: uuid.UUID, agent_id: uuid.UUID, agent_name: str
) -> None:
    """Verify both CLI agent arguments against a live MA agent in this tenant."""
    rows = await list_agents_by_tenant(anthropic, tenant_id=tenant_id)
    if not any(
        derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(row.id)) == agent_id
        and row.name == agent_name
        for row in rows
    ):
        raise ValueError("agent must be a current agent in this workspace")


def _grant_session_action(
    action: str, snapshot: SessionSnapshot | None, desired_urls: tuple[str, ...]
) -> Literal["rotate", "close"]:
    if (
        action in ("stage", "remove")
        and snapshot is not None
        and snapshot.github_mode == "app"
        and desired_urls == snapshot.repo_urls
    ):
        return "rotate"
    return "close"


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
                        rotated_ids: set[str] = set()
                        for mapped in live:
                            snapshot = mapped.effective_config
                            if mapped.ma_session_id in rotated_ids:
                                continue
                            if (
                                action in ("stage", "remove")
                                and snapshot is not None
                                and snapshot.github_mode == "app"
                                and snapshot.vault_id is not None
                            ):
                                if fernet is None:
                                    raise ValueError("App session refresh requires encryption")
                                urls = await effective_repo_urls(
                                    sessionmaker,
                                    tenant_id=tenant,
                                    agent_id=agent,
                                    account_id=mapped.account_id,
                                    is_external=False,
                                    config=settings.github_app,
                                    fernet=fernet,
                                    mounted_only=True,
                                )
                                if _grant_session_action(action, snapshot, urls) == "rotate":
                                    # The scheduler refresh and turn start/finish take the
                                    # same fence; read the turn state only once inside it.
                                    async with session_mutation_fence(
                                        sessionmaker, mapped.ma_session_id, check=False
                                    ):
                                        async with sessionmaker() as session:
                                            current = await get_thread_session_by_id(
                                                session, id=mapped.id
                                            )
                                        if current is not None and current.status == "live":
                                            await rotate_live_app_tokens(
                                                anthropic,
                                                sessionmaker,
                                                session_id=mapped.ma_session_id,
                                                tenant_id=tenant,
                                                agent_id=agent,
                                                account_id=mapped.account_id,
                                                is_external=False,
                                                vault_id=snapshot.vault_id,
                                                resource_ids=snapshot.repo_resource_ids,
                                                config=settings.github_app,
                                                fernet=fernet,
                                                active_turn=current.active_turn_message_id
                                                is not None,
                                            )
                                            async with sessionmaker.begin() as session:
                                                await record_app_token_refresh(
                                                    session,
                                                    ma_session_id=mapped.ma_session_id,
                                                    issued_at=int(datetime.now(UTC).timestamp()),
                                                )
                                    rotated_ids.add(mapped.ma_session_id)
                                    continue
                            if mapped.ma_session_id not in archived_ids:
                                await anthropic.beta.sessions.archive(mapped.ma_session_id)
                                if snapshot is not None:
                                    vault_id = snapshot.vault_id
                                    if snapshot.github_mode == "app":
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
                                            await archive_app_vault(anthropic, vault_id=vault_id)
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
    requester: Annotated[
        str,
        typer.Option(
            "--requester",
            help=(
                "Platform user ID of a workspace admin; mint the invitation on that admin's behalf."
            ),
        ),
    ],
    agent: Annotated[
        uuid.UUID | None, typer.Option("--agent", help="Agent UUID to connect.")
    ] = None,
    agent_name: Annotated[str | None, typer.Option("--agent-name", help="Agent name.")] = None,
) -> None:
    """Print a single-use connection invitation on a workspace admin's behalf."""
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
            if (agent is None) != (agent_name is None):
                raise ValueError("agent id and name must be supplied together")
            if agent is not None and agent_name is not None:
                async with AsyncAnthropic(
                    api_key=settings.anthropic.api_key.get_secret_value(),
                    base_url=str(settings.anthropic.base_url),
                ) as anthropic:
                    await _require_tenant_agent(
                        anthropic, tenant_id=tenant, agent_id=agent, agent_name=agent_name
                    )
            async with sessionmaker.begin() as session:
                account_id = await admin_account_for_platform_user(
                    session, tenant_id=tenant, external_id=requester
                )
                token = await mint_invitation(
                    session,
                    tenant_id=tenant,
                    requester_account_id=account_id,
                    requester_label=requester,
                    agent_id=agent,
                    agent_name=agent_name,
                    operator_issued=True,
                )
                workspace = await get_tenant(session, tenant)
                await append_event(
                    session,
                    tenant_id=tenant,
                    account_id=account_id,
                    agent_id=agent,
                    platform=workspace.platform if workspace is not None else None,
                    platform_user_id=requester,
                    tool_name="github_connect",
                    operation="github_connect",
                    outcome="allowed",
                    reason="admin link minted",
                )
            console.print(f"{root}/oauth/github/connect/{token}")
        finally:
            await engine.dispose()

    run_cli(_run(), console=console)
