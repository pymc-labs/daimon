"""Conversational task handoff and fresh start.

Two tools, both called from inside the mention turn that carries the request,
so changing who answers costs no extra billed turn. Both are authorized the
same way as `set_setup_target`: a trusted `origin_context_id` bound to this
caller, tenant and platform, and a concrete MA agent id — a recreated namesake
has a different id and cannot receive a handoff.

Neither tool writes channel or workspace routing. A handoff binds one thread;
who answers everywhere else is untouched. But the thread then runs as the
destination, with its repo, keys, connectors and memory, so a member may only
hand a thread to an agent of this channel: the one it answers with, one pinned
to it, or one of an isolated channel's own agents. Bringing in any other agent
channels can't be brought in at all. `daimon.core.thread_handoff` decides and
writes the switch under the tenant policy lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._rule_view import load_isolation_view
from daimon.adapters.mcp.tools._words import admin_noun
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.authz import Action, build_agent_ref
from daimon.core.continuity.continuation import (
    MAX_REQUESTED_WORK,
    ContinuationRequest,
    sanitize_requested_work,
)
from daimon.core.continuity.handoff import HandoffRefused, HandoffRefusedInSetupThread
from daimon.core.continuity.messages import render_fresh_start, render_handoff_acknowledged
from daimon.core.continuity.tool_messages import (
    render_tool_refusal_setup_thread,
    render_tool_refusal_unreachable,
    render_tool_unsaved_work_question,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_FORKED_FROM,
    MA_METADATA_KEY_FORKED_IN,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    forked_in,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.native_admins import AdminCaller
from daimon.core.security_audit import record_authz_denial
from daimon.core.stores import agent_backends
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import ChatPlatform, TurnOriginRow
from daimon.core.stores.task_continuations import record_continuation
from daimon.core.stores.thread_session_lineage import request_fresh_start
from daimon.core.stores.thread_sessions import (
    get_live_thread_session,
    set_pending_unsaved_work,
)
from daimon.core.thread_handoff import (
    HandoffCaller,
    HandoffDestination,
    ThreadHandoffRefused,
    hand_over_thread,
    locked_thread_sessions,
    may_carry_work,
    recorded_thread_sessions,
    render_handoff_refused,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.config import DEFAULT_BACKEND
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

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


def forked_in_value(origin: TurnOriginRow) -> str:
    """This conversation, as a copy made in it records it (`MA_METADATA_KEY_FORKED_IN`)."""
    return forked_in(origin.platform, origin.parent_channel_id, origin.thread_id)


async def copy_awaiting_its_switch(
    runtime: McpRuntime,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    *,
    ma_agent_id: str,
    metadata: Mapping[str, str] | None,
) -> bool:
    """A copy fork_agent made of this conversation's responder, in this conversation,
    still on the responder's provider and not answering here yet (J1).

    Its switch card's approval hands it this conversation and switches it in one
    step; any other copy (an older one, a channel rule's) is an ordinary agent.
    """
    from daimon.core.stores import thread_agent_bindings

    stamp = metadata or {}
    if (
        ma_agent_id == origin.responder_ma_agent_id
        or stamp.get(MA_METADATA_KEY_FORKED_FROM) != origin.responder_ma_agent_id
        or stamp.get(MA_METADATA_KEY_FORKED_IN) != forked_in_value(origin)
    ):
        return False
    async with runtime.session_factory() as session:
        handed = await thread_agent_bindings.get_binding(
            session,
            tenant_id=auth.tenant_id,
            platform=origin.platform,
            parent_channel_id=origin.parent_channel_id,
            thread_id=origin.thread_id,
        )
        if handed is not None and handed.responder_ma_agent_id == ma_agent_id:
            return False
        copy, source = (
            (revision.backend if revision is not None else None) or DEFAULT_BACKEND
            for revision in [
                await agent_backends.current(
                    session,
                    auth.tenant_id,
                    derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=agent),
                )
                for agent in (ma_agent_id, origin.responder_ma_agent_id)
            ]
        )
    return copy == source


class _HandOffChecked(Exception):
    """Rolls back a hand-off that was only checked (`switch="check"`)."""


async def _hand_off_task_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    origin_context_id: str,
    agent_id: str,
    continuation: str | None = None,
    unsaved_work: Literal["copy", "leave"] | None = None,
    keep_provider: bool = False,
    switch: Literal["check", "commit"] | None = None,
) -> TaskHandoffResult:
    """Bind this thread's responder to another agent, optionally queueing work.

    Every check runs before anything is written, so a refused handoff leaves
    the conversation exactly as it was — including the uncommitted-work
    question, which is a refusal the caller answers and retries.

    `switch` is a confirmed backend switch handing this thread to a copy awaiting
    it (`copy_awaiting_its_switch`): "check" runs every check and writes nothing,
    before the switch; "commit" hands over after it, when the copy is already on
    the provider the switch moves this thread to.
    """
    origin = await require_turn_origin(runtime, auth, origin_context_id)
    # `require_turn_origin` already refused any non-chat platform.
    platform = cast(ChatPlatform, origin.platform)

    agents = await list_agents_by_tenant(runtime.client, tenant_id=auth.tenant_id)
    # A thread under an isolated channel hands off only to that channel's own
    # agents; anywhere else, never to them.
    caller = await load_isolation_view(
        runtime, auth, agents=agents, location_channel_id=origin.parent_channel_id
    )
    destination = next((agent for agent in agents if agent.id == agent_id), None)
    if destination is None or not caller.sees_agent(destination):
        raise ToolError(
            "That agent is not here, or it was recreated and has a new id. "
            "Look it up with list_agents and pass the id it reports now. Nothing was changed."
        )
    destination_name = destination.metadata.get(MA_METADATA_KEY_NAME)
    if not destination_name:
        raise ToolError(
            "That agent has no configuration name, so nobody can reach it by name. "
            "Choose a named agent here. Nothing was changed."
        )
    if (
        switch is None
        and not keep_provider
        and await copy_awaiting_its_switch(
            runtime, auth, origin, ma_agent_id=destination.id, metadata=destination.metadata
        )
    ):
        # J1: the model chose the order before. Sometimes it handed the thread to the
        # copy and stopped, and the copy's next turn posted the switch card instead
        # of answering.
        raise ToolError(
            f"{destination_name} is a new copy of you, still on your model provider. If "
            "the person asked for it on another provider, post its switch card now "
            f"instead (request_agent_backend_switch for {destination_name}, with the "
            "preview's digest): approving the card hands this thread to it and switches "
            "it, in one step. If the copy should stay on this provider, call "
            "hand_off_task again with keep_provider=true. Nothing was changed."
        )
    continuation = sanitize_requested_work(continuation, echoes=(destination_name,))
    if continuation is not None and auth.platform_user_id is None:
        raise ToolError(
            "Continuing work needs the requester's platform identity, which this "
            "connection does not carry. Ask from the conversation itself."
        )

    async with runtime.session_factory() as session:
        live_session = await get_live_thread_session(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            thread_id=origin.thread_id,
            account_id=auth.account_id,
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
            attended=origin.attended,
            idempotency_key=uuid.uuid4(),
        )
    )

    # Queued work reaches only a destination that can read the caller's session.
    admin = AdminCaller(
        platform_user_id=auth.platform_user_id,
        is_server_admin=auth.is_platform_admin,
        role_ids=auth.verified_role_ids,
    )
    recorded = (
        await recorded_thread_sessions(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            platform=platform,
            thread_id=origin.thread_id,
        )
        if request is not None
        else None
    )
    handoff_destination = HandoffDestination(
        ma_agent_id=destination.id,
        name=destination_name,
        agent=build_agent_ref(destination.name, destination.metadata, destination_name),
        is_daimon_managed=destination.metadata.get(MA_METADATA_KEY_MANAGED) == "true",
    )
    work_withheld = False

    try:
        # One transaction, decided under the tenant policy lock
        # (`hand_over_thread`): the confirmation promises both the switch and
        # the continuation, so a thread must never end up switched with the
        # work it was told would be picked up missing, and a policy edit
        # committed before the decision is the one it is decided on.
        async with runtime.session_factory.begin() as session:
            await hand_over_thread(
                session,
                tenant_id=auth.tenant_id,
                platform=platform,
                parent_channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                caller=HandoffCaller(
                    account_id=auth.account_id,
                    admin=admin,
                    via_agent_key=auth.agent_id is not None,
                    human_invocation=origin.human_invocation,
                ),
                destination=handoff_destination,
                current_responder_ma_agent_id=origin.responder_ma_agent_id,
                default=runtime.deployment_default,
                now=datetime.now(UTC),
                recorded=recorded,
                membership_resolver=runtime.membership_resolver,
            )
            if switch != "commit":
                # After the decision, so authorization speaks first; raising rolls it
                # back. A confirmed switch moves this thread to the copy's provider.
                await _refuse_another_providers_destination(
                    session,
                    auth,
                    origin,
                    destination_ma_agent_id=destination.id,
                    destination_name=destination_name,
                )
            if repo is not None and unsaved_work is None:
                # Raised inside the transaction, so the binding rolls back.
                raise ToolError(render_tool_unsaved_work_question(repo))
            if switch == "check":
                raise _HandOffChecked
            if unsaved_work is not None and live_session is not None:
                # The answer outlives this turn: the replacement it governs
                # happens at this caller's NEXT message, when the destination
                # binds. Written in the same transaction as the binding so a
                # thread can never end up switched with the answer lost.
                await set_pending_unsaved_work(session, id=live_session.id, choice=unsaved_work)
            recorded = await locked_thread_sessions(
                session, tenant_id=auth.tenant_id, platform=platform, thread_id=origin.thread_id
            )
            if request is not None and not may_carry_work(
                await load_access_policy(session, tenant_id=auth.tenant_id),
                destination=handoff_destination,
                parent_channel_id=origin.parent_channel_id,
                thread_id=origin.thread_id,
                recorded=recorded or (),
                account_id=auth.account_id,
            ):
                # The next bind won't carry this session to the destination,
                # so the work named in it doesn't go either.
                request, queued_work, work_withheld = None, None, True
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
                    attended=request.attended,
                    idempotency_key=request.idempotency_key,
                    requested_work=request.requested_work,
                )
    except ThreadHandoffRefused as refused:
        if refused.refusal.authz_reason is not None:
            record_authz_denial(Action.HAND_OFF, refused.refusal.authz_reason)
        raise ToolError(
            _refusal_text(
                refused.refusal,
                channel=_channel_mention(platform, origin.parent_channel_id),
                admin=admin_noun(platform),
            )
        ) from refused
    except HandoffRefusedInSetupThread as error:
        raise ToolError(render_tool_refusal_setup_thread(destination_name)) from error
    except _HandOffChecked:
        pass

    confirmation = render_handoff_acknowledged(
        target_name=destination_name,
        from_name=origin.responder_name,
        channel=_channel_mention(platform, origin.parent_channel_id),
        requested_work=queued_work,
    )
    if work_withheld:
        confirmation += (
            f"\n{destination_name} can't read this conversation's history, which only "
            "conversations inside its channel can read, so the work you named was not passed on."
        )
    return TaskHandoffResult(
        destination_name=destination_name,
        destination_ma_agent_id=destination.id,
        previous_responder_name=origin.responder_name,
        continuation_recorded=request is not None,
        confirmation=confirmation,
        instruction=_REPLY_VERBATIM,
    )


async def _refuse_another_providers_destination(
    session: AsyncSession,
    auth: AuthIdentity,
    origin: TurnOriginRow,
    *,
    destination_ma_agent_id: str,
    destination_name: str,
) -> None:
    """Refuse a hand-off to an agent on another provider than this thread is pinned to.

    The thread keeps its provider, so the destination would answer here on the
    old one. Moving a thread drops its history, which only a confirmed switch
    may do (`daimon.core.thread_release`), never this unconfirmed tool.
    """
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores import agent_backends, mux_state
    from daimon.core.stores.thread_sessions import get_thread_session_at
    from daimon.core.thread_release import caller_slot
    from daimon.core.turn.notices import provider_name
    from mux.contracts.config import DEFAULT_BACKEND

    setting = await agent_backends.current(
        session,
        auth.tenant_id,
        derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=destination_ma_agent_id),
    )
    target = (setting.backend if setting is not None else None) or DEFAULT_BACKEND
    slot = caller_slot(
        tenant_id=auth.tenant_id,
        platform=origin.platform,
        channel_id=origin.parent_channel_id,
        thread_id=origin.thread_id,
        account_id=auth.account_id,
    )
    pinned = await mux_state.get_binding(
        session, slot.model_copy(update={"account_id": None})
    ) or await mux_state.get_binding(session, slot)
    if pinned is not None:
        here = pinned.provider
    elif await get_thread_session_at(
        session,
        tenant_id=auth.tenant_id,
        platform=origin.platform,
        thread_id=origin.thread_id,
        account_id=auth.account_id,
        at=datetime.now(UTC),
    ):
        here = DEFAULT_BACKEND
    else:
        return  # Nothing pins this thread yet: the destination's provider applies.
    if here == target:
        return
    raise ToolError(
        f"{destination_name} runs on {provider_name(target)}, and this conversation runs on "
        f"{provider_name(here)}, so it can't take over here. Nothing changed. To have "
        f"{destination_name} answer here, hand this conversation over while it is on "
        f"{provider_name(here)}, then switch it with a confirmation; or ask it in a new "
        "conversation."
    )


def _refusal_text(refusal: HandoffRefused, *, channel: str, admin: str) -> str:
    if refusal.reason == "setup_thread":
        return render_tool_refusal_setup_thread(refusal.destination_name)
    if refusal.reason == "unreachable":
        return render_tool_refusal_unreachable(refusal.destination_name, channel)
    if refusal.reason == "runs_elsewhere":
        return "\n".join(
            [
                f"{refusal.destination_name} only runs in other channels (its agent rule), "
                "so it can't take over a conversation here.",
                "Tell the caller to ask in one of that agent's own channels.",
                "Nothing was changed. Do not retry.",
            ]
        )
    if refusal.reason == "admin_required":
        return "\n".join(
            [
                f"{refusal.destination_name} is not one of this channel's agents (the one "
                "it answers with, or one whose agent rule names it), and handing a conversation to "
                "another agent brings its repository, keys and connectors here, so only "
                f"{admin} can do it.",
                "Tell the caller an admin can make the handoff, or ask in one of that "
                "agent's own channels.",
                "Nothing was changed. Do not retry.",
            ]
        )
    if refusal.reason in ("writers_none", "invoker_not_allowed", "own_agents_only", "not_a_reader"):
        return "\n".join(
            [
                render_handoff_refused(refusal, channel=channel),
                "Tell the caller. Do not retry.",
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
        keep_provider: Annotated[
            bool,
            Field(
                description=(
                    "True only for a new copy of you that should stay on your model "
                    "provider. A copy being switched to another provider is handed the "
                    "thread after its switch card is approved, never before."
                )
            ),
        ] = False,
    ) -> TaskHandoffResult:
        """Hand this task over to another agent in the same conversation: "have that one take over", "let churn-explorer finish this".

        `set_setup_target` changes your configuration target; `set_agent_default`
        changes who answers a channel; neither does this. The destination must
        already answer in this workspace. A member may hand only to an agent of
        this channel (the one it answers with, or one pinned to it); any other
        needs an admin.

        From the next message here, that agent answers, with its own keys,
        connections and memory; conversation, decisions and files move with the
        requester. Posted files stay posted. No extra billed turn unless
        `continuation` is set.

        `origin_context_id` must name this execution's origin and match the
        calling session's signed grant. Empty IDs and other sessions' origins
        refuse, even for the same account and agent. Attendance comes from
        that verified origin; queueing work does not grant human approval.

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
            keep_provider=keep_provider,
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
        connections. Nothing is removed until the new working files are ready. Who answers
        here does not change.
        """  # noqa: E501
        return await _start_fresh_task_impl(
            runtime, await _auth(ctx), origin_context_id=origin_context_id
        )
