"""Conversational task handoff and fresh start.

Two tools, both called from inside the mention turn that carries the request,
so changing who answers costs no extra billed turn. Both are authorized the
same way as `set_setup_target`: a trusted `origin_context_id` bound to this
caller, tenant and platform, and a concrete MA agent id — a recreated namesake
has a different id and cannot receive a handoff.

Neither tool writes channel or workspace routing. A handoff binds one thread;
who answers everywhere else is untouched. But the thread then runs as the
destination, with its repo, keys, connectors and memory, so a member may only
hand a thread to the agent the channel itself answers with. Bringing in any
other agent is an admin's call, and an agent an operator pinned to other
channels can't be brought in at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.continuity.continuation import (
    MAX_REQUESTED_WORK,
    ContinuationRequest,
    sanitize_requested_work,
)
from daimon.core.continuity.handoff import (
    HandoffRefused,
    HandoffRefusedInSetupThread,
    decide_handoff,
)
from daimon.core.continuity.messages import render_fresh_start, render_handoff_acknowledged
from daimon.core.continuity.tool_messages import (
    render_tool_refusal_setup_thread,
    render_tool_refusal_unreachable,
    render_tool_unsaved_work_question,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.scope import ScopeContext
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import ChatPlatform
from daimon.core.stores.scoped_config_read import is_agent_reachable_in_tenant, resolve
from daimon.core.stores.task_continuations import record_continuation
from daimon.core.stores.thread_agent_bindings import get_binding, upsert_responder_binding
from daimon.core.stores.thread_session_lineage import request_fresh_start
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    set_pending_unsaved_work,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

#: What the model must do with the returned copy. Both tools return final
#: person-facing text, so the model's job is to relay it, not to rewrite it.
_REPLY_VERBATIM = "Reply with `confirmation` verbatim and nothing else."


def _channel_mention(platform: ChatPlatform, channel_id: str) -> str:
    """Render a parent channel id as this platform's channel-link mention.

    Discord and Slack both render `<#id>` as a clickable channel link. Teams has
    no text syntax for one, so it names the place: a 1:1 chat id starts `a:`.
    """
    if platform == "teams":
        return "this chat" if channel_id.startswith("a:") else "this channel"
    return f"<#{channel_id}>"


@dataclass(frozen=True)
class TaskHandoffResult:
    """Result returned from hand_off_task."""

    destination_name: str
    """The agent that answers in this thread from the next message."""
    destination_ma_agent_id: str
    """Its concrete MA id — the identity the handoff was bound to."""
    previous_responder_name: str
    """Who was answering here until now."""
    continuation_recorded: bool
    """True when work was queued for the destination's first turn."""
    confirmation: str
    """Final person-facing copy. Relay it verbatim."""
    instruction: str
    """What to do with `confirmation`."""


@dataclass(frozen=True)
class TaskFreshStartResult:
    """Result returned from start_fresh_task."""

    confirmation: str
    """Final person-facing copy. Relay it verbatim."""
    instruction: str
    """What to do with `confirmation`."""


async def _hand_off_task_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    agent_id: str,
    continuation: str | None = None,
    unsaved_work: Literal["copy", "leave"] | None = None,
) -> TaskHandoffResult:
    """Bind this thread's responder to another agent, optionally queueing work.

    Every check runs before anything is written, so a refused handoff leaves
    the conversation exactly as it was — including the uncommitted-work
    question, which is a refusal the caller answers and retries.
    """
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    # `require_turn_origin` already refused any non-chat platform.
    platform = cast(ChatPlatform, origin.platform)

    destination = next(
        (
            agent
            for agent in await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
            if agent.id == agent_id
        ),
        None,
    )
    if destination is None:
        raise ToolError(
            "That agent is not in this workspace, or it was recreated and has a new id. "
            "Look it up with list_agents and pass the id it reports now. Nothing was changed."
        )
    destination_name = destination.metadata.get(MA_METADATA_KEY_NAME)
    if not destination_name:
        raise ToolError(
            "That agent has no configuration name, so nobody can reach it by name. "
            "Choose a named agent in this workspace. Nothing was changed."
        )
    continuation = sanitize_requested_work(continuation, echoes=(destination_name,))
    if continuation is not None and auth.platform_user_id is None:
        raise ToolError(
            "Continuing work needs the requester's platform identity, which this "
            "connection does not carry. Ask from the conversation itself."
        )

    async with runtime.session_factory() as session:
        reachable = await is_agent_reachable_in_tenant(
            session,
            tenant_id=auth.tenant_id,
            agent_name=destination_name,
            default=runtime.deployment_default,
        )
        binding = await get_binding(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
        )
        policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        # Who the channel answers with, ignoring any thread binding: a member
        # may hand a thread back to that agent, and to no other.
        channel_config = await resolve(
            session,
            context=ScopeContext(tenant_id=auth.tenant_id, channel_id=origin.parent_channel_id),
            default=runtime.deployment_default,
        )
        live_session = await get_live_thread_session(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            thread_id=origin.thread_id,
            account_id=auth.account_id,
        )

    decision = decide_handoff(
        destination_ma_agent_id=destination.id,
        destination_name=destination_name,
        destination_reachable=reachable,
        existing_binding_kind=binding.kind if binding is not None else None,
        origin_responder_ma_agent_id=origin.responder_ma_agent_id,
        # A DM origin runs in no channel, so it is outside every pin.
        destination_pinned_elsewhere=not authorize(
            policy,
            subject=Subject(),
            action=Action.RUN_AGENT,
            surface=Surface.HANDOFF,
            agent=AgentRef.of(
                destination_name, *agent_pin_names(destination.name, destination.metadata)
            ),
            place=Place.from_origin(
                parent_channel_id=origin.parent_channel_id, thread_id=origin.thread_id
            ),
        ),
        destination_answers_channel=channel_config.agent_name == destination_name,
        caller_is_admin=auth.is_admin,
    )
    if isinstance(decision, HandoffRefused):
        raise ToolError(
            _refusal_text(decision, channel=_channel_mention(platform, origin.parent_channel_id))
        )

    # We cannot know whether the checkout is dirty without spending a turn in
    # the session, so the rule is: ask only for a meaningful choice involving loss — when a
    # repo is bound at all, ask once, and let the answer ride the retry. A
    # thread with no repo bound has no uncommitted work to lose and is never
    # interrupted with the question.
    repo = (
        live_session.effective_config.repo_url
        if live_session is not None and live_session.effective_config is not None
        else None
    )
    if repo is not None and unsaved_work is None:
        raise ToolError(render_tool_unsaved_work_question(repo))

    queued_work = None if continuation is None else continuation[:MAX_REQUESTED_WORK]
    request = (
        None
        if queued_work is None or auth.platform_user_id is None
        else ContinuationRequest(
            tenant_id=auth.tenant_id,
            platform=platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
            requester_account_id=auth.account_id,
            requester_external_user_id=auth.platform_user_id,
            target_ma_agent_id=destination.id,
            target_name=destination_name,
            requested_work=queued_work,
            reason="task_handoff",
            idempotency_key=uuid.uuid4(),
        )
    )

    now = datetime.now(UTC)
    try:
        # One transaction: the confirmation promises both the switch and the
        # continuation, so a thread must never end up switched with the work
        # it was told would be picked up missing.
        async with runtime.session_factory.begin() as session:
            if unsaved_work is not None and live_session is not None:
                # The answer outlives this turn: the replacement it governs
                # happens at this caller's NEXT message, when the destination
                # binds. Written in the same transaction as the binding so a
                # thread can never end up switched with the answer lost.
                await set_pending_unsaved_work(session, id=live_session.id, choice=unsaved_work)
            await upsert_responder_binding(
                session,
                tenant_id=auth.tenant_id,
                platform=platform,
                parent_channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                responder_ma_agent_id=destination.id,
                responder_name=destination_name,
                created_by_account_id=auth.account_id,
                now=now,
            )
            if request is not None:
                await record_continuation(
                    session,
                    tenant_id=request.tenant_id,
                    platform=request.platform,
                    parent_channel_id=request.parent_channel_id,
                    thread_id=request.thread_id,
                    requester_account_id=request.requester_account_id,
                    requester_external_user_id=request.requester_external_user_id,
                    target_ma_agent_id=request.target_ma_agent_id,
                    target_name=request.target_name,
                    reason=request.reason,
                    idempotency_key=request.idempotency_key,
                    requested_work=request.requested_work,
                )
    except HandoffRefusedInSetupThread as error:
        # A setup conversation was opened on this location between the decision
        # and the write; the store re-read it under a row lock and refused.
        raise ToolError(render_tool_refusal_setup_thread(destination_name)) from error

    return TaskHandoffResult(
        destination_name=destination_name,
        destination_ma_agent_id=destination.id,
        previous_responder_name=origin.responder_name,
        continuation_recorded=request is not None,
        confirmation=render_handoff_acknowledged(
            target_name=destination_name,
            from_name=origin.responder_name,
            channel=_channel_mention(platform, origin.parent_channel_id),
            requested_work=queued_work,
        ),
        instruction=_REPLY_VERBATIM,
    )


def _refusal_text(refusal: HandoffRefused, *, channel: str) -> str:
    if refusal.reason == "setup_thread":
        return render_tool_refusal_setup_thread(refusal.destination_name)
    if refusal.reason == "unreachable":
        return render_tool_refusal_unreachable(refusal.destination_name, channel)
    if refusal.reason == "pinned_elsewhere":
        return "\n".join(
            [
                f"{refusal.destination_name} is pinned to other channels by an operator, "
                "so it can't take over a conversation here.",
                "Tell the caller to ask in one of that agent's own channels.",
                "Nothing was changed. Do not retry.",
            ]
        )
    if refusal.reason == "admin_required":
        return "\n".join(
            [
                f"{refusal.destination_name} is not the agent this channel answers with, "
                "and handing a conversation to another agent brings its repository, keys "
                "and connectors here, so only a workspace or server admin can do it.",
                "Tell the caller an admin can make the handoff, or ask in one of that "
                "agent's own channels.",
                "Nothing was changed. Do not retry.",
            ]
        )
    return "\n".join(
        [
            f"{refusal.destination_name} already answers in this conversation.",
            "There is nothing to hand over. Tell the caller it is already the one working on this.",
            "Nothing was changed. Do not retry.",
        ]
    )


async def _start_fresh_task_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
) -> TaskFreshStartResult:
    """Record that this caller wants a new empty workspace from their next message.

    Nothing is torn down here. The request is a mark on the caller's live
    mapping row; the next bind builds the replacement first and only then
    retires the old one, so a fresh start that fails halfway leaves the
    existing work reachable. A caller with no live row has nothing to retire
    and still gets the confirmation — their next message starts clean either
    way, which is exactly what they asked for.
    """
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    async with runtime.session_factory.begin() as session:
        live_session = await get_live_thread_session(
            session,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            thread_id=origin.thread_id,
            account_id=auth.account_id,
        )
        if live_session is not None:
            await request_fresh_start(session, id=live_session.id, at=datetime.now(UTC))
    return TaskFreshStartResult(
        confirmation=render_fresh_start(origin.responder_name),
        instruction=_REPLY_VERBATIM,
    )


def register_task_continuity_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def hand_off_task(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
        agent_id: Annotated[
            str,
            Field(description="The destination agent's current MA id, e.g. agt_01…"),
        ],
        continuation: Annotated[
            str | None,
            Field(
                description=(
                    "Null unless the person explicitly asked the destination to continue "
                    "or finish NAMED work, in their own words. 'Take over' alone, with no "
                    "named work, is null. Never restate work that already finished."
                )
            ),
        ] = None,
        unsaved_work: Annotated[
            Literal["copy", "leave"] | None,
            Field(description="Only after the person answers the uncommitted-work question."),
        ] = None,
    ) -> TaskHandoffResult:
        """Hand this task over to another agent in the same conversation: "have that one take over", "let churn-explorer finish this".

        `set_setup_target` changes your configuration target; `set_agent_default`
        changes who answers a channel; neither does this. The destination must
        already answer in this workspace. Only an admin may hand to an agent other
        than the one this channel answers with.

        From the next message here, that agent answers, with its own keys,
        connections and memory; conversation, decisions and files move with the
        requester. Posted files stay posted. No extra billed turn unless
        `continuation` is set.

        `agent_id`: the destination's current id from `list_agents`; a recreated
        namesake is refused. `continuation` is null unless the person asked the
        destination to continue or finish NAMED work ("let it finish the chart").
        "Take over" alone is switch-only — null. Never restate finished work. A
        too-short, empty, or name-only value is auto-nulled — get the wording
        right regardless.
        """  # noqa: E501
        return await _hand_off_task_impl(
            runtime,
            await _auth(ctx),
            origin_context_id=origin_context_id,
            agent_id=agent_id,
            continuation=continuation,
            unsaved_work=unsaved_work,
        )

    @mcp.tool
    async def start_fresh_task(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        origin_context_id: str,
    ) -> TaskFreshStartResult:
        """Start this conversation's work over with an empty workspace: "let's start fresh", "start over", "clear the workspace and begin a new task".

        Only when someone asks for a clean slate. A key, model, repo or instruction
        change keeps the task; `hand_off_task` moves it to another agent. Neither needs
        this.

        From the next message here, the agent works in a new empty workspace. It leaves
        behind this task's working files and unfinished work. It keeps everything
        already posted in this thread, and the agent's saved memory, keys and
        connections. Nothing is removed until the new workspace is ready. Who answers
        here does not change.
        """  # noqa: E501
        return await _start_fresh_task_impl(
            runtime, await _auth(ctx), origin_context_id=origin_context_id
        )
