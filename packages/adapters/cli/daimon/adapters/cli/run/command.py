"""`daimon run` — single-turn subprocess entrypoint."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import anthropic
import typer
from daimon.adapters.cli.logging import configure_admin_logging
from daimon.adapters.cli.run.events import (
    TerminalFailed,
    serialize_event,
    serialize_turn_state,
)
from daimon.adapters.cli.run.lifecycle import NdjsonLifecycle
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import load_settings, load_turn_settings
from daimon.core.errors import UserFacingError
from daimon.core.mux_backend import resource_scope
from daimon.core.stores.accounts import get_account
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.thread_sessions import session_owner_tenants
from daimon.core.tool_safety import trusted_servers_for
from daimon.core.turn.approvals import chat_tool_confirmation
from daimon.core.turn.ceiling import turn_deadline
from daimon.core.turn.driver import run_turn
from daimon.core.turn.outcomes import current_outcome, observe_turn
from daimon.core.turn.posture import BillingExempt, RequireApproval, ToolConfirmation
from daimon.core.turn.state import TurnState
from mux.errors import ScopeViolation
from sqlalchemy.ext.asyncio import AsyncSession

TurnPath = Literal["legacy", "mux"]


class SessionTenantMismatch(UserFacingError):
    """`daimon run` was pointed at a session that another tenant owns (mux path).

    The CLI acts as its own tenant's operator. It never mints a principal or
    account inside another tenant to reach that tenant's session.
    """

    def __init__(self, session_id: str, owner_tenant_id: uuid.UUID) -> None:
        super().__init__(
            f"session {session_id} belongs to tenant {owner_tenant_id}; "
            "run it from that tenant, not the local CLI tenant"
        )
        self.session_id = session_id
        self.owner_tenant_id = owner_tenant_id


def run_command(
    positional_message: Annotated[str | None, typer.Argument(metavar="[MESSAGE]")] = None,
    session: Annotated[str, typer.Option("--session", help="MA session id")] = "",
    message_flag: Annotated[
        str | None,
        typer.Option("--message", "-m", help="User message. Use '-' to read from stdin."),
    ] = None,
) -> None:
    configure_admin_logging()

    if not session:
        print("--session is required", file=sys.stderr)
        raise typer.Exit(code=1)
    if positional_message is not None and message_flag is not None:
        print("pass message as positional OR --message, not both", file=sys.stderr)
        raise typer.Exit(code=1)
    raw_message = positional_message if positional_message is not None else message_flag

    if raw_message is None:
        print("a user message is required for a new turn", file=sys.stderr)
        raise typer.Exit(code=1)

    user_message = _resolve_user_message(raw_message)

    settings = load_settings()
    # The turn path is resolved once, here at the edge, and passed down. The
    # conversation functions below never read settings themselves.
    path = load_turn_settings().path

    # No card surface here: with the tool-safety policy on, reads run and
    # third-party writes are refused (`no_confirmation_surface`); off, this is
    # the driver's default `RequireApproval`.
    tool_confirmation = chat_tool_confirmation(
        settings.tool_safety,
        requester_platform_user_id=settings.cli.local_user or "cli",
        confirm=None,
        trusted_servers=trusted_servers_for(
            str(settings.mcp.public_url) if settings.mcp.public_url is not None else None
        ),
    )

    async def _with_defaults() -> int:
        async with build_runtime(settings) as rt:
            return await run_conversation(
                rt=rt,
                session_id=session,
                user_message=user_message,
                path=path,
                tool_confirmation=tool_confirmation,
            )

    try:
        exit_code = asyncio.run(_with_defaults())
    except SessionTenantMismatch as err:
        print(f"✗ {err}", file=sys.stderr)
        raise typer.Exit(code=1) from err
    raise typer.Exit(code=exit_code)


# `RequireApproval` is frozen and field-less; one shared default (ruff B008).
_DEFAULT_TOOL_CONFIRMATION: ToolConfirmation = RequireApproval()


def _resolve_user_message(raw: str) -> str:
    if raw == "-":
        return sys.stdin.read()
    return raw


async def run_conversation(
    *,
    rt: CliRuntime,
    session_id: str,
    user_message: str,
    path: TurnPath,
    deadline: datetime | None = None,
    tool_confirmation: ToolConfirmation = _DEFAULT_TOOL_CONFIRMATION,
) -> int:
    with observe_turn(rt.sessionmaker, tenant_id=None, platform="cli"):
        return await run_conversation_observed(
            rt=rt,
            session_id=session_id,
            user_message=user_message,
            path=path,
            deadline=deadline,
            tool_confirmation=tool_confirmation,
        )


async def run_conversation_observed(
    *,
    rt: CliRuntime,
    session_id: str,
    user_message: str,
    path: TurnPath,
    deadline: datetime | None = None,
    tool_confirmation: ToolConfirmation = _DEFAULT_TOOL_CONFIRMATION,
) -> int:
    turn_id = f"turn_{uuid.uuid4().hex[:12]}"
    lifecycle = NdjsonLifecycle(stdout=sys.stdout, session_id=session_id, turn_id=turn_id)
    cancel = asyncio.Event()
    # `daimon run` is the other caller (besides headless_runner) that bypasses
    # run_prepared_turn, so it threads its own core-owned deadline into the
    # driver -- fail-safe like headless_runner: a caller that never passes one
    # still gets a full TURN_CEILING_S window. A human operator running this
    # interactively can always Ctrl-C, so this bound is a backstop rather than
    # the primary control; it exists so the CLI is not the one turn path left
    # without the core ceiling.
    effective_deadline = deadline if deadline is not None else turn_deadline(now=datetime.now(UTC))

    scope = None
    if path == "mux":
        # The operator's existing CLI identity supplies the opt-in turn scope.
        # Legacy raw-session runs keep their current database/provider work.
        async with rt.sessionmaker() as db:
            tenant_id = await discover_tenant(db)
            # Before any principal is minted: a session another tenant owns is
            # refused, never reached by creating a CLI identity in that tenant.
            await _refuse_foreign_session(db, session_id=session_id, tenant_id=tenant_id)
            principal = await get_or_create_cli_principal(
                db, tenant_id=tenant_id, os_user=rt.settings.cli.local_user
            )
            account = await get_account(db, principal.account_id)
            if (
                principal.tenant_id != tenant_id
                or account is None
                or account.tenant_id != tenant_id
            ):
                raise ScopeViolation(session_id, "CLI turn identity does not belong to the tenant")
            await db.commit()
        scope = resource_scope(
            tenant_id=str(tenant_id),
            account_id=str(account.id),
            authorization_id="cli-operator-run",
        )

    try:
        state = await run_turn(
            anthropic=rt.anthropic,
            session_id=session_id,
            user_message=user_message,
            lifecycle=lifecycle,
            cancel=cancel,
            billing=BillingExempt(reason="cli-operator-run"),
            tool_confirmation=tool_confirmation,
            deadline=effective_deadline,
            path=path,
            scope=scope,
        )
    except anthropic.APIError as err:
        if (observation := current_outcome.get()) is not None:
            observation.finish(error=err)
        _emit_failed_terminal(
            lifecycle,
            session_id=session_id,
            turn_id=turn_id,
            message=str(err),
        )
        return 1

    if (observation := current_outcome.get()) is not None:
        observation.finish(state=state)
    return 0 if state.error is None else 1


async def _refuse_foreign_session(
    db: AsyncSession, *, session_id: str, tenant_id: uuid.UUID
) -> None:
    """Raise `SessionTenantMismatch` when a tenant other than `tenant_id` owns the session.

    A session nothing records is allowed: `daimon sessions create` makes
    exactly that, an ephemeral session with no tenant stamp and no row
    (docs/billing.md), and it is the session `daimon run` normally targets.
    """
    for owner in sorted(await session_owner_tenants(db, ma_session_id=session_id)):
        if owner != tenant_id:
            raise SessionTenantMismatch(session_id, owner)


def _emit_failed_terminal(
    lifecycle: NdjsonLifecycle,
    *,
    session_id: str,
    turn_id: str,
    message: str,
) -> None:
    event = TerminalFailed(
        session_id=session_id,
        turn_id=turn_id,
        error={"kind": "upstream", "message": message},
        state=serialize_turn_state(TurnState()),
    )
    lifecycle.stdout.write(serialize_event(event) + "\n")
    lifecycle.stdout.flush()
