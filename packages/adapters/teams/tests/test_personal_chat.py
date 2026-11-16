"""Teams personal chats route by DM grants, rules and picks, and fail closed on unread rosters."""

from __future__ import annotations

from datetime import UTC, datetime
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
from daimon.adapters.teams.runtime import DmPick, TeamsRuntime
from daimon.core.authz import AgentRef
from daimon.core.direct_messages import DM_AGENT_REFUSED, dm_permission_text
from daimon.core.dm_access import DmAgent
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores import dm_access as dm_store
from daimon.core.stores.direct_messages import set_dm_enabled
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


def _agents(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    async def listed(*_: object, **__: object) -> list[DmAgent]:
        return [DmAgent(n, f"ag_{n}", AgentRef.of(n)) for n in names]

    monkeypatch.setattr(personal_chat, "list_dm_agents", listed)


async def _route(
    db: async_sessionmaker[AsyncSession],
    inbound: TeamsInbound | None = None,
    *,
    runtime: TeamsRuntime | None = None,
) -> tuple[bool, list[MessageActivityInput]]:
    posted: list[MessageActivityInput] = []

    async def post(card: MessageActivityInput) -> None:
        posted.append(card)

    answered = await route_personal_chat(
        runtime or build_teams_runtime(db), inbound or make_inbound(), tenant_id=TENANT, post=post
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


async def test_route_personal_chat_when_one_agent_then_pins_it_and_answers(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "helper")
    answered, posted = await _route(db_session_factory)
    assert answered and not posted, "one agent answers at once"
    assert await _bound(db_session_factory) == "helper", "the chat is pinned to it"


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


async def test_route_personal_chat_when_several_agents_then_offers_a_button_each(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "alpha", "beta")
    answered, posted = await _route(db_session_factory)
    assert not answered, "nothing answers until the person picks"
    body = _card(posted).get("body", [])
    actions = [a for block in body if block["type"] == "ActionSet" for a in block["actions"]]
    assert [(a["title"], a["verb"], a["data"]["agent"]) for a in actions] == [
        ("alpha", VERB, "alpha"),
        ("beta", VERB, "beta"),
    ], "one button per agent"
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


def _click(agent: str) -> Any:
    activity = SimpleNamespace(
        value=SimpleNamespace(action=SimpleNamespace(data={"agent": agent})),
        conversation=SimpleNamespace(id=CONVERSATION_ID, conversation_type="personal"),
        reply_to_id="1700000000000",
        id="click-1",
        service_url="https://smba.example.com",
        recipient=SimpleNamespace(name="Daimon"),
        from_=SimpleNamespace(name="Pat", id=f"29:{AAD_OBJECT_ID}"),
    )
    return SimpleNamespace(activity=activity)


async def _pick(
    runtime: TeamsRuntime, monkeypatch: pytest.MonkeyPatch, agent: str
) -> tuple[Any, list[TeamsInbound]]:
    async def actor(*_: object, **__: object) -> Actor:
        return Actor(AAD_OBJECT_ID, TENANT, False, CONVERSATION_ID)

    monkeypatch.setattr(personal_chat, "card_actor", actor)
    sent: list[TeamsInbound] = []
    picker = TeamsDmPick(runtime, dispatch=sent.append)
    return await picker._on_action(_click(agent)), sent  # pyright: ignore[reportPrivateUsage]


async def test_dm_pick_when_allowed_then_pins_and_answers_every_held_message(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Messages sent while the card is open join it, as on Slack: one card, one answer."""
    _agents(monkeypatch, "alpha", "beta")
    runtime = build_teams_runtime(db_session_factory)
    first = await _route(db_session_factory, make_inbound("hello"), runtime=runtime)
    later = await _route(db_session_factory, make_inbound("and this"), runtime=runtime)
    assert (first[0], len(first[1])) == (False, 1), "the first message gets the card"
    assert later == (False, []), "a later message waits on the same card"

    reply, sent = await _pick(runtime, monkeypatch, "beta")

    assert await _bound(db_session_factory) == "beta", "the chat is pinned to the pick"
    assert [i.text for i in sent] == ["hello\n\nand this"], "one turn answers both"
    assert (sent[0].activity_id, sent[0].from_form) == ("click-1", True), "under the click's id"
    assert "You chose beta." in str(reply.value), "the card says what was chosen"
    assert CONVERSATION_ID not in runtime.dm_picks, "the choice is closed"


async def test_dm_pick_when_nothing_is_held_then_asks_to_send_again(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card from an expired choice, or another process, pins but has nothing to answer."""
    _agents(monkeypatch, "alpha", "beta")
    runtime = build_teams_runtime(db_session_factory)
    await _route(db_session_factory, runtime=runtime)
    runtime.dm_picks[CONVERSATION_ID].expires_at = 0.0
    reply, sent = await _pick(runtime, monkeypatch, "alpha")
    assert not sent, "nothing to answer"
    assert await _bound(db_session_factory) == "alpha", "the pick still pins the chat"
    assert "Send your message again." in str(reply.value), "the person is told what to do"


async def test_dm_pick_when_a_lock_names_another_agent_then_refused_unpinned(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _agents(monkeypatch, "alpha", "beta")
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
        )
    runtime = build_teams_runtime(db_session_factory)
    runtime.dm_picks[CONVERSATION_ID] = DmPick(("alpha", "beta"), [make_inbound()], 1e12)
    reply, sent = await _pick(runtime, monkeypatch, "beta")
    assert reply.value == DM_AGENT_REFUSED, "the lock refuses another agent"
    assert not sent and await _bound(db_session_factory) is None, "nothing pinned or answered"
    assert CONVERSATION_ID in runtime.dm_picks, "the held message waits for an allowed pick"
