"""The model a conversation runs on when its people pick one (`/model`, or asking in words).

A conversation (a thread, or a DM) keeps the choice until someone changes it
back; no choice means the agent's own model. It is a session override: the
stored agent never changes, and every other conversation keeps the agent's
model. Admission applies it (`apply_thread_model`), so the session check sees
a different desired model and replaces the session the way it does when an
agent's model is edited, carrying the work over.

A Claude pick is a Managed Agents session override: it names a model and keeps the
agent's effort unless the conversation chose one (`/model`'s effort, which can also be
chosen alone). A pick on another provider, or Claude for an agent that runs
elsewhere, runs through `thread_backend` with the consent `thread_model_switch`
stored. A choice that is no longer allowed falls back to the agent's own model
and the turn says so (`MODEL_FALLBACK_NOTICE`).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsModelConfig
from daimon.core.admin_actions import DelegatedAction
from daimon.core.agent_model_names import model_name
from daimon.core.authz import Action, authorize, build_subject, build_turn_place
from daimon.core.channel_backend import ThreadBackend
from daimon.core.constants import MODEL_DISPLAY_NAMES
from daimon.core.errors import UserFacingError
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.models_catalog import is_priced_claude_model
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.thread_models import (
    clear_thread_model,
    get_thread_choice,
    get_thread_consent,
    get_thread_effort,
    set_thread_model,
)
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef
from mux.profiles import backend_selection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# What someone can pick, by the word they would use. "default" is the agent's own.
THREAD_MODEL_CHOICES: Final[Mapping[str, str]] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-2",
}

#: The efforts a conversation can pick, plus "default" for the agent's own.
THREAD_EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")


_NO_XHIGH: Final = ("low", "medium", "high", "max")

#: The efforts each Claude model takes, from Anthropic's model documentation. A model
#: not listed (Haiku 4.5 and older, or an id not confirmed) takes none.
_MODEL_EFFORTS: Final[Mapping[str, tuple[str, ...]]] = {
    **dict.fromkeys(
        (
            "claude-fable-5",
            "claude-fable-5-1",
            "claude-fable-5-2",
            "claude-mythos-5-1",
            "claude-mythos-5-2",
            "claude-opus-5-5",
            "claude-opus-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-5-5",
            "claude-sonnet-5",
            "claude-haiku-5-5",
        ),
        THREAD_EFFORTS,
    ),
    "claude-opus-4-6": _NO_XHIGH,
    "claude-sonnet-4-6": _NO_XHIGH,
}


def model_efforts(model_id: str) -> tuple[str, ...]:
    """The efforts a Claude model takes; none for a model not confirmed to take any."""
    return _MODEL_EFFORTS.get(model_id, ())


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
    if model_id is None or not is_priced_claude_model(model_id):
        offered = ", ".join((*THREAD_MODEL_CHOICES, "default"))
        raise ThreadModelRefused(
            f"{choice.strip() or 'That'} isn't a model this conversation can use. "
            f"Pick one of: {offered}."
        )
    return model_id


def resolve_effort(effort: str | None) -> tuple[str | None, bool]:
    """The effort a pick names and whether it returns to the agent's own; refuses the unknown."""
    if effort is None or not effort.strip():
        return None, False
    word = effort.strip().lower()
    if word == "default":
        return None, True
    if word not in THREAD_EFFORTS:
        offered = ", ".join((*THREAD_EFFORTS, "default"))
        raise ThreadModelRefused(
            f"{effort.strip()} isn't an effort this conversation can use. Pick one of: {offered}."
        )
    return word, False


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
    effort: str | None = None
    """The effort now chosen, None for the agent's own."""


async def choose_thread_model(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    choice: str | None = None,
    effort: str | None = None,
) -> ThreadModelChange:
    """Keep `choice` and/or `effort` for this conversation; refuses before any write.

    A pick without a model keeps the one already chosen, and one without an effort
    keeps that. "default" returns the model (or, for the effort, the effort) to the
    agent's own; "default" alone clears both.
    """
    if choice is None and effort is None:
        raise ThreadModelRefused("Pick a model, an effort, or both.")
    model_id = resolve_choice(choice) if choice is not None else None
    chosen, reset = resolve_effort(effort)
    async with sessionmaker.begin() as session:
        if chosen is not None:
            running = model_id
            if running is None and choice is None:
                stored = await get_thread_choice(
                    session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
                )
                running = stored.id if stored is not None else None
            if running is not None and chosen not in model_efforts(running):
                raise ThreadModelRefused(f"{model_label(running)} doesn't take an effort.")
        if choice is not None and model_id is None:
            await clear_thread_model(
                session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
            )
        if model_id is not None or chosen is not None or reset:
            await set_thread_model(
                session,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                model=model_id,
                effort=chosen,
                reset_effort=reset,
            )
        current = await get_thread_choice(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
        kept = await get_thread_effort(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
    shown_model = current.id if current is not None else None
    kept = _shown_effort(shown_model, kept)
    return ThreadModelChange(
        model_id=shown_model, note=_change_note(shown_model, kept), effort=kept
    )


def _shown_effort(model_id: str | None, effort: str | None) -> str | None:
    """The stored effort, only where the model it runs on takes it."""
    if effort is None or model_id is None:
        return effort
    return effort if effort in model_efforts(model_id) else None


def _change_note(model_id: str | None, effort: str | None) -> str:
    if model_id is None and effort is None:
        return "This conversation is back on the agent's own model from the next reply."
    return f"This conversation runs on {_running(model_id, effort)} from the next reply."


def _running(model_id: str | None, effort: str | None) -> str:
    name = model_label(model_id) if model_id is not None else "the agent's own model"
    return name if effort is None else f"{name} at {effort} effort"


async def describe_thread_model(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
) -> str:
    async with sessionmaker() as session:
        choice = await get_thread_choice(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
        effort = await get_thread_effort(
            session, tenant_id=tenant_id, platform=platform, thread_id=thread_id
        )
    if choice is not None and choice.provider == DEFAULT_BACKEND:
        effort = _shown_effort(choice.id, effort)
    if choice is None and effort is None:
        return "This conversation runs on the agent's own model."
    name = chosen_label(choice) if choice is not None else "the agent's own model"
    return f"This conversation runs on {name if effort is None else f'{name} at {effort} effort'}."


def chosen_label(choice: ModelRef) -> str:
    """A chosen model's name; another provider's model names its provider too."""
    if choice.provider == DEFAULT_BACKEND:
        return model_label(choice.id)
    return model_name(choice)


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


def agent_effort(agent: BetaManagedAgentsAgent) -> str | None:
    """The effort the agent's own model runs at, when it names one."""
    effort = (agent.model.model_extra or {}).get("effort")
    return effort if isinstance(effort, str) else None


def sent_effort(
    agent: BetaManagedAgentsAgent, model_override: str, chosen: str | None
) -> str | None:
    """The effort a session on `model_override` is created with: the chosen, else the agent's."""
    kept = chosen or agent_effort(agent)
    return kept if kept in model_efforts(model_override) else None


def with_model_override(
    agent: BetaManagedAgentsAgent, model_override: str | None, effort: str | None = None
) -> BetaManagedAgentsAgent:
    """The agent as a turn runs it: the stored agent with the conversation's model, if any.

    The agent's effort carries over to the picked model unless the conversation chose
    one, and is dropped where the model takes none. Speed carries over only where the
    model stays the same.
    """
    if model_override is None and effort is None:
        return agent
    model_id = model_override or agent.model.id
    speed = agent.model.speed if model_id == agent.model.id else "standard"
    kept = effort or agent_effort(agent)
    if kept is not None and kept not in model_efforts(model_id):
        kept = None
    extra = {} if kept is None else {"effort": kept}
    return agent.model_copy(
        update={"model": BetaManagedAgentsModelConfig(id=model_id, speed=speed, **extra)}
    )


@dataclass(frozen=True, slots=True)
class AppliedThreadModel:
    agent: BetaManagedAgentsAgent
    """The agent as this turn runs it: the chosen model in place of its own."""
    model_override: str | None
    """The id the session is created with, None when the agent's own model runs."""
    model_effort: str | None
    """The effort it is created with: the conversation's, else the agent's own."""
    fell_back: bool
    """A choice was kept but can't run here, so the agent's own model does."""


def _consented(model: ModelRef, provider: str | None, picked: str | None) -> bool:
    return model.provider == provider and model.id == (
        picked or backend_selection(model.provider).model
    )


async def thread_backend(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    conversation_id: str | None,
    agent_id: uuid.UUID,
    daimon_record: bool,
) -> ThreadBackend | None:
    """This conversation's choice as `resolve_turn_backend`'s thread tier, if it has one.

    A pick off the agent's provider runs only with this agent's consent for it, so a
    Claude pick moves a non-Claude agent only when one was confirmed. Without one the
    turn runs the agent's own provider and says so.
    """
    if conversation_id is None:
        return None
    choice = await get_thread_choice(
        session, tenant_id=tenant_id, platform=platform, thread_id=conversation_id
    )
    if choice is None:
        return None
    consent = await get_thread_consent(
        session,
        tenant_id=tenant_id,
        platform=platform,
        thread_id=conversation_id,
        agent_id=agent_id,
    )
    if consent is None:
        return None
    if choice.provider == DEFAULT_BACKEND:
        claude = consent.backend in (None, DEFAULT_BACKEND) and not daimon_record
        return ThreadBackend(conversation_id, None) if claude else None
    if not _consented(choice, consent.backend, consent.model):
        return None
    return ThreadBackend(conversation_id, consent)


async def apply_thread_model(
    session: AsyncSession,
    *,
    agent: BetaManagedAgentsAgent,
    tenant_id: uuid.UUID,
    platform: str,
    conversation_id: str | None,
    anthropic_backend: bool,
    thread_applied: bool = False,
) -> AppliedThreadModel:
    """The agent a turn in this conversation runs, read fresh for every turn.

    `thread_applied` says the turn already runs another provider's pick through
    `thread_backend`; such a pick is never a session override.
    """
    unchanged = AppliedThreadModel(
        agent=agent, model_override=None, model_effort=None, fell_back=False
    )
    if conversation_id is None:
        return unchanged
    choice = await get_thread_choice(
        session, tenant_id=tenant_id, platform=platform, thread_id=conversation_id
    )
    effort = await get_thread_effort(
        session, tenant_id=tenant_id, platform=platform, thread_id=conversation_id
    )
    if choice is None:
        if effort is None or not anthropic_backend or thread_applied:
            return unchanged
        # Only the effort was chosen: the agent's own model runs at it.
        applied = with_model_override(agent, None, effort)
        if agent_effort(applied) is None:
            return unchanged
        return AppliedThreadModel(
            agent=applied,
            model_override=agent.model.id,
            model_effort=effort,
            fell_back=False,
        )
    if choice.provider != DEFAULT_BACKEND:
        return AppliedThreadModel(
            agent=agent, model_override=None, model_effort=None, fell_back=not thread_applied
        )
    model_id = choice.id
    if not anthropic_backend or not is_priced_claude_model(model_id):
        return AppliedThreadModel(
            agent=agent, model_override=None, model_effort=None, fell_back=True
        )
    applied = with_model_override(agent, model_id, effort)
    return AppliedThreadModel(
        agent=applied,
        model_override=model_id,
        # Only the conversation's own pick is a session identity; the agent's
        # effort that carries over is sent (`sent_effort`) but not recorded.
        model_effort=effort if effort in model_efforts(model_id) else None,
        fell_back=False,
    )
