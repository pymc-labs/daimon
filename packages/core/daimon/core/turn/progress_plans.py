"""Progress plans for one turn: whether they are on, and the guidance the model gets.

The `update_plan` tool and the agent's system prompt are the same on every
turn, so a prompt cache survives a switch flipped mid-thread. What changes is
a short block in front of this turn's user message, sent only when the scope
cascade (`daimon.core.scope_cascade`) resolves on here; the card draws the
plan only then (`TurnObservation.progress_plans`).
"""

from __future__ import annotations

import uuid
from collections.abc import Container

from daimon.core.scope_cascade import (
    CascadeMode,
    CascadeScope,
    ResolvedCascade,
    resolve_modes,
    scope_label,
    tier_word,
)
from daimon.core.stores.domain import CHAT_PLATFORMS
from daimon.core.stores.progress_plans import get_progress_plan_modes
from daimon.core.turn.outcomes import current_outcome
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Status lines are results and next steps, which the agent's always-on prompt
# lets it say between tool calls; its deliberation stays private. A provider
# refuses requests that ask a model to put its reasoning into tool inputs
# (its documented `reasoning_extraction` refusal), so neither this text nor the
# tool's schema asks for any.
GUIDANCE = (
    "Progress plans are on here. For a task that needs three or more steps, call "
    "update_plan before you start, with 2-5 short steps written for the person and "
    "exactly one in progress, and call it again as steps finish. Skip it for quick "
    "questions and single lookups, and never make a one-step plan. Updating the plan "
    "is not a reply: keep working until the task is done. You may also write one "
    "short line, about 12 words, saying what you found and what you will do next, "
    "in the same message as your next tool call. The plan and these lines state "
    "results and next steps only; never put your reasoning in them."
)


def guidance_block() -> str:
    """The block sent in front of the user message on a turn with progress plans on."""
    return f'<progress_plans state="on">\n{GUIDANCE}\n</progress_plans>\n\n'


def plan_thread_scope_id(platform: str, channel_id: str | None, thread_id: str) -> str:
    """The thread's id as the scope rows key it.

    A Slack thread timestamp is unique only within its channel, so its rows
    use ``<channel>:<thread ts>``, the form Daimon's Slack tools take.
    """
    if platform == "slack" and channel_id and ":" not in thread_id:
        return f"{channel_id}:{thread_id}"
    return thread_id


async def resolve_progress_plans(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    deployment: CascadeMode,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str,
) -> ResolvedCascade:
    """Walk the cascade for this thread. One read covers all three tiers."""
    if deployment is CascadeMode.DISABLED:
        return ResolvedCascade(deployment, "deployment")
    async with sessionmaker() as session:
        modes = await get_progress_plan_modes(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=plan_thread_scope_id(platform, channel_id, thread_id),
        )
    return resolve_modes(deployment, modes)


async def progress_plans_prefix(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    deployment: CascadeMode,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    thread_id: str,
    chat_rendered: bool,
) -> str:
    """The guidance block when progress plans are on for this turn, else ``""``.

    Only a turn drawn on a chat card for a person can show a plan. Marks the
    active turn observation so the card knows to draw it.
    """
    chat_platforms: Container[str] = CHAT_PLATFORMS
    if not chat_rendered or platform not in chat_platforms:
        return ""
    resolved = await resolve_progress_plans(
        sessionmaker,
        deployment=deployment,
        tenant_id=tenant_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
    )
    if resolved.mode is not CascadeMode.ON:
        return ""
    if (observation := current_outcome.get()) is not None:
        observation.progress_plans = True
    return guidance_block()


def progress_plans_shown() -> bool:
    """Whether the running turn's card draws the agent's plan."""
    observation = current_outcome.get()
    return observation is not None and observation.progress_plans


# --- Notes returned by the progress-plans tools ---------------------------------


def build_progress_plans_note(
    *,
    scope: CascadeScope,
    scope_id: str | None,
    requested: CascadeMode | None,
    effective: ResolvedCascade,
    platform: str | None = None,
) -> str:
    """One sentence stating what was written and what is now in effect at that scope.

    Returned by the tool rather than recalled from a prompt, so a thread turned
    `on` under a `disabled` channel is reported as still off.
    """
    where = scope_label(scope, scope_id, platform)
    wrote = (
        f"{where} now inherits its setting"
        if requested is None
        else f"{where} is set to {requested.value}"
    )
    if effective.mode is CascadeMode.ON:
        return f"{wrote}; for longer tasks there I will keep a short plan on the progress card."
    if effective.mode is CascadeMode.DISABLED:
        tier = tier_word(effective.tier, platform)
        return (
            f"{wrote}, but progress plans are disabled at the {tier} level, "
            "so none is shown and this cannot be turned on below that level."
        )
    return f"{wrote}; I will not show a plan there."


def build_progress_plans_explanation(
    effective: ResolvedCascade, *, thread_id: str | None, platform: str | None = None
) -> str:
    """One sentence naming the winning tier, so "why is there (no) plan here" is answerable."""
    tier = tier_word(effective.tier, platform)
    where = f"in thread {thread_id}" if thread_id is not None else "here"
    if effective.mode is CascadeMode.ON:
        return f"Progress plans are on {where}: the {tier} setting is on."
    if effective.mode is CascadeMode.DISABLED:
        return (
            f"Progress plans are off {where}: the {tier} setting is disabled, "
            "and no narrower setting can turn them on."
        )
    return (
        f"Progress plans are off {where}: the {tier} setting is the narrowest "
        "one with a value, and it is off."
    )
