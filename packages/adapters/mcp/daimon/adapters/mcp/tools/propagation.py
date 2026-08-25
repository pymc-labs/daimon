"""Propagation tools: set and clear agent defaults at workspace or channel scope.

``register_propagation_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures for
this group; each closure delegates to a module-private ``_*_impl`` function
that can be unit-tested without a FastMCP Context.

These tools close the conversational-parity gap: there was no
MCP tool for propagation / set-default. The same core scoped-config writes that the
modal scope picker uses (``set_fields`` / ``unset_fields`` / ``get_scope``) are now
reachable conversationally via ``@bot help me set up``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.routing_facts import (
    build_clear_default_note,
    build_resolution_note,
    build_set_default_note,
)
from daimon.core.scope import (
    ChannelConfigRow,
    ChannelScopeRef,
    TenantConfigRow,
    TenantScopeRef,
    merge,
)
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
from fastmcp import Context, FastMCP


@dataclass(frozen=True)
class SetDefaultResult:
    """Result returned from set_agent_default."""

    scope: str
    """'workspace' or 'channel:<channel_id>'"""
    agent_name: str
    """The newly-set default agent name."""
    previous_agent_name: str | None
    """The agent name that was overwritten, or None if the scope had no prior default."""
    routing_note: str
    """The routing truth the caller should report back: members reach the agent
    only by @mentioning the bot, and there is one bot for the whole workspace,
    not one per agent. Supplied by the tool rather than recalled from a prompt."""


@dataclass(frozen=True)
class ClearDefaultResult:
    """Result returned from clear_agent_default."""

    scope: str
    """'workspace' or 'channel:<channel_id>'"""
    cleared: bool
    """True if there was an agent_name to clear; False if the scope had none."""
    routing_note: str
    """The routing truth the caller should report back: which scope no longer
    has a default (or had none to begin with), and the same mention
    requirement. Supplied by the tool rather than recalled from a prompt."""


async def _set_agent_default_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
    channel_id: str | None,
) -> SetDefaultResult:
    _require_admin(auth)

    tenant_id: uuid.UUID = auth.tenant_id
    if channel_id is not None:
        scope: ChannelScopeRef | TenantScopeRef = ChannelScopeRef(
            tenant_id=tenant_id, channel_id=channel_id
        )
        scope_label = f"channel:{channel_id}"
    else:
        scope = TenantScopeRef(tenant_id=tenant_id)
        scope_label = "workspace"

    async with runtime.session_factory.begin() as session:
        prior = await get_scope(session, scope=scope)
        prior_agent: str | None = prior.agent_name if prior is not None else None
        await set_fields(
            session,
            scope=scope,
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
            actor_account_id=auth.account_id,
        )

    return SetDefaultResult(
        scope=scope_label,
        agent_name=agent_name,
        previous_agent_name=prior_agent,
        routing_note=build_set_default_note(agent_name=agent_name, scope_label=scope_label),
    )


async def _clear_agent_default_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str | None,
) -> ClearDefaultResult:
    _require_admin(auth)

    tenant_id: uuid.UUID = auth.tenant_id
    if channel_id is not None:
        scope: ChannelScopeRef | TenantScopeRef = ChannelScopeRef(
            tenant_id=tenant_id, channel_id=channel_id
        )
        scope_label = f"channel:{channel_id}"
    else:
        scope = TenantScopeRef(tenant_id=tenant_id)
        scope_label = "workspace"

    async with runtime.session_factory.begin() as session:
        prior = await get_scope(session, scope=scope)
        had_default = prior is not None and prior.agent_name is not None
        if had_default:
            await unset_fields(
                session,
                scope=scope,
                fields=["agent_name"],
                actor_account_id=auth.account_id,
            )

    return ClearDefaultResult(
        scope=scope_label,
        cleared=had_default,
        routing_note=build_clear_default_note(scope_label=scope_label, cleared=had_default),
    )


@dataclass(frozen=True)
class AgentResolutionExplanation:
    """Result returned from explain_agent_resolution."""

    channel_id: str
    """The channel the question was asked about."""
    effective_agent_name: str | None
    """The agent that would actually answer a mention in that channel."""
    winning_tier: str | None
    """Which tier supplied it: 'channel', 'tenant', or 'deployment'."""
    channel_default: str | None
    """The channel tier's own setting, or None if it has none."""
    tenant_default: str | None
    """The workspace tier's own setting, or None if it has none."""
    deployment_default: str | None
    """The deployment fallback from defaults/config.yaml."""
    effective_environment_name: str | None
    """The environment that would be used, resolved over the same cascade."""
    environment_winning_tier: str | None
    """Which tier supplied the environment."""
    explanation: str
    """One sentence naming the winner and the tier it came from, so the caller
    can answer 'why that one' without re-deriving the cascade."""


async def _explain_agent_resolution_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str,
) -> AgentResolutionExplanation:
    """Resolve the cascade for one channel and report every tier's contribution.

    Deliberately NOT admin-gated. This is a read of routing that any member can
    already infer from a turn's footer, and the people most often confused about
    which agent answers are ordinary members. Gating it would leave the question
    unanswerable by exactly the callers who ask it.
    """
    tenant_id: uuid.UUID = auth.tenant_id

    async with runtime.session_factory() as session:
        channel_row = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id)
        )
        tenant_row = await get_scope(session, scope=TenantScopeRef(tenant_id=tenant_id))

    channel_cfg = channel_row if isinstance(channel_row, ChannelConfigRow) else None
    tenant_cfg = tenant_row if isinstance(tenant_row, TenantConfigRow) else None
    resolved = merge(channel=channel_cfg, tenant=tenant_cfg, default=runtime.deployment_default)

    return AgentResolutionExplanation(
        channel_id=channel_id,
        effective_agent_name=resolved.agent_name,
        winning_tier=resolved.agent_name_tier,
        channel_default=channel_cfg.agent_name if channel_cfg is not None else None,
        tenant_default=tenant_cfg.agent_name if tenant_cfg is not None else None,
        deployment_default=runtime.deployment_default.agent_name,
        effective_environment_name=resolved.environment_name,
        environment_winning_tier=resolved.environment_name_tier,
        explanation=build_resolution_note(
            agent_name=resolved.agent_name,
            tier=resolved.agent_name_tier,
            channel_id=channel_id,
        ),
    )


def register_propagation_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def set_agent_default(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        channel_id: str | None = None,
    ) -> SetDefaultResult:
        """Set the agent that responds by default in a channel or the whole workspace.

        When ``channel_id`` is provided the default is scoped to that channel;
        omit it to set the workspace-wide default.  Any existing default at the
        chosen scope is replaced (last-write-wins; an audit stamp is recorded by
        core).  Requires Manage Server (admin).

        Discord: ``channel_id`` MUST be the parent channel's id — the one
        your context gives as
        ``<channel platform="discord" id="..." role="parent_channel">``.
        Never pass the current thread's id here. Slack: use the id from
        ``<channel platform="slack" id="...">`` — Slack's context always
        names the current channel, with no thread-vs-parent split. A turn
        always resolves its agent from the parent channel, so a default
        written against a thread id is a scope nothing ever reads: the write
        succeeds, this tool reports success, and the channel keeps answering
        with the old agent.
        """
        return await _set_agent_default_impl(runtime, await _auth(ctx), agent_name, channel_id)

    @mcp.tool(tags={"admin"})
    async def clear_agent_default(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str | None = None,
    ) -> ClearDefaultResult:
        """Remove the agent default from a channel or the whole workspace.

        When ``channel_id`` is provided only that channel's default is cleared;
        omit it to clear the workspace-wide default.  If the scope had no
        default the call is a no-op (idempotent).  Requires Manage Server (admin).

        Discord: ``channel_id`` MUST be the parent channel's id
        (``<channel platform="discord" id="..." role="parent_channel">``),
        never the current thread's id. Slack: use the id from
        ``<channel platform="slack" id="...">``. Clearing a thread id is a
        silent no-op that leaves the channel's real default in place.
        """
        return await _clear_agent_default_impl(runtime, await _auth(ctx), channel_id)

    @mcp.tool
    async def explain_agent_resolution(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
    ) -> AgentResolutionExplanation:
        """Report which agent answers in a channel, and which tier decided it.

        Resolution is a cascade: the channel's own default wins, else the
        workspace default, else the deployment default. This reports the winner
        AND every tier's setting, so "why that agent" is answerable without
        starting a turn and reading its footer.

        Use it when someone asks which agent is configured here, when an agent
        answers that seems wrong for the channel, or before changing a default —
        the tier that currently wins is the tier worth changing.

        Not admin-gated: this is a read of routing any member can already infer
        from a reply's footer.

        Discord: ``channel_id`` MUST be the parent channel's id
        (``<channel platform="discord" id="..." role="parent_channel">``),
        never the current thread's id. Slack: use the id from
        ``<channel platform="slack" id="...">``. Asking about a thread id
        reports that thread's own (almost always empty) scope, which reads
        as a confident answer about the channel and is not one — and if the
        same wrong id was just passed to ``set_agent_default``, this tool
        will agree with it.
        """
        return await _explain_agent_resolution_impl(runtime, await _auth(ctx), channel_id)
