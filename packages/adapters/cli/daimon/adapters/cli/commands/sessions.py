"""`daimon sessions create` and `daimon sessions get`.

`create` composes adapter preconditions, name resolution, and
`daimon.core.sessions.create_session` (ephemeral MA session). `get` is a
thin proxy around `client.beta.sessions.retrieve`.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.logging import configure_admin_logging
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.sessions_bootstrap import (
    check_preconditions,
    resolve_agent_and_environment,
)
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import load_settings
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.sessions import SessionContext, create_session
from daimon.core.stores.identity import get_or_create_cli_principal
from rich.console import Console

sessions_app = typer.Typer(help="Sessions: create, get")


@sessions_app.command("create")
def sessions_create_command(
    agent: Annotated[str | None, typer.Option("--agent", help="Agent name")] = None,
    environment: Annotated[
        str | None, typer.Option("--environment", help="Environment name")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON (default: bare id)")] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await sessions_create(
                rt=rt,
                console=console,
                agent_flag=agent,
                environment_flag=environment,
                as_json=as_json,
            )

    run_cli(_with_defaults(), console=console)


async def sessions_create(
    *,
    rt: CliRuntime,
    console: Console,
    agent_flag: str | None,
    environment_flag: str | None,
    as_json: bool,
) -> None:
    configure_admin_logging()
    async with rt.sessionmaker() as db:
        tenant_id = await discover_tenant(db)
        await db.commit()
    await check_preconditions(rt.sessionmaker, tenant_id=tenant_id, default=rt.deployment_default)
    async with rt.sessionmaker() as db:
        principal = await get_or_create_cli_principal(
            db, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
        )
        await db.commit()
    agent_row, env_row = await resolve_agent_and_environment(
        rt.sessionmaker,
        rt.anthropic,
        tenant_id=tenant_id,
        account_id=principal.account_id,
        agent_flag=agent_flag,
        environment_flag=environment_flag,
        defaults_root=rt.settings.defaults_root,
        default=rt.deployment_default,
        cache=rt.resolver_cache,
    )
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(agent_row.id))
    ma_session = await create_session(
        rt.anthropic,
        account_id=principal.account_id,
        agent=agent_row,
        environment=env_row,
        mcp_settings=rt.settings.mcp,
        agent_uuid=agent_uuid,
        session_context=SessionContext(is_admin=False),
        github_fallback_pat=(
            rt.settings.github.fallback_pat.get_secret_value()
            if rt.settings.github.fallback_pat is not None
            else None
        ),
        github_app_id=rt.settings.github.app_id,
        github_app_private_key=(
            rt.settings.github.app_private_key.get_secret_value()
            if rt.settings.github.app_private_key is not None
            else None
        ),
    )

    if as_json:
        print(
            json.dumps(
                {
                    "session_id": ma_session.id,
                    "agent": agent_row.name,
                    "environment": env_row.name,
                }
            )
        )
    else:
        print(ma_session.id)


@sessions_app.command("get")
def sessions_get_command(
    session_id: Annotated[str, typer.Argument(help="MA session id")],
    as_json: Annotated[bool, JSON_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_defaults() -> None:
        async with build_runtime(settings) as rt:
            await sessions_get(rt=rt, console=console, session_id=session_id, as_json=as_json)

    run_cli(_with_defaults(), console=console)


async def sessions_get(*, rt: CliRuntime, console: Console, session_id: str, as_json: bool) -> None:
    configure_admin_logging()
    session = await rt.anthropic.beta.sessions.retrieve(session_id)
    emit_rows(
        console,
        [session],
        columns=("id", "status", "environment_id", "title", "created_at", "updated_at"),
        as_json=as_json,
    )
