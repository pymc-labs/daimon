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
from daimon.adapters.mcp.tools._isolation import (
    CallerIsolation,
    load_caller_isolation,
    load_isolation,
    refuse,
    require_bindable,
)
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.adapters.mcp.tools.reachability import (
    require_bindable_as_channel_default,
    require_channel_admin,
)
from daimon.adapters.mcp.tools.setup_target import resolve_setup_agent
from daimon.core.channel_isolation import clear_refusal
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_MANAGED
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
from daimon.core.stores.domain import CHAT_PLATFORMS, ThreadAgentBindingRow
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields, unset_fields
from daimon.core.stores.thread_agent_bindings import get_binding, list_active_bindings
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


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


async def _require_scope_admin(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str | None
) -> None:
    """The workspace default needs a server admin; a channel's also admits its channel admins.

    Operator tokens change only channel defaults, never the workspace one."""
    if channel_id is None:
        if auth.is_operator:
            raise ToolError("An operator token changes only a channel's default; pass channel_id.")
        _require_admin(auth)
    else:
        await require_channel_admin(runtime, auth, channel_id=channel_id)


async def _set_agent_default_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    agent_name: str,
    channel_id: str | None,
    expected_ma_agent_id: str | None = None,
) -> SetDefaultResult:
    require_scope(auth, "channels:write")
    await _require_scope_admin(runtime, auth, channel_id)
    agent = None
    if expected_ma_agent_id is not None or auth.platform in CHAT_PLATFORMS:
        agent = await resolve_setup_agent(
            runtime, auth, name=agent_name, expected_ma_agent_id=expected_ma_agent_id
        )
    if channel_id is not None:
        await require_bindable_as_channel_default(
            runtime,
            auth,
            channel_id=channel_id,
            agent_name=agent_name,
            agent=agent,
            is_daimon_managed=agent is not None
            and agent.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
        )
    else:
        # The workspace default answers everywhere, so no pinned agent can be it.
        policy = await load_isolation(runtime, auth.tenant_id)
        if policy.agent_channel_pins:
            if agent is None:
                agent = await find_agent_by_daimon_tag(
                    runtime.client, tenant_id=auth.tenant_id, name=agent_name
                )
            require_bindable(policy, agent_name, agent=agent, channel_id=None)

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
    require_scope(auth, "channels:write")
    await _require_scope_admin(runtime, auth, channel_id)
    if channel_id is not None:
        policy = await load_isolation(runtime, auth.tenant_id)
        refuse(clear_refusal(policy, channel_id=channel_id), agent_name=None)

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
    responder_ma_agent_id: str | None
    configuration_target_ma_agent_id: str | None
    configuration_target_name: str | None
    recent_setup_conversations: tuple[ThreadAgentBindingRow, ...]
    explanation: str
    """One sentence naming the winner and the tier it came from, so the caller
    can answer 'why that one' without re-deriving the cascade."""


def _thread_explanation(binding: ThreadAgentBindingRow) -> str:
    """Say why this thread has its own responder, in the words that thread's kind earns.

    A setup conversation and a handed-over task both outrank channel routing,
    but for opposite reasons: one is a place to configure another agent, the
    other is that other agent now doing the work. Reporting both as "setup
    thread" would tell somebody asking "why is this agent answering?" a
    confident falsehood.
    """
    if binding.kind == "handoff":
        return (
            f"{binding.responder_name} answers in this thread because this task was handed "
            "to it; the channel's own default is unchanged."
        )
    return (
        f"{binding.responder_name} answers in this setup thread; configuring "
        f"{binding.configuration_target_name or 'an agent not yet selected'}."
    )


_ACROSS_LINE_MSG = (
    "{place} is across an isolated channel's line from this conversation, so its "
    "routing can't be shown here."
)


_NO_OWN_AGENT_NOTE = (
    "None of this isolated channel's own agents answers here, so a mention is refused "
    "until an admin sets the channel's agent."
)


def _visible(caller: CallerIsolation, agent_name: str | None) -> str | None:
    return agent_name if agent_name is not None and caller.sees(agent_name) else None


async def _explain_agent_resolution_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    channel_id: str,
    thread_id: str | None = None,
) -> AgentResolutionExplanation:
    """Resolve the cascade for one channel and report every tier's contribution.

    Deliberately NOT admin-gated. This is a read of routing that any member can
    already infer from a turn's footer, and the people most often confused about
    which agent answers are ordinary members. Gating it would leave the question
    unanswerable by exactly the callers who ask it.

    Isolation still applies: a channel across an isolated channel's line from
    the caller is refused, and agents the caller can't see are left out.
    """
    tenant_id: uuid.UUID = auth.tenant_id
    caller = await load_caller_isolation(runtime, auth)
    if caller.isolated_place(channel_id) != caller.inside_channel_id:
        raise ToolError(_ACROSS_LINE_MSG.format(place=f"channel '{channel_id}'"))

    async with runtime.session_factory() as session:
        channel_row = await get_scope(
            session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id)
        )
        tenant_row = await get_scope(session, scope=TenantScopeRef(tenant_id=tenant_id))
        binding = None
        recent: list[ThreadAgentBindingRow] = []
        if auth.platform in CHAT_PLATFORMS:
            if thread_id is not None:
                binding = await get_binding(
                    session,
                    tenant_id=tenant_id,
                    platform=auth.platform,
                    parent_channel_id=channel_id,
                    thread_id=thread_id,
                )
                if binding is not None and binding.deleted:
                    target = (
                        f"{binding.configuration_target_name} "
                        f"({binding.configuration_target_ma_agent_id})"
                        if binding.configuration_target_ma_agent_id is not None
                        else "not selected"
                    )
                    kind_label = (
                        "Setup conversation" if binding.kind == "setup" else "Handoff conversation"
                    )
                    next_step = (
                        "Open a new setup conversation to continue."
                        if binding.kind == "setup"
                        else "Start the task again in a new thread to continue."
                    )
                    raise ToolError(
                        f"{kind_label} '{thread_id}' in '{channel_id}' was deleted. "
                        f"Its recorded responder is {binding.responder_name} "
                        f"({binding.responder_ma_agent_id}); configuration target: {target}. "
                        f"{next_step}"
                    )
            if binding is not None and not caller.sees(binding.responder_name):
                raise ToolError(_ACROSS_LINE_MSG.format(place=f"thread '{thread_id}'"))
            recent = [
                row
                for row in await list_active_bindings(
                    session,
                    tenant_id=tenant_id,
                    platform=auth.platform,
                    parent_channel_id=channel_id,
                    limit=10,
                )
                if caller.sees(row.responder_name)
            ]

    channel_cfg = channel_row if isinstance(channel_row, ChannelConfigRow) else None
    tenant_cfg = tenant_row if isinstance(tenant_row, TenantConfigRow) else None
    resolved = merge(channel=channel_cfg, tenant=tenant_cfg, default=runtime.deployment_default)
    hidden_winner = binding is None and _visible(caller, resolved.agent_name) is None

    return AgentResolutionExplanation(
        channel_id=channel_id,
        effective_agent_name=binding.responder_name
        if binding
        else _visible(caller, resolved.agent_name),
        winning_tier="thread" if binding else resolved.agent_name_tier,
        channel_default=channel_cfg.agent_name if channel_cfg is not None else None,
        tenant_default=_visible(caller, tenant_cfg.agent_name if tenant_cfg is not None else None),
        deployment_default=_visible(caller, runtime.deployment_default.agent_name),
        effective_environment_name=resolved.environment_name,
        environment_winning_tier=resolved.environment_name_tier,
        responder_ma_agent_id=binding.responder_ma_agent_id if binding else None,
        configuration_target_ma_agent_id=binding.configuration_target_ma_agent_id
        if binding
        else None,
        configuration_target_name=binding.configuration_target_name if binding else None,
        recent_setup_conversations=tuple(recent),
        explanation=_thread_explanation(binding)
        if binding
        else _NO_OWN_AGENT_NOTE
        if hidden_winner and resolved.agent_name is not None
        else build_resolution_note(
            agent_name=resolved.agent_name,
            tier=resolved.agent_name_tier,
            channel_id=channel_id,
        ),
    )


def register_propagation_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", "channel-admin", *scope_tags("channels:write")})
    async def set_agent_default(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        channel_id: str | None = None,
        expected_ma_agent_id: str | None = None,
    ) -> SetDefaultResult:
        """Make an agent answer in a channel or become the whole server/workspace default.
        For example, make churn-explorer answer in #growth. Changes the agent that answers;
        use ``clear_agent_default`` to stop that routing.

        When ``channel_id`` is provided the default is scoped to that channel;
        omit it to set the workspace-wide default.  Any existing default at the
        chosen scope is replaced (last-write-wins; an audit stamp is recorded by
        core).  Requires Manage Server (admin); an admin of the channel may set
        that channel's default to a built-in agent, the workspace default, an
        agent that answers nowhere yet, or one that answers only in channels
        they administer.

        Discord: ``channel_id`` MUST be the parent channel's id — the one
        your context gives as
        ``<channel platform="discord" id="..." role="parent_channel">``.
        Never pass the current thread's id here. Slack: use the id from
        ``<channel platform="slack" id="...">`` — Slack's context always
        names the parent channel. Teams: use ``parent_channel_id`` from
        turn_controls. A channel default is resolved from the parent;
        setup threads keep their bound responder. A default
        written against a thread id is a scope nothing ever reads: the write
        succeeds, this tool reports success, and the channel keeps answering
        with the old agent.
        """
        return await _set_agent_default_impl(
            runtime, await _auth(ctx), agent_name, channel_id, expected_ma_agent_id
        )

    @mcp.tool(tags={"admin", "channel-admin", *scope_tags("channels:write")})
    async def clear_agent_default(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str | None = None,
    ) -> ClearDefaultResult:
        """Stop an agent answering in a channel by clearing its default routing.
        For example, stop churn-explorer answering in #growth. Use
        ``set_agent_default`` to choose a replacement instead.

        When ``channel_id`` is provided only that channel's default is cleared;
        omit it to clear the workspace-wide default.  If the scope had no
        default the call is a no-op (idempotent).  Requires Manage Server (admin);
        an admin of the channel may clear that channel's default.

        Discord: ``channel_id`` MUST be the parent channel's id
        (``<channel platform="discord" id="..." role="parent_channel">``),
        never the current thread's id. Slack: use the id from
        ``<channel platform="slack" id="...">``. Teams: use
        ``parent_channel_id`` from turn_controls. Clearing a thread id is a
        silent no-op that leaves the channel's real default in place.
        """
        return await _clear_agent_default_impl(runtime, await _auth(ctx), channel_id)

    @mcp.tool
    async def explain_agent_resolution(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str,
        thread_id: str | None = None,
    ) -> AgentResolutionExplanation:
        """Who answers in this channel, for example #growth? Report who answers and
        which routing tier decided it.

        Supply thread_id to include its setup binding. Thread responder wins, else
        the channel's own default, else the workspace default, else the deployment
        default. This reports the winner
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
        ``<channel platform="slack" id="...">``. Teams: ``parent_channel_id``
        from turn_controls, with its ``thread_id``. Asking about a thread id
        reports that thread's own (almost always empty) scope, which reads
        as a confident answer about the channel and is not one — and if the
        same wrong id was just passed to ``set_agent_default``, this tool
        will agree with it.
        """
        return await _explain_agent_resolution_impl(
            runtime, await _auth(ctx), channel_id, thread_id
        )
