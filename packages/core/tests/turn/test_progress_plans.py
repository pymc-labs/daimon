"""Progress plans for one turn: the guidance block and when it is sent."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.progress_plans import (
    GUIDANCE,
    guidance_block,
    plan_thread_scope_id,
    progress_plans_prefix,
    progress_plans_shown,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_guidance_asks_for_results_and_next_steps_never_reasoning() -> None:
    """The block must not invite the model to put its reasoning on the card or in a tool."""
    text = GUIDANCE.lower()
    assert "update_plan" in text, "the guidance names the tool"
    assert "never put your reasoning" in text, "the guidance keeps deliberation private"
    for word in ("explain", "explanation", "thinking", "thought process", "why you"):
        assert word not in text, f"the guidance must not ask for {word!r}"
    assert guidance_block().startswith('<progress_plans state="on">'), "a tagged block"


def test_a_slack_thread_is_keyed_by_its_channel() -> None:
    """A Slack timestamp is unique only within its channel."""
    assert plan_thread_scope_id("slack", "C1", "1.2") == "C1:1.2", "Slack prefixes the channel"
    assert plan_thread_scope_id("slack", "C1", "C1:1.2") == "C1:1.2", "an id already keyed stays"
    assert plan_thread_scope_id("discord", "chan", "thr") == "thr", "other platforms are unchanged"


def _plans(value: str) -> ResolvedFeature:
    return ResolvedFeature("progress_plans", value, "thread")


async def test_the_block_is_sent_and_the_card_told_only_when_on(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An On feature gets the block; the card learns it through the observation."""
    observation = TurnObservation(db_session_factory, uuid.uuid4(), "discord", "chan-1", "thr-1")
    with observation.activate():
        off = progress_plans_prefix(_plans("off"), platform="discord", chat_rendered=True)
        assert (off, progress_plans_shown()) == ("", False), "an Off feature sends nothing"
        on = progress_plans_prefix(_plans("on"), platform="discord", chat_rendered=True)
        assert on == guidance_block(), "an On feature gets the block"
        assert progress_plans_shown(), "the card draws the plan on this turn"


@pytest.mark.parametrize(
    ("platform", "chat_rendered"), [("discord", False), ("cli", True), ("mcp", True)]
)
def test_no_block_where_no_card_is_drawn(platform: str, *, chat_rendered: bool) -> None:
    """A turn not drawn on a chat card gets no block even when On."""
    assert progress_plans_prefix(_plans("on"), platform=platform, chat_rendered=chat_rendered) == ""


def test_an_unresolved_feature_sends_nothing() -> None:
    """A hand-built admission resolves nothing, which is Off."""
    assert progress_plans_prefix(None, platform="slack", chat_rendered=True) == ""
