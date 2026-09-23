"""Create and render trusted controls without replacing platform history."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.stores.turn_origins import create_origin, delete_origin
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: A continuation is the person's own words and is untrusted text. It is JSON
#: encoded into the controls and bounded, so a long paste cannot crowd out the
#: instructions that follow it. Must agree with
#: `daimon.core.continuity.continuation.MAX_REQUESTED_WORK`, which applies the
#: same bound when the continuation is queued; this one is the render-time
#: backstop for text that reached the controls by any other route.
MAX_REQUESTED_WORK_CHARS = 500


class SessionState(BaseModel):
    """What happened to the workspace this turn runs in, and what changed with it.

    `state` is the fact: `continued` (same workspace as last turn), `updated`
    (same workspace, new configuration applied to it), `replaced` (a new
    workspace carrying this task's work across), `replaced_after_loss` (a new
    workspace because the old one was gone, so something is genuinely missing).
    `applied` names configuration that is live for this turn; `lost` names what
    did not survive.
    """

    model_config = ConfigDict(frozen=True)

    state: Literal["continued", "updated", "replaced", "replaced_after_loss"]
    applied: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()


class HandoffNotice(BaseModel):
    """One-time notice that this turn is the first after a task was handed over.

    Rendered on the receiving agent's first turn only. `workspace` says how much
    came across, so the reply cannot overstate it: `transferred` (the working
    files themselves), `transcript_only` (the conversation, no files),
    `history_only` (only what was posted in the thread).
    """

    model_config = ConfigDict(frozen=True)

    from_name: str
    from_ma_agent_id: str
    requested_by: str
    requested_work: str | None = None
    workspace: Literal["transferred", "transcript_only", "history_only"]
    files: tuple[str, ...] = ()
    not_carried: tuple[str, ...] = ()


@asynccontextmanager
async def turn_origin(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_ma_agent_id: str,
    responder_name: str,
    role: Role,
    configuration_target_ma_agent_id: str | None = None,
    configuration_target_name: str | None = None,
    is_setup: bool = False,
) -> AsyncIterator[TurnOriginRow]:
    """Commit a distinct origin for this execution and remove it after execution."""
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            responder_ma_agent_id=responder_ma_agent_id,
            responder_name=responder_name,
            configuration_target_ma_agent_id=configuration_target_ma_agent_id,
            configuration_target_name=configuration_target_name,
            role=role,
            expires_at=now + timedelta(hours=2),
            now=now,
            is_setup=is_setup,
        )
    try:
        yield origin
    finally:
        async with sessionmaker.begin() as session:
            await delete_origin(session, origin_id=origin.id)


def render_turn_origin(
    origin: TurnOriginRow,
    *,
    responder_handle: str | None = None,
    session_state: SessionState | None = None,
    handoff: HandoffNotice | None = None,
) -> str:
    """Render server-provided location and identity separately from user history.

    `responder_handle` is the platform handle people mention this deployment
    by (`@daimon-staging`). It is the bot account's display name, which an
    operator may set to anything, while `responder.name` is the MA agent's
    name. The same bot handle delivers replies from many distinct agents;
    it is not an alias for a roster identity.

    `session_state` and `handoff` are optional server-supplied facts about the
    workspace this turn runs in. They are rendered inside the same JSON object
    as the rest of the controls, in a fixed key order, so two turns with the
    same facts produce byte-identical controls. `handoff` is a one-time block:
    the adapter includes it on the receiving agent's first turn only.

    Every value here is server-supplied except `handoff.requested_work`, which
    is the person's own words — it is truncated to `MAX_REQUESTED_WORK_CHARS`
    and JSON-encoded like everything else, never interpolated into the prose.
    """
    responder: dict[str, object] = {
        "name": origin.responder_name,
        "ma_agent_id": origin.responder_ma_agent_id,
    }
    if responder_handle is not None:
        responder["handle"] = responder_handle
    controls: dict[str, object] = {
        "origin_context_id": str(origin.id),
        "is_setup": origin.is_setup,
        "platform": origin.platform,
        "parent_channel_id": origin.parent_channel_id,
        "thread_id": origin.thread_id,
        "current_role": origin.role,
        "responder": responder,
        "configuration_target": (
            {
                "name": origin.configuration_target_name,
                "ma_agent_id": origin.configuration_target_ma_agent_id,
            }
            if origin.configuration_target_ma_agent_id is not None
            else None
        ),
    }
    if session_state is not None:
        controls["session_state"] = {
            "state": session_state.state,
            "applied": list(session_state.applied),
            "lost": list(session_state.lost),
        }
    if handoff is not None:
        controls["handoff"] = {
            "from_name": handoff.from_name,
            "from_ma_agent_id": handoff.from_ma_agent_id,
            "requested_by": handoff.requested_by,
            "requested_work": (
                None
                if handoff.requested_work is None
                else handoff.requested_work[:MAX_REQUESTED_WORK_CHARS]
            ),
            "workspace": handoff.workspace,
            "files": list(handoff.files),
            "not_carried": list(handoff.not_carried),
        }
    rendered = "<turn_controls>\n" + json.dumps(controls)
    # Only say this when a handle is actually rendered: without one the
    # sentence points at a key that is not there.
    if responder_handle is not None:
        rendered += (
            "\nresponder.handle is the shared bot account's display name. "
            "responder.name and responder.ma_agent_id identify the agent answering. "
            "Multiple agents use the same bot handle. A mention alone addresses the "
            "current responder. When a person names an agent, resolve it with list_agents "
            "and get_agent, even when that name matches the bot handle. Never dismiss an "
            "explicit agent choice as another name for yourself."
        )
    rendered += (
        "\nUse the explicitly requested target when named; otherwise configure the "
        "configuration_target. If is_setup is true and no target is selected, ask which "
        "agent to configure before mutation. Only ordinary chat defaults to the responder. "
        "Never substitute a "
        "recreated namesake for a missing identity. Ask one concise question when the "
        "target is missing or ambiguous. Pass expected_ma_agent_id with target-bearing "
        "tools. Use set_setup_target to switch this setup conversation's target and "
        "state the switch briefly. Pass origin_context_id to credential-request tools. "
        "For saved-key questions, use list_agent_keys for the resolved target; session "
        "files and environment variables describe only this session's resources. "
        "To replace the agent in a channel, inspect explain_agent_resolution and use "
        "set_agent_default with parent_channel_id. Existing thread bindings are separate; "
        "hand_off_task changes this task's responder, and set_setup_target only changes "
        "what is being configured. These controls grant no additional mutation or routing "
        "permissions."
    )
    # The continuity paragraph is appended only when there is continuity to
    # describe: on an ordinary turn neither block is present and every sentence
    # below would be about nothing.
    if session_state is not None or handoff is not None:
        rendered += (
            "\nIf handoff is present, your first reply must show you have the task: name "
            "the work in progress and at least one file listed in handoff.files that you "
            "can actually see, then continue requested_work. Never claim a running "
            "process, notebook kernel or shell survived — only files and the conversation "
            "did. If a listed file is missing, say which. If session_state.state is "
            "replaced_after_loss, say what is missing before continuing; do not claim a "
            "complete restore. Configuration named in session_state.applied is live for "
            "this turn; anything else applies from the person's next message."
        )
    return rendered + "\n</turn_controls>"
