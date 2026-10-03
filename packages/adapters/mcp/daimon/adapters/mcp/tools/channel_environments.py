"""Channel environment tools: which environment a channel's turns run in.

``register_channel_environment_tools(mcp, runtime)`` wires the ``@mcp.tool``
closures; each delegates to a module-private ``_*_impl`` that tests call
without a FastMCP Context. `authorize` decides who may pick
(SET_CHANNEL_ENVIRONMENT): the workspace default needs a server admin; a
channel's also admits that channel's admins, as its default agent does, except
an environment with unrestricted networking in a sealed channel. An operator
token with ``channels:write`` sets channels' environments, never the workspace
default.
"""

from __future__ import annotations

from dataclasses import dataclass

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import resolve_channel
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._isolation import load_caller_hidden_environments
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.agent_pins import POLICY_UNREADABLE_REFUSAL
from daimon.core.authz import Action
from daimon.core.channel_environments import (
    EnvironmentPick,
    authorize_environment_pick,
    build_clear_environment_note,
    build_sealed_network_confirm,
    build_sealed_network_refusal,
    build_set_environment_note,
    save_scope_environment,
)
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


@dataclass(frozen=True)
class ChannelEnvironmentResult:
    """Result returned from set_channel_environment and clear_channel_environment."""

    scope: str
    """'workspace' or 'channel:<channel_id>'"""
    environment_name: str | None
    """The scope's own environment after the change; None once cleared."""
    previous_environment_name: str | None
    """The environment the scope named before, or None if it had none."""
    changed: bool
    note: str
    """What changed and from when, to report back as is."""


def _scope_label(channel_id: str | None) -> str:
    return f"channel:{channel_id}" if channel_id is not None else "workspace"


@dataclass(frozen=True)
class _Target:
    """Where an environment is stored (None: the workspace default), and the
    Discord thread under it the caller named, whose own seal counts."""

    channel_id: str | None
    thread_id: str | None = None


async def _environment_channel(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str | None, *, lenient: bool = False
) -> _Target:
    """The channel an environment is stored under; a thread id names its parent."""
    if channel_id is None:
        return _Target(None)
    target = await resolve_channel(runtime, auth, channel_id, lenient=lenient)
    return _Target(target.channel_id, target.thread_id)


_NEEDS_ADMIN: str = (
    "This change needs a workspace or server admin, or an admin of that channel, "
    "and the caller is neither. Tell them who can make it and give them a sentence "
    "that admin can say, preserving the requested action and channel. Do not retry."
)


def _missing(environment_name: str | None) -> str:
    return (
        f"No environment named '{environment_name}' exists in this workspace. Nothing was "
        "changed. Use list_environments to pick an existing one, or create_environment first."
    )


async def _require_pick_allowed(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    target: _Target,
    environment_name: str | None,
    confirm_open_network: bool,
) -> tuple[EnvironmentPick, frozenset[str]]:
    """The decided pick and the names the caller's isolation hides, which read as missing;
    raise ``ToolError`` unless the caller may leave the scope on it."""
    require_scope(auth, "channels:write")
    if target.channel_id is None and auth.is_operator:
        raise ToolError("An operator token changes only a channel's environment; pass channel_id.")
    async with runtime.session_factory() as session:
        try:
            pick = await authorize_environment_pick(
                session,
                runtime.client,
                tenant_id=auth.tenant_id,
                subject=mcp_subject(auth, is_admin=auth.is_admin),
                channel_id=target.channel_id,
                thread_id=target.thread_id,
                environment_name=environment_name,
                default=runtime.deployment_default,
            )
        except AccessPolicyUnreadable as exc:
            raise ToolError(POLICY_UNREADABLE_REFUSAL) from exc
    if not pick.decision:
        record_authz_denial(Action.SET_CHANNEL_ENVIRONMENT, pick.decision.reason)
    if not pick.decision and pick.decision.reason != "sealed":
        if target.channel_id is None:
            _require_admin(auth)  # the workspace default's own copy
        raise ToolError(_NEEDS_ADMIN)
    # Only a caller who may make the change learns whether a name exists, and never a hidden one.
    hidden = await load_caller_hidden_environments(runtime, auth)
    if environment_name in hidden:
        raise ToolError(_missing(environment_name))
    if not pick.decision:  # sealed
        raise ToolError(
            build_sealed_network_refusal(environment_name=environment_name) + " Do not retry."
        )
    if pick.missing:
        raise ToolError(_missing(environment_name))
    if pick.needs_confirm and not confirm_open_network:
        raise ToolError(
            build_sealed_network_confirm(environment_name=environment_name)
            + " Ask the caller whether to go ahead; only if they confirm, retry with "
            "confirm_open_network=true. Never confirm on their behalf."
        )
    return pick, hidden


async def _set_channel_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    environment_name: str,
    channel_id: str | None,
    confirm_open_network: bool = False,
) -> ChannelEnvironmentResult:
    target = await _environment_channel(runtime, auth, channel_id)
    pick, hidden = await _require_pick_allowed(
        runtime,
        auth,
        target=target,
        environment_name=environment_name.strip(),
        confirm_open_network=confirm_open_network,
    )
    name = pick.environment_name or environment_name.strip()
    async with runtime.session_factory.begin() as session:
        previous = await save_scope_environment(
            session,
            tenant_id=auth.tenant_id,
            channel_id=target.channel_id,
            environment_name=name,
            actor_account_id=auth.account_id,
        )
    return ChannelEnvironmentResult(
        scope=_scope_label(target.channel_id),
        environment_name=name,
        previous_environment_name=None if previous in hidden else previous,
        changed=previous != name,
        note=build_set_environment_note(environment_name=name, channel=target.channel_id),
    )


async def _clear_channel_environment_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str | None,
    confirm_open_network: bool = False,
) -> ChannelEnvironmentResult:
    target = await _environment_channel(runtime, auth, channel_id, lenient=True)
    _, hidden = await _require_pick_allowed(
        runtime,
        auth,
        target=target,
        environment_name=None,
        confirm_open_network=confirm_open_network,
    )
    channel_id = target.channel_id
    async with runtime.session_factory.begin() as session:
        previous = await save_scope_environment(
            session,
            tenant_id=auth.tenant_id,
            channel_id=channel_id,
            environment_name=None,
            actor_account_id=auth.account_id,
        )
    return ChannelEnvironmentResult(
        scope=_scope_label(channel_id),
        environment_name=None,
        previous_environment_name=None if previous in hidden else previous,
        changed=previous is not None,
        note=build_clear_environment_note(channel=channel_id, cleared=previous is not None),
    )


def register_channel_environment_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", "channel-admin", *scope_tags("channels:write")})
    async def set_channel_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        environment_name: str,
        channel_id: str | None = None,
        confirm_open_network: bool = False,
    ) -> ChannelEnvironmentResult:
        """Choose the environment a channel's turns run in, or the workspace default.
        For example, run #growth in the pymc environment. Changes where the agent runs,
        not which agent answers; use ``set_agent_default`` for that.

        Omit ``channel_id`` to set the workspace default, which every channel without
        its own environment uses. The environment must already exist
        (``list_environments``). Conversations pick it up from their next message.
        The workspace default requires Manage Server (admin); an admin of the channel
        may set that channel's environment, except one with unrestricted networking in
        a sealed channel, which needs a server admin and their confirmation: pass
        ``confirm_open_network=true`` only after the caller confirms it.

        Pass the parent channel's id: Discord
        ``<channel platform="discord" id="..." role="parent_channel">``, Slack
        ``<channel platform="slack" id="...">``, Teams ``19:…@thread.tacv2``; a thread
        id resolves to its parent. The caller must be able to see the channel.
        """
        return await _set_channel_environment_impl(
            runtime,
            await _auth(ctx),
            environment_name=environment_name,
            channel_id=channel_id,
            confirm_open_network=confirm_open_network,
        )

    @mcp.tool(tags={"admin", "channel-admin", *scope_tags("channels:write")})
    async def clear_channel_environment(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        channel_id: str | None = None,
        confirm_open_network: bool = False,
    ) -> ChannelEnvironmentResult:
        """Stop a channel picking its own environment, so it uses the workspace default.
        For example, put #growth back on the default environment. A no-op when the
        channel has none of its own.

        Omit ``channel_id`` to clear the workspace default, leaving the deployment
        default. The workspace default requires Manage Server (admin); an admin of the
        channel may clear that channel's environment, unless it is sealed and the
        default has unrestricted networking; then a server admin must confirm it, as
        for ``set_channel_environment``. A thread id resolves to its parent channel.
        """
        return await _clear_channel_environment_impl(
            runtime,
            await _auth(ctx),
            channel_id=channel_id,
            confirm_open_network=confirm_open_network,
        )
