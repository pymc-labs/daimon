"""Workflow cards in chat: the draft card's Publish, a confirmed run and the card that follows it."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from daimon.core._models import PlatformPrincipal
from daimon.core.posted_controls import PostedCard, card_text
from daimon.core.stores import workflow_runs
from daimon.core.workflows import chat_cards, service
from daimon.core.workflows.access import Who
from daimon.core.workflows.chat_cards import ChatPlace, Presser
from daimon.core.workflows.service import Env
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy.ext.asyncio import AsyncSession

NODES: dict[str, Any] = {
    "think": {
        "kind": "agent",
        "agent": "default",
        "title": "Read the deals",
        "instructions": "x",
        "next": "done",
    },
    "done": {"kind": "finish"},
}
PLACE = ChatPlace("discord", "100", "200")


async def _world(db_session: AsyncSession) -> tuple[World, Presser]:
    world = await make_world(db_session, NODES, "think")
    db_session.add(
        PlatformPrincipal(
            tenant_id=world.tenant_id, platform="discord", external_id="u1", account_id=world.owner
        )
    )
    await db_session.commit()
    return world, Presser(world.tenant_id, "discord", "u1")


def _ids(card: PostedCard) -> list[str]:
    return [b.custom_id or b.label for b in card.buttons]


async def test_the_draft_card_publishes_its_own_revision_only(db_session: AsyncSession) -> None:
    world, presser = await _world(db_session)
    owner = Env(world.sm, world.settings, Who(world.tenant_id, world.owner), NOW)
    draft = await service.get_draft(owner, world.workflow_id)
    spec = draft.spec | {"name": "Deals"}
    saved = await service.save_draft(
        owner, world.workflow_id, base_revision=draft.revision, spec=spec
    )
    card = chat_cards.draft_card(world.workflow_id, saved, None)
    assert card.headline == "📝 Deals"
    assert card.facts == ("Starts by hand", "1. Read the deals", "2. Done"), card.facts
    old = chat_cards._id(world.workflow_id, "publish", f"{draft.revision:x}")  # pyright: ignore[reportPrivateUsage]
    stale = await chat_cards.press(
        world.sm, world.settings, presser=presser, custom_id=old, place=PLACE
    )
    assert stale.say == chat_cards.STALE, "an older card cannot publish a newer draft"
    publish = _ids(card)[-1]
    done = await chat_cards.press(
        world.sm, world.settings, presser=presser, custom_id=publish, place=PLACE
    )
    assert done.card is not None and done.card.headline == "✅ Deals: version 2 is live"
    stranger = Presser(world.tenant_id, "discord", "u2")
    refused = await chat_cards.press(
        world.sm, world.settings, presser=stranger, custom_id=publish, place=PLACE
    )
    assert refused.say and refused.card is None, "a stranger's press is the service's refusal"


async def test_a_confirmed_run_starts_on_its_card_and_the_card_follows_it(
    db_session: AsyncSession,
) -> None:
    world, presser = await _world(db_session)
    now = datetime.now(UTC)  # presses run on the real clock
    card = chat_cards.confirm_card(world.workflow_id, "T", 1, {}, None)
    go = _ids(card)[0]
    pressed = await chat_cards.press(
        world.sm,
        world.settings,
        presser=presser,
        custom_id=go,
        place=PLACE,
        card_text=card_text(card),
    )
    assert pressed.card is not None and pressed.card.headline == "🔄 T: Run 1 running", pressed
    cancel = _ids(pressed.card)[-1]
    async with world.sm() as session:
        (run,) = await workflow_runs.carded_runs(
            session, platform="discord", ended_after=now, started_after=NOW - timedelta(days=1)
        )
    assert run.triggered_by["card"] == PLACE.ref(), "the run names the card it shows on"
    edits: list[tuple[ChatPlace, str]] = []

    async def edit(place: ChatPlace, shown: PostedCard) -> None:
        edits.append((place, shown.headline))

    seen: dict[uuid.UUID, tuple[str, datetime]] = {}

    async def follow(at: datetime) -> int:
        return await chat_cards.follow_cards(
            world.sm, world.settings, platform="discord", edit=edit, seen=seen, now=at
        )

    assert await follow(now) == 1
    stopped = await chat_cards.press(
        world.sm, world.settings, presser=presser, custom_id=cancel, place=PLACE
    )
    assert stopped.card is not None and "stopped" in stopped.card.headline
    soon = now + timedelta(seconds=1)
    assert await follow(soon) == 0
    later = now + chat_cards.EDIT_EVERY
    assert await follow(later) == 1
    assert edits[-1] == (PLACE, "⏹️ T: Run 1 stopped"), edits


async def test_a_confirmation_card_starts_only_the_inputs_it_shows(
    db_session: AsyncSession,
) -> None:
    world, presser = await _world(db_session)
    card = chat_cards.confirm_card(world.workflow_id, "T", 1, {"x": "1"}, None)
    tampered = card_text(card).replace("x: ", "y: ")
    pressed = await chat_cards.press(
        world.sm,
        world.settings,
        presser=presser,
        custom_id=_ids(card)[0],
        place=PLACE,
        card_text=tampered,
    )
    assert pressed.say == chat_cards.CHANGED, "inputs that read back otherwise start nothing"


def test_inputs_read_back_from_the_card_as_shown() -> None:
    inputs = {"email": "jane@example.com", "n": 3, "flag": True, "s": "42", "note": "a\nb"}
    card = chat_cards.confirm_card(uuid.uuid4(), "T", 1, inputs, None)
    drawn = "\n\n".join(f"-# {fact}" for fact in card.facts).replace("&", "&amp;")
    assert chat_cards.confirmed_inputs(drawn, frozenset(inputs)) == inputs


def test_a_run_form_types_what_was_typed() -> None:
    spec = {
        "name": "Lead",
        "inputs": {
            "type": "object",
            "required": ["email"],
            "properties": {"email": {"type": "string"}, "n": {"type": "integer"}},
        },
    }
    form = chat_cards.run_form(uuid.uuid4(), spec, limit=5)
    assert form is not None and [f.key for f in form.fields] == ["email", "n"]
    assert chat_cards.form_inputs(form, {"n": "2"}) == "Fill in email."
    assert chat_cards.form_inputs(form, {"email": "a@b.c", "n": "2"}) == {"email": "a@b.c", "n": 2}
    assert chat_cards.run_form(uuid.uuid4(), spec, limit=1) is None, "too many fields: the web"
