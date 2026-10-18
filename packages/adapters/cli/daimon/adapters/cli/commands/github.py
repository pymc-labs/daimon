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
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.github_retirement import (
    RETIREMENT_AFFECTED_AGENTS_SQL,
    RETIREMENT_LIVE_GRANTS_SQL,
    RETIREMENT_LIVE_SESSIONS_SQL,
    RETIREMENT_LOSS_SQL,
    RETIREMENT_TENANT_EVIDENCE_SQL,
    accepted_loss_agent_ids,
    resolve_retirement_tenants,
    retirement_repo_name_sql,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import archive_session, delete_credential, list_credentials, list_vaults
from daimon.core.session_mutation import session_mutation_fence
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import (
    list_agent_grants,
    remove_grant,
    stage_grant,
)
from daimon.core.stores.github_connect import (
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
from mux.contracts.ids import Scope
from rich.console import Console
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

github_app = typer.Typer(help="GitHub App connection commands.")
grants_app = typer.Typer(help="Manage live agent GitHub grants.")
github_app.add_typer(grants_app, name="grants")


def _repo_name(url: str) -> str:
    return normalize_owner_repo(url).casefold()


async def preview_legacy_retirement(session: AsyncSession, console: Console) -> None:
    """Show access and saved keys that the next migration will retire."""
    accepted_loss_ids = accepted_loss_agent_ids()
    if await session.scalar(text("SELECT to_regclass('agent_repo_binding')")) is None:
        console.print("GitHub legacy tables have already been retired.")
        return
    affected = (
        await session.execute(
            text(f"SELECT DISTINCT tenant_id, agent_id FROM ({RETIREMENT_AFFECTED_AGENTS_SQL}) a")
        )
    ).all()
    scoped_evidence = (await session.execute(text(RETIREMENT_TENANT_EVIDENCE_SQL))).all()
    live_sessions = (await session.execute(text(RETIREMENT_LIVE_SESSIONS_SQL))).all()
    _, unresolved = resolve_retirement_tenants(
        ((row.tenant_id, row.agent_id) for row in affected),
        ((row.tenant_id, row.agent_id) for row in scoped_evidence),
        ((row.tenant_id, row.ma_agent_id) for row in live_sessions),
    )
    if unresolved:
        for agent in sorted(unresolved):
            console.print(f"STOP: retired GitHub credential tenant unresolved for agent {agent}")
        raise RuntimeError("GitHub retirement has unresolved credential owners")
    staged = (
        await session.execute(
            text("""
            SELECT tenant_id, agent_id, repo_id FROM agent_github_grants
            WHERE staged ORDER BY tenant_id, agent_id, repo_id
        """)
        )
    ).all()
    counts: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    for tenant_id, agent_id, repo_id in staged:
        key = (tenant_id, agent_id)
        counts[key] = counts.get(key, 0) + 1
        console.print(f"DELETE staged grant tenant={tenant_id} agent={agent_id} repo_id={repo_id}")
    for (tenant_id, agent_id), count in sorted(counts.items()):
        console.print(f"staged grants tenant={tenant_id} agent={agent_id}: {count}")
    console.print(f"staged grants total: {len(staged)}")

    rows = (
        await session.execute(
            text(f"""
            WITH live_grants AS (
                {RETIREMENT_LIVE_GRANTS_SQL}
            ), affected AS (
                {RETIREMENT_AFFECTED_AGENTS_SQL}
            )
            SELECT a.tenant_id, a.agent_id, b.repo_url,
                EXISTS (SELECT 1 FROM agent_files f WHERE f.tenant_id=a.tenant_id
                    AND f.agent_id=a.agent_id
                    AND f.key IN ('GH_TOKEN','GITHUB_TOKEN')) AS saved_key,
                EXISTS (SELECT 1 FROM agent_github_binding gb
                    WHERE gb.agent_id=a.agent_id) AS github_binding,
                EXISTS (SELECT 1 FROM agent_skill_repo_credentials sc
                    WHERE sc.tenant_id=a.tenant_id AND sc.agent_id=a.agent_id) AS skill_binding,
                COALESCE((SELECT g.repo_full_name FROM live_grants g
                    WHERE g.tenant_id=a.tenant_id AND g.agent_id=a.agent_id
                      AND g.is_working_repo
                    LIMIT 1), '') AS app_working_repo,
                EXISTS (SELECT 1 FROM live_grants g
                    WHERE g.tenant_id=a.tenant_id AND g.agent_id=a.agent_id
                      AND b.repo_url IS NOT NULL
                      AND g.repo_name={retirement_repo_name_sql("b.repo_url")}) AS binding_covered
            FROM affected a LEFT JOIN agent_repo_binding b
              ON b.tenant_id=a.tenant_id AND b.agent_id=a.agent_id
            ORDER BY a.tenant_id, a.agent_id
        """)
        )
    ).all()
    loss_ids = set((await session.execute(text(RETIREMENT_LOSS_SQL))).scalars().all())
    failures: list[str] = []
    for row in rows:
        working = row.app_working_repo or row.repo_url or "none"
        covered = row.binding_covered if row.repo_url is not None else bool(row.app_working_repo)
        live_repo_rows = (
            await session.execute(
                text(f"""
                    WITH live_grants AS ({RETIREMENT_LIVE_GRANTS_SQL})
                    SELECT g.repo_full_name, g.repo_id FROM live_grants g
                    WHERE g.tenant_id=:tenant AND g.agent_id=:agent
                """),
                {"tenant": row.tenant_id, "agent": row.agent_id},
            )
        ).all()
        live_repo_names = {name.casefold() for name, _ in live_repo_rows}
        skill_repos = list(
            await session.scalars(
                text("""
                    SELECT repo_url FROM agent_skill_repo_credentials
                    WHERE tenant_id=:tenant AND agent_id=:agent ORDER BY repo_url
                """),
                {"tenant": row.tenant_id, "agent": row.agent_id},
            )
        )
        uncovered_skills = [repo for repo in skill_repos if _repo_name(repo) not in live_repo_names]
        accepted_agent = row.agent_id in accepted_loss_ids
        mode = await session.scalar(
            text("""
                SELECT mode FROM agent_github_mode
                WHERE tenant_id=:tenant AND agent_id=:agent
            """),
            {"tenant": row.tenant_id, "agent": row.agent_id},
        )
        loss = row.agent_id in loss_ids
        if loss and not accepted_agent:
            failures.append(
                f"agent {row.agent_id}: working repo, skill repo, or App grant access uncovered"
            )
        note = (
            f" accepted_loss=True expected_loss={_repo_name(row.repo_url or working)} "
            "until reconnected"
            if loss and accepted_agent
            else ""
        )
        console.print(
            f"RETIRE tenant={row.tenant_id} agent={row.agent_id} "
            f"saved_key={row.saved_key} repo_binding={row.repo_url is not None} "
            f"github_binding={row.github_binding} skill_binding={row.skill_binding} "
            f"legacy_binding_repo={row.repo_url or 'none'} "
            f"working_repo={working} live_app_grant_covers_working_repo={covered} "
            f"skill_repo_coverage={len(skill_repos) - len(uncovered_skills)}/{len(skill_repos)} "
            f"uncovered_skill_repos={uncovered_skills} "
            f"app_mode={mode == 'app'} loss={loss} "
            f"next_session_gh_token_source={'app' if covered else 'none'}{note}"
        )
    console.print(f"agents losing saved keys or bindings: {len(rows)}")
    if failures:
        for failure in failures:
            console.print(f"STOP: {failure}")
        raise RuntimeError("GitHub retirement would remove or cannot verify another agent's access")


@github_app.command("retirement-preview")
def retirement_preview() -> None:
    """Dry-run the App-only migration against the current database."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _run() -> None:
        engine = build_engine(str(settings.database.url))
        try:
            sessionmaker = build_session_factory(engine)
            async with sessionmaker() as session:
                await preview_legacy_retirement(session, console)
        finally:
            await engine.dispose()

    run_cli(_run(), console=console)


@github_app.command("cleanup-legacy-vault")
def cleanup_legacy_vault(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--apply", help="Preview or delete retired GitHub vault credentials."
        ),
    ] = True,
) -> None:
    """Delete only recorded legacy github.com repo credentials from MA vaults."""
    settings = load_settings()
    console = Console(highlight=False)

    async def _run() -> None:
        engine = build_engine(str(settings.database.url))
        try:
            sessionmaker = build_session_factory(engine)
            async with sessionmaker() as session:
                pending = (
                    await session.execute(
                        text(
                            "SELECT tenant_id, agent_id, credential_id "
                            "FROM github_legacy_vault_cleanup "
                            "WHERE deleted_at IS NULL ORDER BY tenant_id, agent_id, credential_id"
                        )
                    )
                ).all()
            console.print(f"{len(pending)} legacy credential reference(s) pending")
            if not pending:
                return
            scope = Scope.legacy_host_authorized(call_site="cli.github.cleanup_legacy_vault")
            async with AsyncAnthropic(
                api_key=settings.anthropic.resolved_api_key(),
                base_url=str(settings.anthropic.base_url),
            ) as anthropic:
                vaults = [vault async for vault in list_vaults(anthropic, scope=scope)]
                for tenant_id, agent_id, credential_id in pending:
                    # MA vaults are account scoped; the derived agent UUID is
                    # tenant scoped. Never search a different agent's vault.
                    matches: list[str] = []
                    for vault in vaults:
                        if not vault.display_name.startswith(
                            "daimon-mcp:"
                        ) or not vault.display_name.endswith(f":{agent_id}"):
                            continue
                        async for credential in list_credentials(anthropic, vault.id, scope=scope):
                            if credential.id != credential_id:
                                continue
                            url = getattr(credential.auth, "mcp_server_url", None)
                            if url != "https://github.com":
                                raise ValueError(
                                    f"{credential_id} is not a legacy repo credential; "
                                    "cleanup stopped"
                                )
                            matches.append(vault.id)
                    console.print(
                        f"{tenant_id} {agent_id} {credential_id}: "
                        + (f"{len(matches)} vault(s)" if matches else "already absent")
                    )
                    if dry_run:
                        continue
                    for vault_id in matches:
                        await delete_credential(anthropic, vault_id, credential_id, scope=scope)
                    async with sessionmaker.begin() as session:
                        await session.execute(
                            text(
                                "UPDATE github_legacy_vault_cleanup SET deleted_at = now() "
                                "WHERE tenant_id = :tenant AND agent_id = :agent "
                                "AND credential_id = :credential AND deleted_at IS NULL"
                            ),
                            {"tenant": tenant_id, "agent": agent_id, "credential": credential_id},
                        )
            console.print("Dry run complete." if dry_run else "Legacy vault cleanup complete.")
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
        action in ("add", "remove")
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
                if account is None or account.tenant_id != tenant or account.role is not Role.ADMIN:
                    raise ValueError("current CLI user is not a workspace admin")
                if action == "add":
                    if repo is None:
                        raise ValueError("provide a repository ID")
                    result = await stage_grant(
                        session,
                        tenant_id=tenant,
                        agent_id=agent,
                        repo_id=repo,
                        baseline_access="write",
                        ceiling_access="write",
                        granted_by_account_id=account_id,
                        is_working_repo=working,
                    )
                    console.print(f"updated {result.repo_id}")
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
                else:
                    rows = await list_agent_grants(session, tenant_id=tenant, agent_id=agent)
                    for row in rows:
                        console.print(f"{row.repo_id} live")
            if action in ("add", "remove"):
                async with sessionmaker() as session:
                    live = await list_live_sessions_for_agent(
                        session, tenant_id=tenant, agent_id=agent
                    )
                if live:
                    keys = tuple(secret.get_secret_value() for secret in settings.crypto.keys)
                    fernet = build_multifernet(keys) if keys else None
                    async with (
                        AsyncAnthropic(
                            api_key=settings.anthropic.resolved_api_key(),
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
                                action in ("add", "remove")
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
                                                vault_id=snapshot.vault_id,
                                                resource_ids=snapshot.repo_resource_ids,
                                                config=settings.github_app,
                                                fernet=fernet,
                                                unavailable_repos=(
                                                    current.effective_config.unavailable_repos
                                                    if current.effective_config is not None
                                                    else ()
                                                ),
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
                                scope = resource_scope(
                                    tenant_id=str(tenant), account_id=str(account.account_id)
                                )
                                await archive_session(anthropic, mapped.ma_session_id, scope=scope)
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
                                            await archive_app_vault(
                                                anthropic, vault_id=vault_id, scope=scope
                                            )
                                archived_ids.add(mapped.ma_session_id)
                            async with sessionmaker.begin() as session:
                                await mark_dead(session, id=mapped.id)
        finally:
            await engine.dispose()

    run_cli(_run(), console=console)


@grants_app.command("add")
def grants_add(
    tenant: Annotated[uuid.UUID, typer.Option("--tenant")],
    agent: Annotated[uuid.UUID, typer.Option("--agent")],
    repo: Annotated[int, typer.Option("--repo")],
    working: Annotated[bool, typer.Option("--working")] = False,
) -> None:
    _run_grant_command(tenant, "add", agent, repo=repo, working=working)


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
                    api_key=settings.anthropic.resolved_api_key(),
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
