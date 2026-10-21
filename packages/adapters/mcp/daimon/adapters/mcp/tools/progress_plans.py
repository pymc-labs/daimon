"""Progress-plan tools: the switch (`set_progress_plans`, `get_progress_plans`) and the plan.

``register_progress_plan_tools(mcp, runtime)`` wires the ``@mcp.tool`` closures;
each delegates to a module-private ``_*_impl`` function testable without a
FastMCP Context.

The switch is the same scope cascade as thread participation
(``daimon.core.scope_cascade``) with the same permission rules: thread scope is a
member action, channel and workspace scope need an admin, and ``disabled`` is
final below the scope that sets it. Unlike participation it covers Slack too,
whose card draws the plan as Discord's and Teams' do.

``update_plan`` is registered on every chat session, whether or not plans are
on, so the tool list stays the same from turn to turn and a prompt cache holds.
The turn says when plans are on (``daimon.core.turn.progress_plans``); when they
are off the card ignores the call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_within_hold
from daimon.adapters.mcp.tools._channel_target import resolve_channel
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord import verify_participation_scope
from daimon.adapters.mcp.tools.teams._participation import verify_teams_participation_scope
from daimon.core.scope_cascade import (
    CascadeMode,
    CascadeScope,
    resolve_above,
    resolve_modes,
    scope_label,
    tier_word,
)
from daimon.core.stores.progress_plans import (
    clear_progress_plan_mode,
    get_progress_plan_modes,
    set_progress_plan_mode,
)
from daimon.core.turn.progress_plans import (
    build_progress_plans_explanation,
    build_progress_plans_note,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

_PLATFORMS: Final = frozenset({"discord", "slack", "teams"})
_MAX_STEPS: Final = 12

_WRONG_PLATFORM = "Progress plans show on the chat card, so there is nothing to turn on here."
_DEPLOYMENT_DISABLED = (
    "Progress plans are turned off for all of Daimon and cannot be turned on; "
    "the operator would have to change that."
)
_DISABLED_NEEDS_A_WIDER_SCOPE = (
    "'disabled' is not a thread-level setting — use mode='off' to stop showing "
    "a plan in this thread. 'disabled' locks a whole channel or everything here and "
    "is an admin action there."
)
_PLAN_UPDATED = "Plan updated. This is not a reply: carry on with the task."


@dataclass(frozen=True)
class ProgressPlansResult:
    """Result returned from set_progress_plans."""

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
    """The sentence to report back: what was written and what is in effect."""


@dataclass(frozen=True)
class ProgressPlansStatus:
    """Result returned from get_progress_plans: every tier's setting."""

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
    """One sentence naming the winner and the tier it came from."""


class PlanStep(BaseModel):
    """One step of the plan, as the person reads it on the card."""

    model_config = ConfigDict(extra="forbid")

    step: str = Field(min_length=1, max_length=120, description="A few plain words.")
    status: Literal["pending", "in_progress", "completed"]


@dataclass(frozen=True)
class _Target:
    scope: CascadeScope
    scope_id: str | None
    """The id the rows are keyed by: a Slack thread is ``<channel>:<thread ts>``."""
    channel_id: str | None
    """The channel the cascade's channel tier reads, once known."""
    thread_id: str | None


def _target(platform: str, thread_id: str | None, channel_id: str | None) -> _Target:
    """Narrowest id given wins. A thread's channel_id is its parent, not a second target."""
    if thread_id is not None and not thread_id.strip():
        raise ToolError("thread_id must not be empty")
    if channel_id is not None and not channel_id.strip():
        raise ToolError("channel_id must not be empty")
    if thread_id is not None:
        if platform == "slack":
            channel, sep, ts = thread_id.partition(":")
            if not sep:
                if channel_id is None:
                    raise ToolError("pass the thread's channel_id with a Slack thread_id")
                channel, ts = channel_id, thread_id
            thread_id = f"{channel}:{ts}"
            channel_id = channel
        return _Target(CascadeScope.THREAD, thread_id, channel_id, thread_id)
    if channel_id is not None:
        return _Target(CascadeScope.CHANNEL, channel_id, channel_id, None)
    return _Target(CascadeScope.WORKSPACE, None, None, None)


async def _verify(runtime: McpRuntime, auth: AuthIdentity, target: _Target) -> _Target:
    """The caller can see what they named; thread scope reads the thread's real parent."""
    if target.scope is CascadeScope.WORKSPACE or target.scope_id is None:
        return target
    if auth.platform == "slack":
        channel = target.channel_id or target.scope_id
        await resolve_channel(runtime, auth, channel)
        return target
    if auth.platform == "teams":
        parent = await verify_teams_participation_scope(
            runtime, auth, target.scope, target.scope_id
        )
    else:
        parent = await verify_participation_scope(runtime, auth, target.scope, target.scope_id)
    if parent is None:
        return target
    return _Target(target.scope, target.scope_id, parent, target.thread_id)


def _result_label(target: _Target) -> str:
    if target.scope is CascadeScope.WORKSPACE:
        return "workspace"
    return f"{target.scope.value}:{target.scope_id}"


def _require_platform(auth: AuthIdentity) -> str:
    if auth.platform not in _PLATFORMS:
        raise ToolError(_WRONG_PLATFORM)
    return auth.platform


async def _set_progress_plans_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    mode: Literal["on", "off", "disabled", "inherit"],
    thread_id: str | None,
    channel_id: str | None,
) -> ProgressPlansResult:
    """Validate every refusal BEFORE any write: a refused call must leave no row behind."""
    platform = _require_platform(auth)
    deployment = runtime.settings.progress_plans.mode
    if deployment is CascadeMode.DISABLED:
        raise ToolError(_DEPLOYMENT_DISABLED)
    target = _target(platform, thread_id, channel_id)
    if target.scope is not CascadeScope.THREAD:
        _require_admin(auth)
    if target.scope is CascadeScope.THREAD and mode == "disabled":
        raise ToolError(_DISABLED_NEEDS_A_WIDER_SCOPE)
    target = await _verify(runtime, auth, target)
    requested = None if mode == "inherit" else CascadeMode(mode)

    async with runtime.session_factory.begin() as session:
        before = await get_progress_plan_modes(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=target.channel_id,
            thread_id=target.thread_id,
        )
        above = resolve_above(target.scope, deployment, before)
        if requested is CascadeMode.ON and above.mode is CascadeMode.DISABLED:
            tier = tier_word(above.tier, platform)
            raise ToolError(
                f"Progress plans are disabled at the {tier} level, so "
                f"{scope_label(target.scope, target.scope_id, platform)} cannot be turned "
                f"on. An admin would have to change the {tier} setting first."
            )
        if requested is None:
            await clear_progress_plan_mode(
                session,
                tenant_id=auth.tenant_id,
                platform=platform,
                scope=target.scope,
                scope_id=target.scope_id,
            )
        else:
            await set_progress_plan_mode(
                session,
                tenant_id=auth.tenant_id,
                platform=platform,
                scope=target.scope,
                scope_id=target.scope_id,
                mode=requested,
            )
        after = await get_progress_plan_modes(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=target.channel_id,
            thread_id=target.thread_id,
        )

    effective = resolve_modes(deployment, after)
    return ProgressPlansResult(
        scope=_result_label(target),
        mode="inherit" if requested is None else requested.value,
        effective_mode=effective.mode.value,
        effective_tier=effective.tier,
        note=build_progress_plans_note(
            scope=target.scope,
            scope_id=target.scope_id,
            requested=requested,
            effective=effective,
            platform=platform,
        ),
    )


async def _get_progress_plans_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    thread_id: str | None,
    channel_id: str | None,
) -> ProgressPlansStatus:
    platform = _require_platform(auth)
    deployment = runtime.settings.progress_plans.mode
    target = await _verify(runtime, auth, _target(platform, thread_id, channel_id))
    if target.scope_id is not None:
        # The workspace's mode reveals no channel; a held call reads only its own.
        await require_within_hold(
            runtime, auth, target.thread_id or target.scope_id, target.channel_id
        )
    async with runtime.session_factory() as session:
        modes = await get_progress_plan_modes(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=target.channel_id,
            thread_id=target.thread_id,
        )
    effective = resolve_modes(deployment, modes)
    return ProgressPlansStatus(
        deployment_mode=deployment.value,
        workspace_mode=modes.workspace.value if modes.workspace is not None else None,
        channel_mode=modes.channel.value if modes.channel is not None else None,
        thread_mode=modes.thread.value if modes.thread is not None else None,
        effective_mode=effective.mode.value,
        effective_tier=effective.tier,
        explanation=build_progress_plans_explanation(
            effective, thread_id=target.thread_id, platform=platform
        ),
    )


def _update_plan_impl(steps: list[PlanStep]) -> str:
    """The card reads the plan from the call itself; the tool only acknowledges it."""
    del steps
    return _PLAN_UPDATED


def register_progress_plan_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def set_progress_plans(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        mode: Literal["on", "off", "disabled", "inherit"],
        thread_id: str | None = None,
        channel_id: str | None = None,
    ) -> ProgressPlansResult:
        """Turn progress plans on or off where you are asked to.

        A progress plan is a short checklist of a longer task's steps on the
        progress card. Call this when someone asks, for example "turn on
        progress plans here".

        Scope: ``thread_id`` and ``channel_id`` (this turn's thread and channel
        ids) for a thread; ``channel_id`` alone for a channel; neither for the
        workspace. Channel and workspace scope need an admin.

        Modes: ``on``, ``off``, ``disabled`` (lock a channel or workspace so
        nothing below can turn it on; not valid for a thread), ``inherit``
        (drop this scope's setting). Read the returned ``note`` back to the user.
        """
        return await _set_progress_plans_impl(
            runtime, await _auth(ctx), mode, thread_id, channel_id
        )

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def get_progress_plans(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        thread_id: str | None = None,
        channel_id: str | None = None,
    ) -> ProgressPlansStatus:
        """Report whether progress plans are on for a thread or channel, and which tier decided it.

        Scope: ``thread_id`` and ``channel_id`` for a thread; ``channel_id``
        alone for a channel; neither for the workspace. Reading
        needs no admin. Read the returned ``explanation`` back to the user.
        """
        return await _get_progress_plans_impl(runtime, await _auth(ctx), thread_id, channel_id)

    @mcp.tool(tags={"discord", "slack", "teams"})  # pyright: ignore[reportArgumentType]
    async def update_plan(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        steps: Annotated[list[PlanStep], Field(min_length=1, max_length=_MAX_STEPS)],
    ) -> str:
        """Show the task's plan as a checklist on the progress card.

        Use it only when this turn's progress_plans block says progress plans
        are on; otherwise nothing is shown. Send the whole list each time, in
        order, with at most one step in_progress. Updating the plan is not a
        reply: carry on with the task afterwards.
        """
        del ctx
        return _update_plan_impl(steps)
