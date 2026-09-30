"""Typed accessor for per-request AuthIdentity set by IdentityMiddleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.access_policy import is_invoker_allowed, is_outside_agent_pin
from daimon.core.billing import BillingConfig, is_over_cap
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.tenant_balance import is_over_balance
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.termination import TerminationReason
from fastmcp import Context
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


async def _auth(ctx: Context) -> AuthIdentity:  # pyright: ignore[reportUnusedFunction]
    """Return the AuthIdentity seeded into request state by IdentityMiddleware.

    Raises ToolError if the state is missing — this is a programming error
    (middleware failed to run), not a caller-facing condition.
    """
    identity = await ctx.get_state("auth")
    if not isinstance(identity, AuthIdentity):
        raise ToolError("internal: missing auth context")
    return identity


def _require_admin(auth: AuthIdentity) -> None:  # pyright: ignore[reportUnusedFunction]
    """Raise ToolError if the caller is not an admin.

    Call at the top of every mutating _*_impl to enforce admin chat gating.
    Reads (list_*/get_*/self_read*/self_list*) stay ungated.
    """
    if not auth.is_admin:
        raise ToolError(
            "This operation requires a workspace or server admin. Tell the caller who can "
            "make the change and give them a sentence the admin can say, preserving "
            "the requested action and target from the conversation. They are not available "
            "to this permission check; do not invent missing details. Do not retry the mutation."
        )


async def _admit(  # pyright: ignore[reportUnusedFunction]
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    tool_name: str,
    agent_names: Callable[[], Awaitable[tuple[str | None, ...]]] | None = None,
) -> AuthIdentity:
    """Balance and cap gate for an already-resolved identity.

    ``_check_admission`` wraps this for tools whose identity is the request's
    own; the hub mounts call it directly because their identity is chosen per
    call from the daimon being addressed.

    - ``platform_user_id is None`` is the trusted, fully-unbilled path:
      CLI-only/internal operator tokens run with no balance/cap checks, no
      usage row, no debit. This is intentional, not an oversight — never add
      a fallback that bills this path.
      The tenant access policy does not apply to it either: it is the
      operator, not a platform member.
    - Otherwise checks the tenant's invoker allowlist first, exempting an
      account whose stored role is admin (the hub pins ``is_admin=False``, so
      the stored role is the only admin signal every caller has). A refusal,
      or a policy that can't be read, raises a ``TERMINAL ERROR:`` ``ToolError``.
    - Then, when ``agent_names`` is given and the tenant pins any agent,
      refuses a turn on a pinned agent. An MCP turn has no channel, so it is
      outside every pin, exactly as a DM is in ``admit()``; admins get no
      exemption. ``agent_names`` is called only when a pin exists, so the
      agent lookup it may need costs nothing on unpinned tenants.
    - Then runs ``is_over_balance`` then ``is_over_cap``; either denial
      raises a ``TERMINAL ERROR:`` ``ToolError`` naming ``/billing`` and logs
      a deny event carrying only ids (tenant/user/tool/gate) — never prompt
      content or raw Gemini text (Pitfall 9).
    """

    def refused(reason: TerminationReason) -> None:
        if tool_name in {"ask", "start_turn", "continue_turn"}:
            TurnObservation(
                sessionmaker,
                auth.tenant_id,
                "mcp",
                account_id=auth.account_id,
                agent_id=str(auth.agent_id) if auth.agent_id is not None else None,
            ).finish(reason=reason)

    if auth.platform_user_id is None:
        return auth

    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
            account = await get_account(session, auth.account_id)
    except AccessPolicyUnreadable as exc:
        refused(TerminationReason.ADMISSION_DENIED)
        raise ToolError(f"TERMINAL ERROR: {exc}.") from exc
    is_admin = auth.is_admin or (account is not None and account.role is Role.ADMIN)
    if not is_invoker_allowed(policy, external_user_id=auth.platform_user_id, is_admin=is_admin):
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="invoker_policy",
        )
        refused(TerminationReason.ADMISSION_DENIED)
        raise ToolError(
            "TERMINAL ERROR: You aren't on this workspace's list of people who can "
            "use daimon. A workspace admin can add you."
        )

    if (
        agent_names is not None
        and policy.agent_channel_pins
        and is_outside_agent_pin(policy, agent_names=await agent_names(), channel_id=None)
    ):
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="agent_pin",
        )
        refused(TerminationReason.ADMISSION_DENIED)
        raise ToolError(
            "TERMINAL ERROR: An operator pinned this agent to specific channels, so it "
            "only runs in a conversation inside them, not from here. Talk to it in its "
            "channel."
        )

    if await is_over_balance(sessionmaker=sessionmaker, tenant_id=auth.tenant_id):
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="balance",
        )
        refused(TerminationReason.ADMISSION_BALANCE_DEPLETED)
        raise ToolError(
            "TERMINAL ERROR: This server's daimon credit is depleted. "
            "An admin can top up with /billing."
        )

    if await is_over_cap(
        billing_config=billing_config,
        sessionmaker=sessionmaker,
        tenant_id=auth.tenant_id,
        user_id=auth.platform_user_id,
        now=datetime.now(UTC),
    ):
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="cap",
        )
        refused(TerminationReason.ADMISSION_CAP_EXCEEDED)
        raise ToolError(
            "TERMINAL ERROR: Monthly usage cap reached for this guild. "
            "An admin can adjust the cap with /billing."
        )

    return auth


async def _check_admission(  # pyright: ignore[reportUnusedFunction]
    ctx: Context,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    tool_name: str,
    agent_names: Callable[[AuthIdentity], Awaitable[tuple[str | None, ...]]] | None = None,
) -> AuthIdentity:
    """Shared admission gate for the billed media and agent-chat turn tools. See ``_admit``."""
    auth = await _auth(ctx)

    async def names() -> tuple[str | None, ...]:
        assert agent_names is not None
        return await agent_names(auth)

    return await _admit(
        auth,
        sessionmaker=sessionmaker,
        billing_config=billing_config,
        tool_name=tool_name,
        agent_names=None if agent_names is None else names,
    )
