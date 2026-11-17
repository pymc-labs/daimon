"""Teams personal chats route by DM grants, rules and picks, and fail closed on unread rosters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from time import time
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.teams import personal_chat
from daimon.adapters.teams.card_actions import Actor
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.personal_chat import (
    UNCHECKED,
    VERB,
    TeamsDmPick,
    route_personal_chat,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core import dm_debounce
from daimon.core.authz import AgentRef
from daimon.core.direct_messages import DM_AGENT_REFUSED, dm_permission_text
from daimon.core.dm_access import DmAgent
from daimon.core.dm_debounce import PICK_EXPIRED, DmBurst
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import dm_access as dm_store
from daimon.core.stores.direct_messages import set_dm_agent_name, set_dm_enabled
from daimon.core.stores.thread_agent_bindings import get_binding
from microsoft_teams.api import MessageActivityInput
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    build_teams_runtime,
    make_inbound,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def routed(
    db_session_factory: async_sessionmaker[AsyncSession], provisioned_tenant: None
) -> None:
    """A rule, so DMs route; an unrouted workspace answers with the chat's own agent."""
    async with db_session_factory() as session, session.begin():
        await dm_store.add_rule(
            session,
            tenant_id=TENANT,
            subject="everyone",
            subject_id=None,
            agent_name=None,
            locked=False,
            set_by=None,
            now=_NOW,
        )


def _agents(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    async def listed(*_: object, **__: object) -> list[DmAgent]:
        return [DmAgent(n, f"ag_{n}", AgentRef.of(n)) for n in names]

    monkeypatch.setattr(personal_chat, "list_dm_agents", listed)


async def _route(
    db: async_sessionmaker[AsyncSession],
    inbound: TeamsInbound | None = None,
    *,
    runtime: TeamsRuntime | None = None,
    burst: DmBurst[TeamsInbound] | None = None,
    marks: list[tuple[str, bool]] | None = None,
) -> tuple[bool, list[MessageActivityInput]]:
    posted: list[MessageActivityInput] = []

    async def post(card: MessageActivityInput) -> None:
        posted.append(card)

    async def mark(item: TeamsInbound, on: bool) -> None:
        (marks if marks is not None else []).append((item.text, on))

    inbound = inbound or make_inbound()
    answered = await route_personal_chat(
        runtime or build_teams_runtime(db),
        inbound,
        tenant_id=TENANT,
        post=post,
        burst=burst or DmBurst(inbound, lambda item: item.text),
        mark=mark,
    )
    return answered, posted


async def _bound(db: async_sessionmaker[AsyncSession]) -> str | None:
    async with db() as session:
        row = await get_binding(
            session,
            tenant_id=TENANT,
            platform="teams",
            parent_channel_id=CONVERSATION_ID,
            thread_id=CONVERSATION_ID,
        )
    return None if row is None else row.responder_name


def _card(posted: list[MessageActivityInput]) -> dict[str, Any]:
    assert len(posted) == 1, "exactly one card"
    attachments = posted[0].attachments or []
    content: Any = attachments[0].content
    return content.model_dump(by_alias=True, exclude_none=True)


@pytest.mark.usefixtures("routed")
async def test_route_personal_chat_when_one_agent_then_pins_it_and_answers(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "helper")
    answered, posted = await _route(db_session_factory)
    assert answered and not posted, "one agent answers at once"
    assert await _bound(db_session_factory) == "helper", "the chat is pinned to it"


async def test_route_personal_chat_when_unrouted_then_the_chats_own_agent_answers(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No rule or narrower grant: no card, no pin, as before DM routing; a default answers."""
    _agents(monkeypatch, "alpha", "beta")
    answered, posted = await _route(db_session_factory)
    assert answered and not posted, "the chat's own agent answers"
    assert await _bound(db_session_factory) is None, "nothing pinned"
    async with db_session_factory() as session, session.begin():
        await set_dm_agent_name(session, tenant_id=TENANT, agent_name="beta")
    assert (await _route(db_session_factory))[0], "the default answers"
    assert await _bound(db_session_factory) == "beta", "and the chat is pinned to it"


@pytest.mark.usefixtures("routed")
async def test_route_personal_chat_when_bound_or_setup_then_answers_unrouted(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "helper")
    await _route(db_session_factory)

    async def never(*_: object, **__: object) -> list[DmAgent]:
        raise AssertionError("a pinned chat lists no agents")

    monkeypatch.setattr(personal_chat, "list_dm_agents", never)
    assert (await _route(db_session_factory))[0], "a pinned chat answers, rechecked on admission"
    setup = make_inbound(setup_thread_id="setup-1")
    assert (await _route(db_session_factory, setup))[0], "a setup chat is not routed"


@pytest.mark.usefixtures("routed")
async def test_route_personal_chat_when_several_agents_then_holds_under_a_button_each(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card is up and the burst waits for the pick; nothing answers until then."""
    _agents(monkeypatch, "alpha", "beta")
    runtime = build_teams_runtime(db_session_factory)
    routing, burst, marks = await _held(db_session_factory, runtime, make_inbound("hello"))
    await asyncio.sleep(0.05)
    assert not routing.done() and marks == [("hello", True)], "the message waits, marked"
    burst.stop()
    answered, posted = await asyncio.wait_for(routing, 5)
    assert not answered, "unpicked, nothing answers"
    body = _card(posted).get("body", [])
    actions = [a for block in body if block["type"] == "ActionSet" for a in block["actions"]]
    assert [(a["title"], a["verb"], a["data"]["agent"]) for a in actions] == [
        ("alpha", VERB, "alpha"),
        ("beta", VERB, "beta"),
    ], "one button per agent"
    assert all(a["data"]["until"] > time() for a in actions), "each button carries its expiry"
    assert await _bound(db_session_factory) is None, "no agent pinned before the pick"


async def test_route_personal_chat_when_dms_off_then_refuses_with_who_can_help(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "helper")
    async with db_session_factory() as session, session.begin():
        await set_dm_enabled(session, tenant_id=TENANT, enabled=False)
    answered, posted = await _route(db_session_factory)
    assert not answered, "DMs off answers nobody"
    first = dm_permission_text("teams").split("\n", 1)[0]
    assert first in str(_card(posted)), "the refusal names who can help"


async def test_route_personal_chat_when_team_roster_unreadable_then_fails_closed(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "helper")
    async with db_session_factory() as session, session.begin():
        await dm_store.set_grant(
            session,
            tenant_id=TENANT,
            agent_name="helper",
            who="listed",
            user_ids=[],
            group_ids=["team-1"],
            set_by=None,
            now=_NOW,
        )
    answered, posted = await _route(db_session_factory)  # No Graph is wired in tests.
    assert not answered, "an unread roster admits nobody"
    assert UNCHECKED.split("\n", 1)[0] in str(_card(posted)), "it says access could not be checked"
    assert await _bound(db_session_factory) is None, "nothing pinned"


def _click(agent: str, until: int) -> Any:
    activity = SimpleNamespace(
        value=SimpleNamespace(action=SimpleNamespace(data={"agent": agent, "until": until})),
        conversation=SimpleNamespace(id=CONVERSATION_ID, conversation_type="personal"),
        reply_to_id="1700000000000",
        id="click-1",
        service_url="https://smba.example.com",
        recipient=SimpleNamespace(name="Daimon"),
        from_=SimpleNamespace(name="Pat", id=f"29:{AAD_OBJECT_ID}"),
    )
    return SimpleNamespace(activity=activity)


async def _pick(
    runtime: TeamsRuntime,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    *,
    wake: list[str],
    refuse: Callable[[str], None] = lambda _: None,
    until: int = 9_999_999_999,
) -> Any:
    async def actor(*_: object, **__: object) -> Actor:
        return Actor(AAD_OBJECT_ID, TENANT, False, CONVERSATION_ID)

    monkeypatch.setattr(personal_chat, "card_actor", actor)
    picker = TeamsDmPick(runtime, wake=wake.append, refuse=refuse)
    return await picker._on_action(_click(agent, until))  # pyright: ignore[reportPrivateUsage]


async def _held(
    db: async_sessionmaker[AsyncSession], runtime: TeamsRuntime, first: TeamsInbound
) -> tuple[asyncio.Task[tuple[bool, list[MessageActivityInput]]], DmBurst[TeamsInbound], list[Any]]:
    """Route `first` with a burst, until the card is up and the burst holds."""
    burst, marks = DmBurst(first, lambda item: item.text), list[tuple[str, bool]]()
    routing = asyncio.create_task(_route(db, first, runtime=runtime, burst=burst, marks=marks))
    while not marks:
        await asyncio.sleep(0.01)
    return routing, burst, marks


@pytest.mark.usefixtures("routed")
@pytest.mark.parametrize("here", [True, False], ids=["this process", "another process"])
async def test_dm_pick_when_allowed_then_pins_and_answers_every_held_message(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    here: bool,
) -> None:
    """As on Discord and Slack: one card, 👀 while held, one answer for the burst."""
    _agents(monkeypatch, "alpha", "beta")
    monkeypatch.setattr(dm_debounce, "RECHECK_S", 0.05)
    runtime = build_teams_runtime(db_session_factory)
    routing, burst, marks = await _held(db_session_factory, runtime, make_inbound("hello"))
    burst.add(make_inbound("and this"))

    woken: list[str] = []
    reply = await _pick(runtime, monkeypatch, "beta", wake=woken)
    if here:
        burst.wake()  # the app wakes its own burst; another process's finds the binding
    answered, posted = await asyncio.wait_for(routing, 5)

    assert answered and len(posted) == 1, "the held burst answers once picked"
    assert await _bound(db_session_factory) == "beta", "the chat is pinned to the pick"
    assert [i.text for i in burst.unanswered] == ["hello", "and this"], "both wait for one turn"
    assert marks == [("hello", True)], "the held message shows 👀"
    assert woken == [CONVERSATION_ID], "the click wakes the chat's burst"
    assert "You chose beta." in str(reply.value), "the card says what was chosen"


@pytest.mark.usefixtures("routed")
async def test_dm_pick_when_not_made_in_time_then_told_and_a_late_click_refused(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "alpha", "beta")
    monkeypatch.setattr(personal_chat, "PICK_SECONDS", 0.05)
    runtime = build_teams_runtime(db_session_factory)
    routing, _, _ = await _held(db_session_factory, runtime, make_inbound("hello"))
    answered, posted = await asyncio.wait_for(routing, 5)
    assert not answered, "nothing answers an unpicked burst"
    assert PICK_EXPIRED.split("\n", 1)[0] in str(_card(posted[1:])), "the person is told"

    woken: list[str] = []
    reply = await _pick(runtime, monkeypatch, "alpha", wake=woken, until=int(time()) - 1)
    assert PICK_EXPIRED.split("\n", 1)[0] in str(reply.value), "a late click is refused"
    assert not woken and await _bound(db_session_factory) is None, "nothing pinned"


@pytest.mark.usefixtures("routed")
async def test_dm_pick_when_the_process_stops_meanwhile_then_holds_nothing_unsaid(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app keeps a stopped burst for the next boot, so no expiry is said."""
    _agents(monkeypatch, "alpha", "beta")
    runtime = build_teams_runtime(db_session_factory)
    routing, burst, _ = await _held(db_session_factory, runtime, make_inbound("hello"))
    burst.stop()
    answered, posted = await asyncio.wait_for(routing, 5)
    assert (answered, len(posted)) == (False, 1), "only the card was posted"


@pytest.mark.usefixtures("routed")
async def test_dm_pick_when_a_lock_names_another_agent_then_refused_and_the_hold_ends(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal on the card speaks for the burst, so it ends at once with no expiry."""
    _agents(monkeypatch, "alpha", "beta")
    runtime = build_teams_runtime(db_session_factory)
    routing, burst, _ = await _held(db_session_factory, runtime, make_inbound("hello"))
    async with db_session_factory() as session, session.begin():
        await dm_store.add_rule(
            session,
            tenant_id=TENANT,
            subject="everyone",
            subject_id=None,
            agent_name="alpha",
            locked=True,
            set_by=None,
            now=_NOW,
            position=0,
        )
    woken: list[str] = []
    refused: list[str] = []

    def refuse(chat: str) -> None:
        refused.append(chat)
        burst.refuse()

    reply = await _pick(runtime, monkeypatch, "beta", wake=woken, refuse=refuse)
    answered, posted = await asyncio.wait_for(routing, 5)

    assert DM_AGENT_REFUSED.split("\n", 1)[0] in str(reply.value), "the lock refuses another"
    assert refused == [CONVERSATION_ID], "the click ends the chat's held burst"
    assert (answered, len(posted)) == (False, 1), "only the card was posted: no expiry"
    assert not woken and await _bound(db_session_factory) is None, "nothing pinned or woken"
