"""A conversation's model card: the pick, its one Confirm, and the ask-again after an agent edit.

Slack and Teams show the card (`thread_model_flow`); an agent edit that leaves the stored
consent stale posts it again (`thread_model_reask`), once, off the turn.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from daimon.core import thread_model_reask
from daimon.core.authz import Subject
from daimon.core.channel_backend import BackendActor
from daimon.core.stores.thread_models import get_thread_choice
from daimon.core.thread_model_flow import (
    ThreadConfirm,
    confirm_thread_pick,
    pick_thread_model,
    thread_options,
)
from daimon.core.thread_model_reask import (
    StaleThread,
    ThreadReask,
    drain_thread_reasks,
    spawn_thread_reask,
)
from daimon.core.thread_model_switch import ThreadPick
from daimon.core.thread_models import thread_backend

from .test_declared_drop_service import owned as owned
from .test_declared_drop_service import switching as switching

LUNA = "openai:gpt-6-luna"
MA_AGENT = "agent_helper"
CONVERSATION = "1700000000.000100"
SETTINGS = SimpleNamespace(turn=SimpleNamespace(path="mux", channel_backends=True))


def _pick(service, agent, actor, sessions) -> ThreadPick:
    return ThreadPick(service, sessions, None, agent, actor, MA_AGENT, "slack", "person")


async def _picked(pick: ThreadPick, sessions, choice: str = LUNA):
    return await pick_thread_model(
        sessionmaker=sessions,
        settings=SETTINGS,  # type: ignore[arg-type]
        pick=pick,
        tenant_id=pick.agent.tenant_id,
        platform="slack",
        conversation_id=CONVERSATION,
        choice=choice,
        admin=True,
    )


async def test_a_cross_provider_pick_shows_what_changes_then_keeps_it_on_one_confirm(switching):
    service, agent, actor, sessions, _, _ = switching
    pick = _pick(service, agent, actor, sessions)

    shown = await _picked(pick, sessions)

    assert shown.confirm is not None and shown.confirm.value == LUNA, "one Confirm is offered"
    assert shown.lines[0].startswith("Model: "), "the facts lead with the model"
    async with sessions() as db:
        assert (
            await get_thread_choice(
                db, tenant_id=agent.tenant_id, platform="slack", thread_id=CONVERSATION
            )
            is None
        ), "nothing is kept before the Confirm"

    note = await confirm_thread_pick(pick, shown.confirm, conversation_id=CONVERSATION, admin=True)

    assert note.startswith("This conversation runs on "), note
    async with sessions() as db:
        chosen = await get_thread_choice(
            db, tenant_id=agent.tenant_id, platform="slack", thread_id=CONVERSATION
        )
    assert chosen is not None and chosen.provider == "openai"


async def test_a_pick_without_opt_in_is_refused_with_the_core_words(switching):
    service, agent, _, sessions, _, _ = switching
    stranger = BackendActor(agent.tenant_id, uuid.uuid4(), Subject())
    pick = _pick(service, agent, stranger, sessions)

    shown = await _picked(pick, sessions)

    assert shown.confirm is None and shown.lines == (), "no card, no Confirm"
    assert shown.text.endswith(" Nothing changed."), shown.text
    assert "admin" in shown.text or "owner" in shown.text, "the refusal names who can help"
    async with sessions() as db:
        assert (
            await get_thread_choice(
                db, tenant_id=agent.tenant_id, platform="slack", thread_id=CONVERSATION
            )
            is None
        )


async def test_the_select_lists_the_catalog_with_prices_and_the_agent_default(switching):
    service, agent, actor, sessions, _, _ = switching

    options = await thread_options(SETTINGS, _pick(service, agent, actor, sessions))  # type: ignore[arg-type]

    values = [o.value for o in options]
    assert LUNA in values, "a provider the tenant allows is offered"
    assert options[-1].value == "default", "the agent's own model is the last entry"
    assert all("(" in o.label for o in options[:-1]), "each catalog entry shows its price"


class _Posted:
    def __init__(self) -> None:
        self.cards: list[ThreadReask] = []
        self.fail = False

    async def __call__(self, card: ThreadReask) -> None:
        if self.fail:
            raise RuntimeError("the platform is down")
        self.cards.append(card)


@pytest.fixture
async def stale(switching, monkeypatch):
    """A confirmed cross-provider pick whose agent was edited since (a new agent version)."""
    service, agent, actor, sessions, _, _ = switching
    pick = _pick(service, agent, actor, sessions)
    shown = await _picked(pick, sessions)
    assert shown.confirm is not None
    await confirm_thread_pick(pick, shown.confirm, conversation_id=CONVERSATION, admin=True)
    async with sessions() as db:
        thread = await thread_backend(
            db,
            tenant_id=agent.tenant_id,
            platform="slack",
            conversation_id=CONVERSATION,
            agent_id=agent.agent_id,
            daimon_record=False,
        )
    assert thread is not None and thread.consent is not None

    async def actor_of(**_):
        return actor, service

    monkeypatch.setattr(thread_model_reask, "thread_actor", actor_of)

    def at(version: int) -> StaleThread:
        return StaleThread(
            deps=None,
            settings=None,
            tenant_id=agent.tenant_id,
            platform="slack",
            channel_id="C1",
            thread_id=CONVERSATION,
            conversation_id=CONVERSATION,
            user_id="person",
            admin=True,
            source=agent,
            ma_agent_id=MA_AGENT,
            agent_version=version,
            consent=thread.consent,
        )

    return SimpleNamespace(at=at, sessions=sessions, pick=pick)


async def test_the_card_is_posted_once_after_an_agent_edit(stale):
    posted = _Posted()

    for _message in range(3):
        spawn_thread_reask(sessionmaker=stale.sessions, notifier=posted, stale=stale.at(2))
        await drain_thread_reasks()

    assert len(posted.cards) == 1, "later messages in the same state stay quiet"
    card = posted.cards[0]
    assert card.conversation_id == CONVERSATION and card.thread_id == CONVERSATION
    assert card.confirm is not None and card.confirm.value == LUNA
    assert card.lines and card.lines[0].startswith("Model: ")

    spawn_thread_reask(sessionmaker=stale.sessions, notifier=posted, stale=stale.at(3))
    await drain_thread_reasks()
    assert len(posted.cards) == 2, "another edit is asked about again"


async def test_the_card_confirms_the_pick_against_the_edited_agent(stale):
    posted = _Posted()
    spawn_thread_reask(sessionmaker=stale.sessions, notifier=posted, stale=stale.at(2))
    await drain_thread_reasks()
    confirm = posted.cards[0].confirm
    assert isinstance(confirm, ThreadConfirm)

    note = await confirm_thread_pick(stale.pick, confirm, conversation_id=CONVERSATION, admin=True)

    assert note.startswith("This conversation runs on "), note


async def test_a_failed_post_is_never_raised_and_the_next_message_tries_again(stale):
    posted = _Posted()
    posted.fail = True

    spawn_thread_reask(sessionmaker=stale.sessions, notifier=posted, stale=stale.at(2))
    await drain_thread_reasks()
    assert posted.cards == [], "a failed post is not an error for the turn"

    posted.fail = False
    spawn_thread_reask(sessionmaker=stale.sessions, notifier=posted, stale=stale.at(2))
    await drain_thread_reasks()
    assert len(posted.cards) == 1, "it was not recorded as asked"


async def test_a_slow_second_message_does_not_post_twice_while_one_is_in_flight(stale):
    release = asyncio.Event()
    cards: list[ThreadReask] = []

    async def slow(card: ThreadReask) -> None:
        await release.wait()
        cards.append(card)

    for _message in range(2):
        spawn_thread_reask(sessionmaker=stale.sessions, notifier=slow, stale=stale.at(2))
    release.set()
    await drain_thread_reasks()

    assert len(cards) == 1
