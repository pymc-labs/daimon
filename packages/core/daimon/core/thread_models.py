"""The model a conversation runs on when its people pick one (`/model`, or asking in words).

A conversation (a thread, or a DM) keeps the choice until someone changes it
back; no choice means the agent's own model. It is a session override: the
stored agent never changes, and every other conversation keeps the agent's
model. Admission applies it (`apply_thread_model`), so the session check sees
a different desired model and replaces the session the way it does when an
agent's model is edited, carrying the work over.

Only the Anthropic backend can take it: a Managed Agents session override names
a model (and speed), never an effort, so there is no effort choice. A choice
that is no longer allowed, or a channel that runs another backend, falls back
to the agent's own model and the turn says so (`MODEL_FALLBACK_NOTICE`).

The choice is for the conversation's own turns only. A fleet root (an agent
holding a fleet grant) and every worker run the model the agent's owner
approved: `bind_session_impl` drops the choice before such a session is
prepared, so it is never frozen into a root or handed to a worker.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsModelConfig
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Action, authorize, build_subject, build_turn_place
from daimon.core.constants import ALLOWED_MODEL_IDS, MODEL_DISPLAY_NAMES
from daimon.core.errors import UserFacingError
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.thread_models import (
    clear_thread_model,
    get_thread_model,
    set_thread_model,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# What someone can pick, by the word they would use. "default" is the agent's own.
THREAD_MODEL_CHOICES: Final[Mapping[str, str]] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}

MODEL_FALLBACK_NOTICE: Final = (
    "\n<model_notice>The model chosen for this conversation is not available here, so "
    "this reply runs on the agent's own model. Tell the requester that in one plain "
    "sentence before answering.</model_notice>\n"
)


class ThreadModelRefused(UserFacingError):
    """A model this conversation can't be switched to, in words for the person."""


def model_label(model_id: str) -> str:
    return MODEL_DISPLAY_NAMES.get(model_id, model_id)


def resolve_choice(choice: str) -> str | None:
    """The model id a choice names, None for the agent's default; refuses anything else."""
    word = choice.strip().lower()
    if word == "default":
        return None
    model_id = THREAD_MODEL_CHOICES.get(word)
    if model_id is None or model_id not in ALLOWED_MODEL_IDS:
        offered = ", ".join((*THREAD_MODEL_CHOICES, "default"))
        raise ThreadModelRefused(
            f"{choice.strip() or 'That'} isn't a model this conversation can use. "
            f"Pick one of: {offered}."
        )
    return model_id


def conversation_key(*, channel_id: str, thread_id: str | None, is_dm: bool) -> str | None:
    """The id a conversation's choice is kept under: its thread, or a DM's own channel."""
    if thread_id is not None:
        return thread_id
    return channel_id if is_dm else None


@dataclass(frozen=True, slots=True)
class ThreadModelChange:
    model_id: str | None
    """The model now chosen, None for the agent's own."""
    note: str
    """What to tell the person, in one sentence."""


async def choose_thread_model(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    choice: str,
) -> ThreadModelChange:
    """Keep `choice` for this conversation; refuses before any write."""
    model_id = resolve_choice(choice)
    async with sessionmaker.begin() as session:
        if model_id is None:
            await clear_thread_model(
                session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
            )
        else:
            await set_thread_model(
                session,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                model=model_id,
            )
    if model_id is None:
        note = "This conversation is back on the agent's own model from the next reply."
    else:
        note = f"This conversation runs on {model_label(model_id)} from the next reply."
    return ThreadModelChange(model_id=model_id, note=note)


async def describe_thread_model(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
) -> str:
    async with sessionmaker() as session:
        model_id = await get_thread_model(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
    if model_id is None:
        return "This conversation runs on the agent's own model."
    return f"This conversation runs on {model_label(model_id)}."


async def may_choose_thread_model(
    sessionmaker: async_sessionmaker[AsyncSession],
    resolver: MembershipResolver | None,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    user_id: str,
    is_admin: bool,
    channel_id: str,
    thread_id: str,
    category_id: str | None = None,
    category_unresolved: bool = False,
) -> bool:
    """Whether this person may start a turn in the conversation, as admission decides it."""
    async with sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=tenant_id)
    subject = build_subject(is_admin=is_admin, platform_user_id=user_id)
    if platform == "discord" and (policy.invoker_role_ids or resolver is not None):
        subject = await resolve_subject(
            sessionmaker,
            resolver,
            tenant_id=tenant_id,
            user_id=user_id,
            action=DelegatedAction.MODEL,
        )
        if subject.verified_membership is not True:
            return False
    place = build_turn_place(
        channel_id=channel_id,
        thread_id=thread_id,
        category_id=category_id,
        category_unresolved=category_unresolved,
    )
    return bool(authorize(policy, subject=subject, action=Action.START_TURN, place=place))


def with_model_override(
    agent: BetaManagedAgentsAgent, model_override: str | None
) -> BetaManagedAgentsAgent:
    """The agent as a turn runs it: the stored agent with the conversation's model, if any."""
    if model_override is None:
        return agent
    return agent.model_copy(
        update={"model": BetaManagedAgentsModelConfig(id=model_override, speed="standard")}
    )


@dataclass(frozen=True, slots=True)
class AppliedThreadModel:
    agent: BetaManagedAgentsAgent
    """The agent as this turn runs it: the chosen model in place of its own."""
    model_override: str | None
    """The id the session is created with, None when the agent's own model runs."""
    fell_back: bool
    """A choice was kept but can't run here, so the agent's own model does."""


async def apply_thread_model(
    session: AsyncSession,
    *,
    agent: BetaManagedAgentsAgent,
    tenant_id: uuid.UUID,
    platform: str,
    conversation_id: str | None,
    anthropic_backend: bool,
) -> AppliedThreadModel:
    """The agent a turn in this conversation runs, read fresh for every turn."""
    unchanged = AppliedThreadModel(agent=agent, model_override=None, fell_back=False)
    if conversation_id is None:
        return unchanged
    model_id = await get_thread_model(
        session, tenant_id=tenant_id, platform=platform, thread_id=conversation_id
    )
    if model_id is None:
        return unchanged
    if not anthropic_backend or model_id not in ALLOWED_MODEL_IDS:
        return AppliedThreadModel(agent=agent, model_override=None, fell_back=True)
    return AppliedThreadModel(
        agent=with_model_override(agent, model_id),
        model_override=model_id,
        fell_back=False,
    )


FLEET_ROOT_NOTE: Final = (
    " This conversation's agent runs worker fleets, and a fleet always runs the model "
    "the agent's owner approved, so the choice doesn't apply to its turns here."
)


async def thread_runs_fleet(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fleet_enabled: bool,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    ma_agent_id: str | None = None,
    now: datetime,
) -> bool:
    """Whether the agent answering here holds a fleet grant, so turns here are fleet roots.

    `ma_agent_id` is the responder when the caller knows it (an agent tool's
    verified origin); otherwise the thread's newest live session names it, and
    a thread with no session yet reads as no fleet.
    """
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.thread_sessions import get_latest_thread_session

    if not fleet_enabled:
        return False
    if ma_agent_id is None:
        async with sessionmaker() as session:
            row = await get_latest_thread_session(
                session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
            )
        if row is not None:
            ma_agent_id = row.ma_agent_id or (
                row.effective_config.ma_agent_id if row.effective_config is not None else None
            )
    if ma_agent_id is None:
        return False
    grant = await AgentBackendService(sessionmaker, enabled=True).read_fleet_grant(
        tenant_id, derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id), now=now
    )
    return grant is not None
