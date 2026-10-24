"""Progress plans for one turn: the guidance block and when it is sent."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.scope_cascade import CascadeMode, CascadeScope
from daimon.core.stores.progress_plans import set_progress_plan_mode
from daimon.core.turn.outcomes import TurnObservation
from daimon.core.turn.progress_plans import (
    GUIDANCE,
    guidance_block,
    plan_thread_scope_id,
    progress_plans_prefix,
    progress_plans_shown,
)
from daimon.testing.factories import make_tenant
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


def _observation(sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> TurnObservation:
    return TurnObservation(sm, tenant_id, "discord", "chan-1", "thr-1")


async def test_the_block_is_sent_and_the_card_told_only_when_on(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A thread turned on gets the block; the card learns it through the observation."""
    tenant = await make_tenant(db_session)
    await set_progress_plan_mode(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        scope=CascadeScope.THREAD,
        scope_id="thr-1",
        mode=CascadeMode.ON,
    )
    args = {
        "deployment": CascadeMode.OFF,
        "tenant_id": tenant.id,
        "platform": "discord",
        "channel_id": "chan-1",
    }

    observation = _observation(db_session_factory, tenant.id)
    with observation.activate():
        off = await progress_plans_prefix(
            db_session_factory, **args, thread_id="thr-2", chat_rendered=True
        )
        assert (off, progress_plans_shown()) == ("", False), "an unset thread inherits off"
        on = await progress_plans_prefix(
            db_session_factory, **args, thread_id="thr-1", chat_rendered=True
        )
        assert on == guidance_block(), "a thread turned on gets the block"
        assert progress_plans_shown(), "the card draws the plan on this turn"


@pytest.mark.parametrize(
    ("platform", "chat_rendered"), [("discord", False), ("cli", True), ("mcp", True)]
)
async def test_no_block_where_no_card_is_drawn(platform: str, *, chat_rendered: bool) -> None:
    """A turn not drawn on a chat card never reads the switch."""
    sm: async_sessionmaker[AsyncSession] = async_sessionmaker()
    prefix = await progress_plans_prefix(
        sm,
        deployment=CascadeMode.ON,
        tenant_id=uuid.uuid4(),
        platform=platform,
        channel_id="c",
        thread_id="t",
        chat_rendered=chat_rendered,
    )
    assert prefix == "", "nothing is sent and the unbound sessionmaker is never used"


async def test_a_disabled_deployment_never_reads_the_store() -> None:
    """The operator's `disabled` is final, so no query is spent on it."""
    prefix = await progress_plans_prefix(
        async_sessionmaker(),
        deployment=CascadeMode.DISABLED,
        tenant_id=uuid.uuid4(),
        platform="slack",
        channel_id="C1",
        thread_id="1.2",
        chat_rendered=True,
    )
    assert prefix == "", "disabled sends nothing"
