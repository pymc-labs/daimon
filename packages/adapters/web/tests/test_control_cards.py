"""Call cards in the web chat: raised from the MCP process's notice, pressed in the chat."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from daimon.core.host_calls import WAKE_SEED
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores import host_call_cards as cards
from daimon.core.stores.web_notices import WebControlNotice, parse_web_notice
from daimon.core.turn.mutation_gate import initial_send_prefix
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import Workspace, build_turns, funded_workspace, one_session, signed_in
from .conftest import PUBLIC_URL, WebClientFactory, web_settings


def _text(said: str) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=said)])


async def _call_card(
    factory: async_sessionmaker[AsyncSession], ws: Workspace, thread_key: str, requester: str
) -> str:
    """A posted host call card in `thread_key`; its web card id."""
    async with factory.begin() as session:
        created = await cards.create_card(
            session,
            tenant_id=ws.tenant_id,
            origin_id=uuid.uuid4(),
            requester_account_id=ws.person.account_id,
            requester_platform_user_id=requester,
            platform="web",
            channel_id=f"web:{ws.person.id}",
            thread_id=thread_key,
            executing_agent_id=derive_agent_uuid(tenant_id=ws.tenant_id, ma_agent_id=AGENT_ID),
            executing_ma_agent_id=AGENT_ID,
            agent_name="Daimon",
            tool_name="publish_report",
            args_digest="d",
            reference={"route": "daimon", "args": {}},
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        assert created is not None, "the card is recorded"
        card_id = f"hcc:{created.row.id}"
        await cards.mark_posted(session, card_id=created.row.id, message_id=card_id)
    return card_id


async def _press(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    answer: str,
    *,
    requester: str | None = None,
) -> tuple[httpx.Response, list[str], list[object], dict[str, Any]]:
    """The first turn posts a call card; then the person presses `answer` on it."""
    ws = await funded_workspace(factory)
    turns = build_turns(factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(factory, ws.person)
    asked: list[str] = []
    prefixes: list[object] = []
    card: dict[str, Any] = {}

    async def turn(*_args: object, **kw: Any) -> SimpleNamespace:
        asked.append(kw["user_message"])
        prefixes.append(initial_send_prefix.get())
        if len(asked) == 1:
            card_id = await _call_card(factory, ws, kw["thread_id"], requester or str(ws.person.id))
            card["id"] = card_id
            await turns.on_notice(
                WebControlNotice(
                    thread_id=kw["thread_id"], card_id=card_id, title="Daimon wants to run it"
                )
            )
        said = _text("Waiting." if len(asked) == 1 else "Done.")
        await kw["lifecycle"].on_render(said)
        return SimpleNamespace(state=said)

    async def wake() -> str:
        return "ran"

    with (
        one_session(),
        patch("daimon.adapters.web.turns.run_prepared_turn", turn),
        patch("daimon.adapters.web.chat.wake_prefix", lambda *_a, **_k: wake),  # pyright: ignore[reportUnknownLambdaType]
    ):
        async with web_client(web_settings(), sessionmaker=factory, turns=turns) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update({"X-Daimon-Web": "1", "Origin": PUBLIC_URL})
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            await client.post(
                f"/api/chat/sessions/{sid}/messages",
                json={"id": str(uuid.uuid4()), "content": "publish it"},
            )
            pressed = await client.post(
                f"/api/chat/sessions/{sid}/cards/{card['id']}", json={"answer": answer}
            )
            await turns.drain(10)
            detail = (await client.get(f"/api/chat/sessions/{sid}")).json()
    return pressed, asked, prefixes, detail


def _card_of(detail: dict[str, Any]) -> dict[str, Any]:
    shown = [m["card"] for m in detail["messages"] if m.get("card")]
    assert len(shown) == 1, f"one card shows in the chat: {shown}"
    return shown[0]


async def test_an_approved_call_carries_on_in_its_wake_turn(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Approve settles the card and runs the wake turn with the approved call's prefix."""
    pressed, asked, prefixes, detail = await _press(
        web_client, db_session_factory, tmp_path, "approve"
    )

    assert pressed.status_code == 200, pressed.text
    assert len(asked) == 2 and asked[1].endswith(WAKE_SEED), "the press's reply is the wake turn"
    assert prefixes[0] is None and prefixes[1] is not None, "only the wake turn runs the call"
    card = _card_of(detail)
    assert (card["kind"], card["state"]) == ("approval", "approved"), card


async def test_a_denied_call_settles_the_card_and_starts_no_turn(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Deny closes the card and nothing runs."""
    pressed, asked, _, detail = await _press(web_client, db_session_factory, tmp_path, "deny")

    assert pressed.status_code == 200 and pressed.json()["type"] == "settled", pressed.text
    assert len(asked) == 1, "no wake turn runs"
    assert _card_of(detail)["state"] == "denied", "the card reads as denied"


async def test_only_the_requester_may_press(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Someone else's card is refused and stays pending."""
    pressed, asked, _, detail = await _press(
        web_client, db_session_factory, tmp_path, "approve", requester="someone-else"
    )

    assert pressed.status_code == 409, "another person's card is refused"
    assert len(asked) == 1, "no wake turn runs"
    assert _card_of(detail)["state"] == "pending", "the card is left as it was"


def test_a_control_notice_round_trips() -> None:
    """The listener reads back what the MCP process sends."""
    sent = WebControlNotice(thread_id="web:t", card_id="hcc:x", title="t", details=("a",))

    assert parse_web_notice(sent.model_dump_json()) == sent, "the notice parses as sent"
