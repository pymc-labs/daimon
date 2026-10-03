"""Typed accessor for per-request AuthIdentity set by IdentityMiddleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.tools._authz_facts import mcp_place, mcp_subject
from daimon.core.authz import Action, AgentRef, Surface, authorize, build_subject
from daimon.core.billing import BillingConfig, is_over_cap
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.permissions import agent_permissions, any_confidential, any_pinned
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.tenant_balance import is_over_balance
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.termination import TerminationReason, denial_termination_reason
from fastmcp import Context
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

# How MCP refusals name the tenant, matching the invoker allowlist refusal below.
_REFUSAL_NOUNS = RefusalNouns(scope="workspace", admin="a workspace admin", billing="/billing")


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


def _refused(
    sessionmaker: async_sessionmaker[AsyncSession],
    auth: AuthIdentity,
    tool_name: str,
    reason: TerminationReason,
) -> None:
    """Record a refused turn; a re-check inside a running turn finishes that turn's row."""
    if tool_name not in {"ask", "start_turn", "continue_turn"}:
        return
    if (observation := current_outcome.get()) is not None:
        observation.finish(reason=reason)
        return
    TurnObservation(
        sessionmaker,
        auth.tenant_id,
        "mcp",
        account_id=auth.account_id,
        agent_id=str(auth.agent_id) if auth.agent_id is not None else None,
    ).finish(reason=reason)


async def _policy_gate(
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
    agent_names: Callable[[], Awaitable[tuple[str | None, ...]]] | None,
    pin_exempt: bool,
) -> str | None:
    """The access-policy half of ``_admit``: the agent pin, then the invoker allowlist.

    Reads the policy fresh on every call, so ``_admission_recheck`` can run it
    again right before a turn's session is created or its message is sent.
    Returns the isolated channel the agent is one of the own agents of
    (`isolation_owner`), when ``agent_names`` is given, else None.
    """

    def refused(reason: TerminationReason) -> None:
        _refused(sessionmaker, auth, tool_name, reason)

    try:
        async with sessionmaker() as session:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
            account = await get_account(session, auth.account_id)
    except AccessPolicyUnreadable as exc:
        refused(TerminationReason.ADMISSION_DENIED)
        raise ToolError(f"TERMINAL ERROR: {exc}.") from exc

    # The hub admin exemption needs no agent lookup (`authorize` allows it
    # before reading names), so skip the lookup for it unless the budget needs
    # it (below). The stored role is read again here, so a re-check also sees
    # a demotion saved meanwhile.
    hub_admin = (
        pin_exempt
        and auth.platform_user_id is not None
        and account is not None
        and account.role is Role.ADMIN
    )
    names: tuple[str | None, ...] = ()
    gated = any_pinned(policy) or any_confidential(policy)
    # An isolated channel's own agent is charged to that channel, so the
    # names are read for a hub admin too while anything is isolated.
    if agent_names is not None and gated and (not hub_admin or any_confidential(policy)):
        # Resolving the agent's names may await the network (an MA lookup);
        # read the policy again after it, so the pin is decided on the policy
        # as it is once every await is done, not as it was before the lookup.
        names = await agent_names()
        try:
            async with sessionmaker() as session:
                policy = await load_access_policy(session, tenant_id=auth.tenant_id)
                account = await get_account(session, auth.account_id)
        except AccessPolicyUnreadable as exc:
            refused(TerminationReason.ADMISSION_DENIED)
            raise ToolError(f"TERMINAL ERROR: {exc}.") from exc
        hub_admin = (
            pin_exempt
            and auth.platform_user_id is not None
            and account is not None
            and account.role is Role.ADMIN
        )
        gated = any_pinned(policy) or any_confidential(policy)
    decision = (
        authorize(
            policy,
            subject=(
                # The hub mounts a person's own agent identity, never an
                # agent key; a demoted hub admin is held like any member.
                # A channel admin keeps their grants: an isolated channel's
                # own agent answers them in their hub, as it does an admin.
                build_subject(
                    is_admin=False,
                    platform_user_id=auth.platform_user_id,
                    administered_channel_ids=auth.administered_channel_ids,
                )
                if pin_exempt
                else mcp_subject(auth)
            ),
            action=Action.RUN_AGENT,
            surface=Surface.HUB if pin_exempt else Surface.AGENT_CHAT,
            agent=AgentRef.of(*names),
            place=mcp_place(auth),
        )
        if agent_names is not None and gated and not hub_admin
        else None
    )
    if decision is not None and not decision:
        record_authz_denial(Action.RUN_AGENT, decision.reason)
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="channel_isolation" if decision.reason == "channel_isolated" else "agent_pin",
        )
        refused(denial_termination_reason(decision.reason))
        if decision.reason == "channel_isolated":
            raise ToolError(
                "TERMINAL ERROR: This agent's key is bound to a confidential channel that "
                "only that channel's own agents answer in, so it can't run here."
            )
        raise ToolError(
            "TERMINAL ERROR: An operator pinned this agent to specific channels, so it "
            "only runs in a conversation inside them, not from here. Ask it in one of "
            "those channels; a question about a shared report can't be answered here."
        )

    owner = agent_permissions(policy, names).own_channel if names else None
    if auth.platform_user_id is None:
        return owner
    is_admin = auth.is_admin or (account is not None and account.role is Role.ADMIN)
    start = authorize(
        policy,
        subject=mcp_subject(auth, is_admin=is_admin),
        action=Action.START_TURN,
    )
    if not start:
        record_authz_denial(Action.START_TURN, start.reason)
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
    return owner


def _admission_recheck(  # pyright: ignore[reportUnusedFunction]
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tool_name: str,
    agent_names: Callable[[], Awaitable[tuple[str | None, ...]]] | None,
    pin_exempt: bool = False,
) -> Callable[[], Awaitable[None]]:
    """The policy decision of ``_admit`` again, for the moment a turn acts.

    ``_admit`` decides at the start of the tool call; the session create and
    the message send come after MA round trips, and a pin or allowlist change
    saved in between must still stop them. The agent-chat and hub turn tools
    hand this to the turn implementation, which awaits it immediately before
    each create and send (a resumed handle included). Billing is not
    re-checked: it was charged against at admission.
    """

    async def recheck() -> None:
        await _policy_gate(
            auth,
            sessionmaker=sessionmaker,
            tool_name=tool_name,
            agent_names=agent_names,
            pin_exempt=pin_exempt,
        )

    return recheck


async def _admit(  # pyright: ignore[reportUnusedFunction]
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    tool_name: str,
    agent_names: Callable[[], Awaitable[tuple[str | None, ...]]] | None = None,
    pin_exempt: bool = False,
) -> AuthIdentity:
    """Balance and cap gate for an already-resolved identity.

    ``_check_admission`` wraps this for tools whose identity is the request's
    own; the hub mounts call it directly because their identity is chosen per
    call from the daimon being addressed.

    - ``platform_user_id is None`` is the trusted, fully-unbilled path:
      CLI-only/internal operator tokens run with no balance/cap checks, no
      usage row, no debit. This is intentional, not an oversight — never add
      a fallback that bills this path.
      It skips billing only: when ``agent_names`` is given its turn is still
      held to an agent pin (below), with no admin exemption.
    - Otherwise checks the tenant's invoker allowlist first, exempting an
      account whose stored role is admin (the hub pins ``is_admin=False``, so
      the stored role is the only admin signal every caller has). A refusal,
      or a policy that can't be read, raises a ``TERMINAL ERROR:`` ``ToolError``.
    - Then, when ``agent_names`` is given and the tenant pins any agent,
      refuses a turn on a pinned agent outside its pin. An MCP turn has no
      channel, so it is outside every pin, exactly as a DM is in ``admit()``,
      unless its agent key was minted in a channel (``token_channel_id``),
      which is then where it runs. The pin is a
      security gate, not a billing one: it is enforced for no-platform bearer
      and agent-key identities too, before the unbilled return below, with no
      admin exemption. Only the hub passes ``pin_exempt``, for a caller
      whose stored role is admin (`_session_access.load_hub_subject`;
      refreshed by the person's next platform turn); its reply reaches only
      them.
      ``agent_names`` is called only when a pin exists, so the agent lookup it
      may need costs nothing on unpinned tenants.
    - Then runs ``is_over_balance`` then ``is_over_cap``, then the channel
      budget of the isolated channel whose own agent this is (an exempt
      caller's hub or DM run included), else of a key's bound channel;
      each denial raises a ``TERMINAL ERROR:`` ``ToolError`` and logs a deny
      event carrying only ids (tenant/user/tool/gate) — never prompt content
      or raw Gemini text (Pitfall 9).
    """

    def refused(reason: TerminationReason) -> None:
        _refused(sessionmaker, auth, tool_name, reason)

    if auth.platform_user_id is None and agent_names is None:
        return auth

    owner = await _policy_gate(
        auth,
        sessionmaker=sessionmaker,
        tool_name=tool_name,
        agent_names=agent_names,
        pin_exempt=pin_exempt,
    )
    if auth.platform_user_id is None:
        return auth

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
        raise ToolError(f"TERMINAL ERROR: {admission_refusal_text('cap_exceeded', _REFUSAL_NOUNS)}")

    if await is_over_channel_budget(
        sessionmaker=sessionmaker,
        tenant_id=auth.tenant_id,
        platform=auth.platform or "",
        channel_id=owner or token_channel_id(auth),
        now=datetime.now(UTC),
    ):
        log.info(
            "mcp.admission_denied",
            tenant_id=str(auth.tenant_id),
            platform_user_id=auth.platform_user_id,
            tool=tool_name,
            gate="channel_budget",
        )
        refused(TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED)
        raise ToolError(
            "TERMINAL ERROR: This channel has used its spending budget. "
            "An admin can raise or clear it."
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
