"""Workflow cards in chat: the draft card's Publish, a confirmed run and the card that follows it."""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from daimon.core._models import PlatformPrincipal
from daimon.core.config import WebSettings
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.posted_controls import PostedCard, card_text
from daimon.core.stores import workflow_runs, workflows
from daimon.core.web_links import WebAt, WebLinks
from daimon.core.workflows import chat_cards, convert, gate, service
from daimon.core.workflows.access import Who
from daimon.core.workflows.catalog import tenant_agents, tenant_skills
from daimon.core.workflows.chat_cards import ChatPlace, Presser
from daimon.core.workflows.service import Env
from daimon.core.workflows.views import Grant
from daimon.testing.builders import skill_listed
from daimon.testing.workflows import NOW, World, ma_agents_with, make_world
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
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
PLACE = ChatPlace("discord", "100", "200", "300")


async def NO_LINKS(*_: object) -> None:  # web_awareness Off everywhere
    return None


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


async def test_the_draft_card_shows_the_limits_an_admin_approves(db_session: AsyncSession) -> None:
    world, _ = await _world(db_session)
    env = Env(world.sm, world.settings, Who(world.tenant_id, world.owner), NOW)
    draft = await service.get_draft(env, world.workflow_id)
    limits = "mail/send_reply: to is an address at example.com. Any text in body."
    grants = [Grant(kind="scoped_write", node="think", detail=d) for d in (limits, "x" * 400)]
    card = chat_cards.draft_card(
        world.workflow_id, draft.model_copy(update={"new_grants": grants}), None
    )
    assert f"Writes only within: {limits}" in card.facts
    assert any(f.startswith("Writes only within: xx") and f.endswith("…") for f in card.facts)
    assert "An admin approves before it goes live: writes only to set places." in card.facts


async def test_the_draft_card_publishes_its_own_revision_only(db_session: AsyncSession) -> None:
    world, presser = await _world(db_session)
    listed = MARouter()  # a card press lists the workspace's agents and skills itself
    listed.add_agent_list(*ma_agents_with({"default": []}, world.tenant_id))
    title = tenant_scoped_display_title(tenant_id=world.tenant_id, name="deck")
    skill = skill_listed(id="skill_deck", display_title=title).model_dump(mode="json")
    listed.add("GET", r"/v1/skills", lambda _r, _m: list_response([skill]))
    client = build_fake_anthropic(listed.dispatch)
    who = Who(world.tenant_id, world.owner)
    agents, skills = tenant_agents(client, world.tenant_id), tenant_skills(client, world.tenant_id)
    owner = Env(world.sm, world.settings, who, NOW, agents, skills)
    draft = await service.get_draft(owner, world.workflow_id)
    think = draft.spec["nodes"]["think"] | {"skills": ["deck"]}
    spec = draft.spec | {"name": "Deals", "nodes": draft.spec["nodes"] | {"think": think}}
    saved = await service.save_draft(
        owner, world.workflow_id, base_revision=draft.revision, spec=spec
    )
    card = chat_cards.draft_card(world.workflow_id, saved, None)
    assert card.headline == "📝 Deals"
    assert card.facts == ("Starts by hand", "1. Read the deals", "2. Done"), card.facts
    old = chat_cards._id(world.workflow_id, "publish", f"{draft.revision:x}")  # pyright: ignore[reportPrivateUsage]
    stale = await chat_cards.press(
        world.sm, world.settings, presser=presser, custom_id=old, place=PLACE, links=NO_LINKS
    )
    assert stale.say == chat_cards.STALE, "an older card cannot publish a newer draft"
    publish = _ids(card)[-1]
    done = await chat_cards.press(
        world.sm,
        world.settings,
        presser=presser,
        custom_id=publish,
        place=PLACE,
        links=NO_LINKS,
        client=client,
    )
    assert done.card is not None and done.card.headline == "✅ Deals: version 2 is live"
    async with world.sm() as session:
        live = await workflows.get_version(
            session, tenant_id=world.tenant_id, workflow_id=world.workflow_id, number=2
        )
    assert live is not None and live.skills == {"deck": {"id": "skill_deck", "version": "1"}}
    stranger = Presser(world.tenant_id, "discord", "u2")
    refused = await chat_cards.press(
        world.sm, world.settings, presser=stranger, custom_id=publish, place=PLACE, links=NO_LINKS
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
        links=NO_LINKS,
        card_text=card_text(card),
    )
    assert pressed.card is not None and pressed.card.headline == "🔄 T: Run 1 running", pressed
    cancel = _ids(pressed.card)[-1]
    async with world.sm() as session:
        (run,) = await workflow_runs.carded_runs(
            session, platform="discord", ended_after=now, started_after=NOW - timedelta(days=1)
        )
    assert run.triggered_by["card"] == PLACE.ref(), "the run names the card it shows on"
    assert run.origin == PLACE.ref(), "a run started on a card posts back to it as `origin`"
    edits: list[tuple[ChatPlace, str]] = []

    async def edit(_: uuid.UUID, place: ChatPlace, shown: PostedCard) -> None:
        edits.append((place, shown.headline))

    seen: dict[uuid.UUID, tuple[str, datetime]] = {}

    async def follow(at: datetime) -> int:
        return await chat_cards.follow_cards(
            world.sm, platform="discord", edit=edit, links=NO_LINKS, seen=seen, now=at
        )

    assert await follow(now) == 1
    stopped = await chat_cards.press(
        world.sm, world.settings, presser=presser, custom_id=cancel, place=PLACE, links=NO_LINKS
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
        links=NO_LINKS,
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


async def test_every_chat_entry_refuses_while_workflows_are_off(db_session: AsyncSession) -> None:
    world, presser = await _world(db_session)
    off = world.settings.model_copy(update={"features": {"workflows": "off"}})
    entries = {
        name
        for name, fn in inspect.getmembers(chat_cards, inspect.iscoroutinefunction)
        if "presser" in inspect.signature(fn).parameters and name != "env_of"
    }
    assert entries == {
        "press",
        "submit_form",
        "open_pressed",
        "list_for",
        "pick",
        "move_routine",
    }, entries
    wid = world.workflow_id

    async def never(*_: object) -> Any:
        raise AssertionError("nothing is posted while workflows are off")

    run = chat_cards._id(wid, "run")  # pyright: ignore[reportPrivateUsage]
    pressed = await chat_cards.press(
        world.sm, off, presser=presser, custom_id=run, place=PLACE, links=NO_LINKS
    )
    form = f"wff:{wid}:l"
    said = [
        pressed.say,
        await chat_cards.submit_form(world.sm, off, presser=presser, form_id=form, raw={}),
        await chat_cards.open_pressed(
            world.sm, off, presser, wid, test=False, links=NO_LINKS, post=never, edit=never
        ),
        await chat_cards.list_for(world.sm, off, presser),
        card_text(await chat_cards.pick(world.sm, off, presser, str(wid), NO_LINKS)),
    ]
    assert all(gate.OFF in str(s) for s in said), said
    moved = await chat_cards.move_routine(world.sm, off, presser, uuid.uuid4())
    assert moved is not None and convert.ADMINS in moved, "a person who is not an admin is refused"


async def test_a_card_links_the_web_only_where_web_awareness_is_on_at_its_place(
    db_session: AsyncSession,
) -> None:
    world, presser = await _world(db_session)
    presser = Presser(world.tenant_id, "discord", "u1", channel_id="100", thread_id="300")
    asked: list[tuple[str | None, str | None]] = []
    on = WebLinks.from_settings(WebSettings(public_url="https://app.example.com"))  # type: ignore[call-arg]
    assert on is not None

    def at(links: bool) -> WebAt:
        async def gate(tenant: uuid.UUID, channel: str | None, thread: str | None) -> Any:
            asked.append((channel, thread))
            return on.for_tenant(tenant, admin=False) if links else None

        return gate

    pause = chat_cards._id(world.workflow_id, "pause")  # pyright: ignore[reportPrivateUsage]
    for links, shown in ((True, True), (False, False)):
        pressed = await chat_cards.press(
            world.sm, world.settings, presser=presser, custom_id=pause, place=PLACE, links=at(links)
        )
        assert pressed.card is not None, pressed
        assert ("Open in web" in _ids(pressed.card)) is shown, (links, _ids(pressed.card))
    assert asked == [("100", "300")] * 2, "the press asks at the channel and thread it came from"
    picked = await chat_cards.pick(
        world.sm, world.settings, presser, str(world.workflow_id), at(False)
    )
    assert "Open in web" not in _ids(picked), "the panel's card follows the same gate"
