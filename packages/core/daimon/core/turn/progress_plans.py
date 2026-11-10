"""Progress plans for one turn: whether they are on, and the guidance the model gets.

The `update_plan` tool and the agent's system prompt are the same on every
turn, so a prompt cache survives a switch flipped mid-thread. What changes is
a short block in front of this turn's user message, sent only when the
`progress_plans` feature resolves on at the turn's place; the card draws the
plan only then (`TurnObservation.progress_plans`).
"""

from __future__ import annotations

from collections.abc import Container

from daimon.core.features.resolver import ResolvedFeature
from daimon.core.stores.domain import CHAT_PLATFORMS
from daimon.core.turn.outcomes import current_outcome

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


def progress_plans_prefix(
    resolved: ResolvedFeature | None, *, platform: str, chat_rendered: bool
) -> str:
    """The guidance block when the `progress_plans` feature is on for this turn, else ``""``.

    Only a turn drawn on a chat card for a person can show a plan; a hand-built
    admission (`resolved` None) shows none. Marks the active turn observation so the
    card knows to draw it.
    """
    chat_platforms: Container[str] = CHAT_PLATFORMS
    if not chat_rendered or platform not in chat_platforms or resolved is None or not resolved.on:
        return ""
    if (observation := current_outcome.get()) is not None:
        observation.progress_plans = True
    return guidance_block()


def progress_plans_shown() -> bool:
    """Whether the running turn's card draws the agent's plan."""
    observation = current_outcome.get()
    return observation is not None and observation.progress_plans
