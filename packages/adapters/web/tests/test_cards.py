"""Approval cards: a card ends the reply so far, and its answer carries the turn on."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
from daimon.core.posted_controls.tool_rule_confirmation import ONLY_ADMIN_MESSAGE
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import build_turns, funded_workspace, one_session, signed_in, sse_frames
from .conftest import PUBLIC_URL, WebClientFactory, web_settings


def _text(text: str) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


async def _chat(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    script: Any,
) -> Any:
    """Run `script(client, sid)` against a chat whose turn asks for one approval."""
    ws = await funded_workspace(factory)
    turns = build_turns(factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    token = await signed_in(factory, ws.person)
    answers: list[ConfirmationAnswer] = []

    async def turn_with_a_card(*_args: object, **kw: Any) -> SimpleNamespace:
        await kw["lifecycle"].on_render(_text("I'll post it."))
        prompt = ConfirmationPrompt(
            title="Post to #general?",
            requester_platform_user_id=str(ws.person.id),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        answers.append(await kw["confirm_write"](prompt))
        done = _text("I'll post it. Posted." if answers[-1] == "approved" else "I'll post it.")
        await kw["lifecycle"].on_render(done)
        return SimpleNamespace(state=done)

    with one_session(), patch("daimon.adapters.web.turns.run_prepared_turn", turn_with_a_card):
        async with web_client(
            web_settings(ws.tenant_id), sessionmaker=factory, turns=turns
        ) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update({"X-Daimon-Web": "1", "Origin": PUBLIC_URL})
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            result = await script(client, sid)
            await turns.drain(10)
    return result, answers


async def test_approving_carries_the_turn_on_in_a_new_reply(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str) -> Any:
        body = {"id": str(uuid.uuid4()), "content": "post it"}
        first = await client.post(f"/api/chat/sessions/{sid}/messages", json=body)
        waiting = (await client.get(f"/api/chat/sessions/{sid}")).json()
        card = sse_frames(first.text)[-1][1]["message"]["card"]
        later = await client.post(
            f"/api/chat/sessions/{sid}/cards/{card['id']}", json={"answer": "approve"}
        )
        await asyncio.sleep(0.05)
        return first, waiting, later, (await client.get(f"/api/chat/sessions/{sid}")).json()

    (first, waiting, later, detail), answers = await _chat(
        web_client, db_session_factory, tmp_path, script
    )

    events = [e for _, e in sse_frames(first.text)]
    assert [e["type"] for e in events[-2:]] == ["card", "done"], events
    card = events[-1]["message"]["card"]
    assert (card["state"], card["always_allowed"]) == ("pending", False)
    assert events[-1]["message"]["content"] == "I'll post it."
    assert waiting["session"]["running"] is False and "resume_from" not in waiting
    assert answers == ["approved"]
    assert sse_frames(later.text)[-1][1]["message"]["content"] == "Posted.", later.text
    replies = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert [m["content"] for m in replies] == ["I'll post it.", "Posted."]
    assert replies[0]["card"]["state"] == "approved"


async def test_always_needs_a_rule_and_stop_denies_the_card(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str) -> Any:
        body = {"id": str(uuid.uuid4()), "content": "post it"}
        first = await client.post(f"/api/chat/sessions/{sid}/messages", json=body)
        card_id = sse_frames(first.text)[-1][1]["message"]["card"]["id"]
        always = await client.post(
            f"/api/chat/sessions/{sid}/cards/{card_id}", json={"answer": "always"}
        )
        unknown = await client.post(
            f"/api/chat/sessions/{sid}/cards/nope", json={"answer": "approve"}
        )
        await client.post(f"/api/chat/sessions/{sid}/stop")
        await asyncio.sleep(0.05)
        return always, unknown, (await client.get(f"/api/chat/sessions/{sid}")).json()

    (always, unknown, detail), answers = await _chat(
        web_client, db_session_factory, tmp_path, script
    )

    assert (always.status_code, always.json()["detail"]) == (409, ONLY_ADMIN_MESSAGE)
    assert unknown.status_code == 409
    assert answers == ["denied"]
    replies = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert replies[0]["card"]["state"] == "denied"
    assert replies[-1]["stopped"] is True


async def test_a_restart_denies_the_card_a_turn_waits_on(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str) -> float:
        body = {"id": str(uuid.uuid4()), "content": "post it"}
        await client.post(f"/api/chat/sessions/{sid}/messages", json=body)
        return asyncio.get_running_loop().time()

    started, answers = await _chat(web_client, db_session_factory, tmp_path, script)

    assert answers == ["denied"], "the drain answers the open card, so the turn can end"
    assert asyncio.get_running_loop().time() - started < 5, "and does not wait out the card"
