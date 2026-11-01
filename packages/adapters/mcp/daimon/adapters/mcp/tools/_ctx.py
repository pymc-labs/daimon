"""Typed accessor for per-request AuthIdentity set by IdentityMiddleware."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity, token_channel_id
from daimon.adapters.mcp.tools._authz_facts import mcp_place, mcp_subject
from daimon.adapters.mcp.tools._words import admin_noun, space_noun
from daimon.core.authz import Action, AgentRef, Surface, authorize, build_subject
from daimon.core.billing import BillingConfig
from daimon.core.permissions import agent_permissions, any_agent_rules, any_own_readers
from daimon.core.security_audit import record_authz_denial
from daimon.core.spend_gate import SpendBlock, SpendRequest, spend_gate
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.turn.notices import RefusalNouns, admission_refusal_text
from daimon.core.turn.outcomes import TurnObservation, current_outcome
from daimon.core.turn.termination import TerminationReason, denial_termination_reason
from fastmcp import Context
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()


def _refusal_nouns(auth: AuthIdentity) -> RefusalNouns:
    """How a refusal names the caller's tenant, admin and commands; neutral for a caller
    with no chat platform."""
    return RefusalNouns.for_caller(auth.platform)


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
            f"This operation requires {admin_noun(auth.platform)}. Tell the caller who can "
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
    Returns the agent's home, the channel it is one of the own agents of
    (`AgentPermissions.home`), when ``agent_names`` is given, else None.
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
    gated = any_agent_rules(policy) or any_own_readers(policy)
    # An isolated channel's own agent is charged to that channel, so the
    # names are read for a hub admin too while anything is isolated.
    if agent_names is not None and gated and (not hub_admin or any_own_readers(policy)):
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
        gated = any_agent_rules(policy) or any_own_readers(policy)
    decision = (
        authorize(
            policy,
            subject=(
                # The hub mounts a person's own agent identity, never an
                # agent key; a demoted hub admin is held like any member.
                build_subject(
                    is_admin=False,
                    platform_user_id=auth.platform_user_id,
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
            gate="channel_rule" if decision.reason == "own_agents_only" else "agent_rule",
        )
        refused(denial_termination_reason(decision.reason))
        if decision.reason == "own_agents_only":
            raise ToolError(
                "TERMINAL ERROR: This agent's key is bound to a channel kept to its own "
                "agents, and this agent isn't one of them, so it can't run here."
            )
        raise ToolError(
            "TERMINAL ERROR: This agent's rule runs it only in certain channels, so it "
            "only runs in a conversation inside them, not from here. Ask it in one of "
            "those channels; a question about a shared report can't be answered here."
        )

    owner = agent_permissions(policy, names).home if names else None
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
            f"TERMINAL ERROR: You aren't on this {space_noun(auth.platform)}'s list of "
            f"people who can use daimon. {admin_noun(auth.platform).capitalize()} can add you."
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


_GATE_LOG: dict[SpendBlock, str] = {
    "balance_depleted": "balance",
    "cap_exceeded": "cap",
    "budget_exceeded": "channel_budget",
    "allowance_exhausted": "allowance",
}
_BLOCK_REASON: dict[SpendBlock, TerminationReason] = {
    "balance_depleted": TerminationReason.ADMISSION_BALANCE_DEPLETED,
    "cap_exceeded": TerminationReason.ADMISSION_CAP_EXCEEDED,
    "budget_exceeded": TerminationReason.ADMISSION_CHANNEL_BUDGET_EXCEEDED,
    "allowance_exhausted": TerminationReason.ADMISSION_DENIED,
}


async def _admit(  # pyright: ignore[reportUnusedFunction]
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    tool_name: str,
    markup: Decimal,
    agent_names: Callable[[], Awaitable[tuple[str | None, ...]]] | None = None,
    pin_exempt: bool = False,
    agent_id: str | None = None,
) -> AuthIdentity:
    """Spend gate for an already-resolved identity.

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
    - Then runs the one spend gate (``daimon.core.spend_gate``): balance, cap,
      the channel budget of the isolated channel whose own agent this is (an
      exempt caller's hub or DM run included, else a key's bound channel),
      then the person's spend allowances, on ``agent_id`` when the caller
      knows the agent (a derived id on a key does not name it);
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

    async with sessionmaker() as session:
        block = await spend_gate(
            session,
            billing_config=billing_config,
            request=SpendRequest(
                tenant_id=auth.tenant_id,
                platform_user_id=auth.platform_user_id,
                platform=auth.platform or "",
                channel_id=owner or token_channel_id(auth),
                agent_id=agent_id,
            ),
            markup=markup,
            now=datetime.now(UTC),
        )
    if block is None:
        return auth
    log.info(
        "mcp.admission_denied",
        tenant_id=str(auth.tenant_id),
        platform_user_id=auth.platform_user_id,
        tool=tool_name,
        gate=_GATE_LOG[block],
    )
    refused(_BLOCK_REASON[block])
    nouns = _refusal_nouns(auth)
    if block == "balance_depleted":
        raise ToolError(
            f"TERMINAL ERROR: This {nouns.scope}'s daimon credit is depleted. "
            f"An admin can top up with {nouns.billing}."
        )
    if block == "cap_exceeded":
        raise ToolError(f"TERMINAL ERROR: {admission_refusal_text('cap_exceeded', nouns)}")
    if block == "allowance_exhausted":
        raise ToolError(f"TERMINAL ERROR: {admission_refusal_text('allowance_exhausted', nouns)}")
    raise ToolError(
        "TERMINAL ERROR: This channel has used its spending budget. An admin can raise or clear it."
    )


async def _gate_origin_spend(  # pyright: ignore[reportUnusedFunction]
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    markup: Decimal,
    tool_name: str,
    channel_id: str | None,
    agent_id: str | None,
    budget_message: str,
) -> None:
    """The spend gate for a tool whose channel and agent are known only after admission.

    Runs the checks admission could not: the budget of the channel the spend counts
    toward and the person's allowance on the agent it counts toward. Balance and cap
    were decided by ``_admit``. No-ops for the unbilled path (no platform user).
    """
    if auth.platform_user_id is None:
        return
    async with sessionmaker() as session:
        block = await spend_gate(
            session,
            billing_config=None,
            request=SpendRequest(
                tenant_id=auth.tenant_id,
                platform_user_id=auth.platform_user_id,
                platform=auth.platform or "",
                channel_id=channel_id,
                agent_id=agent_id,
            ),
            markup=markup,
            now=datetime.now(UTC),
            checks=frozenset({"budget_exceeded", "allowance_exhausted"}),
        )
    if block is None:
        return
    log.info(
        "mcp.admission_denied",
        tenant_id=str(auth.tenant_id),
        platform_user_id=auth.platform_user_id,
        tool=tool_name,
        gate=_GATE_LOG[block],
    )
    _refused(sessionmaker, auth, tool_name, _BLOCK_REASON[block])
    if block == "allowance_exhausted":
        raise ToolError(
            f"TERMINAL ERROR: {admission_refusal_text('allowance_exhausted', _refusal_nouns(auth))}"
        )
    raise ToolError(budget_message)


async def _check_admission(  # pyright: ignore[reportUnusedFunction]
    ctx: Context,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    markup: Decimal,
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
        markup=markup,
        tool_name=tool_name,
        agent_names=None if agent_names is None else names,
    )
