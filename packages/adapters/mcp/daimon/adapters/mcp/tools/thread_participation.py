"""Thread-participation tools: follow a thread (or a channel, or the workspace).

``register_thread_participation_tools(mcp, runtime)`` wires the ``@mcp.tool``
closures for this group; each closure delegates to a module-private ``_*_impl``
function that can be unit-tested without a FastMCP Context.

These are the conversational surface for the participation cascade
(deployment, workspace, channel, thread) that
``daimon.core.thread_participation`` resolves: "follow this thread" is a thing
users say mid-conversation, so it has to be a tool the agent can call, not a
slash command they have to go find.

Deliberately NOT tagged ``admin``: thread scope is a member action, and the
tool would be invisible to the very callers who ask for it. Channel and
workspace scope are admin-only, enforced inside the impl instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord import verify_participation_scope
from daimon.core.stores.thread_participation import (
    ParticipationModes,
    clear_participation_mode,
    get_participation_modes,
    set_participation_mode,
)
from daimon.core.thread_participation import (
    ParticipationMode,
    ParticipationScope,
    ResolvedParticipation,
    build_participation_note,
    resolve_participation,
    scope_label,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_PLATFORM: Final = "discord"

_WRONG_PLATFORM = (
    "Following threads is only available on Discord right now, so there is nothing to turn on here."
)
_DEPLOYMENT_DISABLED = (
    "Participation is disabled for this deployment and cannot be turned on. "
    "I only reply when @mentioned; the operator would have to change that."
)
_DISABLED_NEEDS_A_WIDER_SCOPE = (
    "'disabled' is not a thread-level setting — use mode='off' to stop "
    "following this thread. 'disabled' locks a whole channel or workspace and "
    "is an admin action at those scopes."
)


@dataclass(frozen=True)
class ThreadParticipationResult:
    """Result returned from set_thread_participation."""

    scope: str
    """'thread:<thread_id>', 'channel:<channel_id>' or 'workspace'."""
    mode: str
    """What was stored at that scope: 'on', 'off', 'disabled', or 'inherit'
    when the scope's own setting was removed."""
    effective_mode: str
    """What the whole cascade now resolves to for that scope — not always the
    mode just written, since a wider scope set to 'disabled' still wins."""
    effective_tier: str
    """Which tier supplied effective_mode: 'deployment', 'workspace',
    'channel' or 'thread'. The tier to change to get a different answer."""
    note: str
    """The sentence to report back: what was written and what is actually in
    effect. Supplied by the tool rather than recalled from a prompt, so a
    thread turned on under a disabled channel is reported as still silent."""


@dataclass(frozen=True)
class ThreadParticipationStatus:
    """Result returned from get_thread_participation: every tier's setting."""

    deployment_mode: str
    """The deployment default from the operator's environment."""
    workspace_mode: str | None
    """The workspace tier's own setting, or None if it has none."""
    channel_mode: str | None
    """The channel tier's own setting, or None if it has none (or no channel_id was given)."""
    thread_mode: str | None
    """The thread tier's own setting, or None if it has none (or no thread_id was given)."""
    effective_mode: str
    """What the cascade resolves to for the thread/channel asked about."""
    effective_tier: str
    """Which tier supplied it: 'deployment', 'workspace', 'channel' or 'thread'."""
    explanation: str
    """One sentence naming the winner and the tier it came from, so "why am I
    (not) replying here" is answerable without re-deriving the cascade."""


def _deployment_mode(runtime: McpRuntime) -> ParticipationMode:
    """The deployment tier. Without a Discord settings block there is no bot to follow anything."""
    if runtime.settings.discord is None:
        return ParticipationMode.DISABLED
    return ParticipationMode(runtime.settings.thread_participation.mode)


def _target_scope(
    thread_id: str | None, channel_id: str | None
) -> tuple[ParticipationScope, str | None]:
    """Narrowest id given wins. A thread's channel_id is its parent, not a second target."""
    if thread_id is not None and not thread_id.strip():
        raise ToolError("thread_id must not be empty")
    if channel_id is not None and not channel_id.strip():
        raise ToolError("channel_id must not be empty")
    if thread_id is not None:
        return ParticipationScope.THREAD, thread_id
    if channel_id is not None:
        return ParticipationScope.CHANNEL, channel_id
    return ParticipationScope.WORKSPACE, None


def _resolve_above(
    scope: ParticipationScope, deployment: ParticipationMode, modes: ParticipationModes
) -> ResolvedParticipation:
    """Resolve only the tiers wider than `scope` — what a write here has to live under."""
    return resolve_participation(
        deployment=deployment,
        workspace=modes.workspace if scope is not ParticipationScope.WORKSPACE else None,
        channel=modes.channel if scope is ParticipationScope.THREAD else None,
        thread=None,
    )


def _scope_result_label(scope: ParticipationScope, scope_id: str | None) -> str:
    return "workspace" if scope is ParticipationScope.WORKSPACE else f"{scope.value}:{scope_id}"


async def _set_thread_participation_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    mode: Literal["on", "off", "disabled", "inherit"],
    thread_id: str | None,
    channel_id: str | None,
) -> ThreadParticipationResult:
    """Validate every refusal BEFORE any write: a refused call must leave no row behind."""
    if auth.platform != _PLATFORM:
        raise ToolError(_WRONG_PLATFORM)

    deployment = _deployment_mode(runtime)
    if deployment is ParticipationMode.DISABLED:
        raise ToolError(_DEPLOYMENT_DISABLED)

    scope, scope_id = _target_scope(thread_id, channel_id)
    if scope is not ParticipationScope.THREAD:
        _require_admin(auth)
    if scope is ParticipationScope.THREAD and mode == "disabled":
        raise ToolError(_DISABLED_NEEDS_A_WIDER_SCOPE)
    parent_id = await verify_participation_scope(runtime, auth, scope, scope_id)
    if parent_id is not None:
        channel_id = parent_id

    requested = None if mode == "inherit" else ParticipationMode(mode)
    tenant_id: uuid.UUID = auth.tenant_id

    async with runtime.session_factory.begin() as session:
        before = await get_participation_modes(
            session,
            tenant_id=tenant_id,
            platform=auth.platform,
            channel_id=channel_id,
            thread_id=thread_id,
        )
        above = _resolve_above(scope, deployment, before)
        if requested is ParticipationMode.ON and above.mode is ParticipationMode.DISABLED:
            raise ToolError(
                f"Participation is disabled at the {above.tier} level, so "
                f"{scope_label(scope, scope_id)} cannot be turned on. An admin would "
                f"have to change the {above.tier} setting first."
            )

        if requested is None:
            await clear_participation_mode(
                session,
                tenant_id=tenant_id,
                platform=auth.platform,
                scope=scope,
                scope_id=scope_id,
            )
        else:
            await set_participation_mode(
                session,
                tenant_id=tenant_id,
                platform=auth.platform,
                scope=scope,
                scope_id=scope_id,
                mode=requested,
            )

        after = await get_participation_modes(
            session,
            tenant_id=tenant_id,
            platform=auth.platform,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    effective = resolve_participation(
        deployment=deployment,
        workspace=after.workspace,
        channel=after.channel,
        thread=after.thread,
    )
    return ThreadParticipationResult(
        scope=_scope_result_label(scope, scope_id),
        mode="inherit" if requested is None else requested.value,
        effective_mode=effective.mode.value,
        effective_tier=effective.tier,
        note=build_participation_note(
            scope=scope, scope_id=scope_id, requested=requested, effective=effective
        ),
    )


def _explain(effective: ResolvedParticipation, *, thread_id: str | None) -> str:
    where = f"thread {thread_id}" if thread_id is not None else "here"
    if effective.mode is ParticipationMode.ON:
        return (
            f"I follow {where}: the {effective.tier} tier is set to on, so I may reply "
            "to messages that call for it without being addressed first."
        )
    if effective.mode is ParticipationMode.DISABLED:
        return (
            f"I only reply in {where} when @mentioned: the {effective.tier} tier is "
            "disabled, and no narrower scope can turn it back on."
        )
    return (
        f"I only reply in {where} when @mentioned: the {effective.tier} tier is the "
        "narrowest one with a setting, and it is off."
    )


async def _get_thread_participation_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    thread_id: str | None,
    channel_id: str | None,
) -> ThreadParticipationStatus:
    if auth.platform != _PLATFORM:
        raise ToolError(_WRONG_PLATFORM)
    deployment = _deployment_mode(runtime)
    scope, scope_id = _target_scope(thread_id, channel_id)
    parent_id = await verify_participation_scope(runtime, auth, scope, scope_id)
    if parent_id is not None:
        channel_id = parent_id
    async with runtime.session_factory() as session:
        modes = await get_participation_modes(
            session,
            tenant_id=auth.tenant_id,
            platform=auth.platform,
            channel_id=channel_id,
            thread_id=thread_id,
        )

    effective = resolve_participation(
        deployment=deployment,
        workspace=modes.workspace,
        channel=modes.channel,
        thread=modes.thread,
    )
    return ThreadParticipationStatus(
        deployment_mode=deployment.value,
        workspace_mode=modes.workspace.value if modes.workspace is not None else None,
        channel_mode=modes.channel.value if modes.channel is not None else None,
        thread_mode=modes.thread.value if modes.thread is not None else None,
        effective_mode=effective.mode.value,
        effective_tier=effective.tier,
        explanation=_explain(effective, thread_id=thread_id),
    )


def register_thread_participation_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def set_thread_participation(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        mode: Literal["on", "off", "disabled", "inherit"],
        thread_id: str | None = None,
        channel_id: str | None = None,
    ) -> ThreadParticipationResult:
        """Start or stop replying in a thread without being addressed each time.

        Following is OFF everywhere by default, and that is the right default —
        prefer to leave it off. Turn a thread on only when the user asks you to
        follow it, stay in it, or keep replying in it, or when a live
        back-and-forth clearly needs your replies to continue. Turn it off with
        ``mode="off"`` the moment someone asks you to stop following, to be
        quiet, or to leave the thread alone. When it is on you still choose
        silence far more often than speech: nothing here obliges you to reply.

        Which ids to pass (Discord only; the tool refuses elsewhere):
        - a thread: ``thread_id`` from ``<thread role="current_thread">``. The
          thread must exist in this server and be visible to the caller; its
          parent channel is looked up, so ``channel_id`` is optional here.
        - a whole channel: ``channel_id`` only. **Admin action** (Manage Server).
        - the whole workspace: neither id. **Admin action** (Manage Server).

        Modes: ``on`` follow, ``off`` do not follow, ``disabled`` lock a channel
        or workspace off so no narrower scope can turn it back on (not valid for
        a single thread — use ``off``), ``inherit`` remove this scope's own
        setting so it follows whatever the wider scope says.

        The narrowest scope with a setting wins, except that a ``disabled``
        scope wins over everything below it. Read the returned ``note`` back to
        the user: a thread turned on under a disabled channel is still silent,
        and the note says so rather than promising something that will not happen.
        """
        return await _set_thread_participation_impl(
            runtime, await _auth(ctx), mode, thread_id, channel_id
        )

    @mcp.tool
    async def get_thread_participation(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        thread_id: str | None = None,
        channel_id: str | None = None,
    ) -> ThreadParticipationStatus:
        """Report whether you follow a thread or channel, and which tier decided it.

        Use it when someone asks whether you are following this thread, why you
        did or did not reply unprompted, or before changing a setting — the tier
        that currently wins is the tier worth changing.

        Pass ``thread_id`` from ``<thread role="current_thread">`` for a
        thread (its parent channel is looked up), ``channel_id`` from
        ``<channel role="parent_channel">`` alone for a channel, neither for
        the workspace. Not gated: any member can ask, about threads and
        channels they can see.

        Reports every tier (deployment, workspace, channel, thread) plus the
        winner, so "why" is answerable without guessing. Following is off by
        default, so an all-unset cascade answering ``off`` is the normal state,
        not a misconfiguration.
        """
        return await _get_thread_participation_impl(
            runtime, await _auth(ctx), thread_id, channel_id
        )
