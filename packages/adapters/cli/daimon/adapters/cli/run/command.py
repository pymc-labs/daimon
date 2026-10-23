"""`daimon run` — single-turn subprocess entrypoint."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import anthropic
import typer
from anthropic.types.beta import BetaManagedAgentsSession
from daimon.adapters.cli.logging import configure_admin_logging
from daimon.adapters.cli.run.events import (
    TerminalFailed,
    serialize_event,
    serialize_turn_state,
)
from daimon.adapters.cli.run.lifecycle import NdjsonLifecycle
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.adapters.cli.tenant import discover_tenant
from daimon.core.config import Settings, load_settings, load_turn_settings
from daimon.core.defaults.metadata import MA_METADATA_KEY_TENANT
from daimon.core.errors import UserFacingError
from daimon.core.github_credentials import build_multifernet
from daimon.core.mux_backend import platform_scope, resource_scope
from daimon.core.session_ports_compat import retrieve_session_record
from daimon.core.stores.accounts import get_account
from daimon.core.stores.identity import get_or_create_cli_principal
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import session_owner_tenants
from daimon.core.turn.ceiling import turn_deadline
from daimon.core.turn.driver import run_turn, unattended_sends
from daimon.core.turn.outcomes import current_outcome, observe_turn
from daimon.core.turn.posture import BillingExempt, RequireApproval, ToolConfirmation
from daimon.core.turn.state import TurnState
from daimon.core.turn.unattended import (
    session_gate,
    session_rules_channel,
    unattended_posture,
)
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
    path = load_turn_settings().path
    try:
        exit_code = asyncio.run(_run_with_defaults(settings, session, user_message, path=path))
    except SessionTenantMismatch as err:
        print(f"✗ {err}", file=sys.stderr)
        raise typer.Exit(code=1) from err
    raise typer.Exit(code=exit_code)


async def _run_with_defaults(
    settings: Settings, session: str, user_message: str, *, path: TurnPath | None = None
) -> int:
    """Gate the operator's session, then run the turn under the unattended rules.

    Nobody answers a card here, so this is an unattended turn: before the
    message the session's own tool config is rewritten from the current
    policy (`gate_session_snapshot`), whatever it was created with, and every
    pause is decided by `unattended_posture`.
    """
    path = path if path is not None else load_turn_settings().path
    async with build_runtime(settings) as rt:
        public_url = str(settings.mcp.public_url) if settings.mcp.public_url is not None else None
        scope = platform_scope("CLI operator run gate")
        snapshot = await retrieve_session_record(rt.anthropic, session, scope=scope)
        tenant_id, platform = await _session_tenant(rt, snapshot)
        if path == "mux":
            async with rt.sessionmaker() as db:
                local_tenant = await discover_tenant(db)
                if tenant_id != local_tenant:
                    raise SessionTenantMismatch(session, tenant_id)
                await _refuse_foreign_session(db, session_id=session, tenant_id=local_tenant)
        gate = session_gate(
            rt.anthropic,
            session,
            settings.tool_safety,
            scope=scope,
            public_url=public_url,
            session_factory=rt.sessionmaker,
            tenant_id=tenant_id,
            entry_point="cli_run",
        )
        # Decide on what a first gate leaves (a healed reserved URL, for one);
        # the driver gates again before the message and every confirmation.
        await gate()
        snapshot = await retrieve_session_record(rt.anthropic, session, scope=scope)
        keys = tuple(key.get_secret_value() for key in rt.settings.crypto.keys)
        with unattended_sends(gate):
            return await run_conversation(
                rt=rt,
                session_id=session,
                user_message=user_message,
                path=path,
                tool_confirmation=unattended_posture(
                    settings.tool_safety,
                    session=snapshot,
                    public_url=public_url,
                    session_factory=rt.sessionmaker,
                    tenant_id=tenant_id,
                    channel_id=session_rules_channel(snapshot, platform=platform),
                    fernet=build_multifernet(keys) if keys else None,
                    entry_point="cli_run",
                ),
            )


async def _session_tenant(
    rt: CliRuntime, session: BetaManagedAgentsSession
) -> tuple[uuid.UUID, str]:
    """The tenant and platform whose rules the operator's session runs under.

    A session stamped for a tenant (a chat or agent-chat conversation) keeps
    that tenant's rules and its channel's; only an unstamped operator session
    falls back to the local CLI tenant.
    """
    stamped = (session.metadata or {}).get(MA_METADATA_KEY_TENANT)
    async with rt.sessionmaker() as db:
        if stamped is None:
            return await discover_tenant(db), "cli"
        tenant = await get_tenant(db, uuid.UUID(stamped))
    if tenant is None:
        raise ScopeViolation(session.id, "the session's tenant is not on this deployment")
    return tenant.id, tenant.platform


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
