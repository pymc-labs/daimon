"""The model is told that a native `<@U…>` mention addresses the agent answering.

Slack delivers the app mention as the bot account's user id, which matches
neither a custom agent's name nor the display handle. These tests drive the
real turn path with a custom agent whose name differs from the Slack display
name, for a fresh top-level turn, its recovery re-seed and a queued turn, and
check what the model receives: the workspace's bot account inside the
trusted responder, the mention still XML-escaped in the query, and history
without the turn's own status card but with the account's earlier answers and
other bots' messages.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import CallbackResult
from aioresponses import aioresponses as AioResponsesMock
from daimon.adapters.slack import app as slack_app
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing import ma_session, ma_session_agent
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import _register_slack_defaults  # pyright: ignore[reportPrivateUsage]
from .harness import make_orchestrate_app

_TEAM_ID = "T_RESPONDER_IDENTITY"
# A workspace's bot user as auth.test reports it; another workspace has another.
_BOT_USER_ID = "U0WSBOT01"
_CHANNEL = "C_IDENTITY"
_THREAD_TS = "1900000000.000001"
_AGENT_NAME = "qa-specialist"
_REPLIES = re.compile(r"https://slack\.com/api/conversations\.replies.*")
_HISTORY = re.compile(r"https://slack\.com/api/conversations\.history.*")


class _Thread:
    """A Slack thread whose replies include every status card posted so far.

    The newest card is still `Thinking · 0s`, as Slack returns it while the
    turn that posted it is building context; earlier cards have become
    answers.
    """

    def __init__(self) -> None:
        self.cards: list[str] = []

    def post(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        ts = f"1900000001.{len(self.cards) + 1:06d}"
        self.cards.append(ts)
        return CallbackResult(payload={"ok": True, "ts": ts, "channel": _CHANNEL})

    def replies(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        messages: list[dict[str, str]] = [
            {"user": "U_PERSON", "text": f"<@{_BOT_USER_ID}> first question", "ts": _THREAD_TS},
            {
                "user": "U_OTHER_BOT",
                "bot_id": "B_OTHER",
                "text": "Other bot note",
                "ts": "1900000000.000002",
            },
        ]
        for ts in self.cards:
            text = "Thinking · 0s" if ts == self.cards[-1] else f"Prior answer {ts}"
            messages.append({"user": _BOT_USER_ID, "bot_id": "B_SELF", "text": text, "ts": ts})
        return CallbackResult(payload={"ok": True, "messages": messages, "has_more": False})

    def channel_history(self, _url: Any, **_kwargs: Any) -> CallbackResult:
        """The channel up to the first mention, newest first: an earlier answer
        from this bot account and another bot's note precede it."""
        messages: list[dict[str, str]] = [
            {"user": "U_PERSON", "text": f"<@{_BOT_USER_ID}> first question", "ts": _THREAD_TS},
            {
                "user": "U_OTHER_BOT",
                "bot_id": "B_OTHER",
                "text": "Other bot note",
                "ts": "1899999999.000002",
            },
            {
                "user": _BOT_USER_ID,
                "bot_id": "B_SELF",
                "text": "Earlier channel answer",
                "ts": "1899999999.000001",
            },
        ]
        return CallbackResult(payload={"ok": True, "messages": messages, "has_more": False})


@pytest.fixture
def thread() -> _Thread:
    return _Thread()


@pytest.fixture
def web_client(thread: _Thread) -> Iterator[AsyncWebClient]:
    # aioresponses answers with the first matching registration, so these go
    # ahead of the shared defaults.
    with AioResponsesMock() as mock:
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            "https://slack.com/api/chat.postMessage", callback=thread.post, repeat=True
        )
        mock.get(_REPLIES, callback=thread.replies, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        mock.get(_HISTORY, callback=thread.channel_history, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        _register_slack_defaults(mock)
        yield AsyncWebClient(token="xoxb-test")


def _controls(user_message: str) -> dict[str, Any]:
    assert user_message.startswith("<turn_controls>")
    return json.loads(user_message.splitlines()[1])


def _history(user_message: str) -> str:
    return re.split(r"<thread_|<channel_context", user_message, maxsplit=1)[1].split(
        "</context>", 1
    )[0]


def _event(ts: str, text: str, *, in_thread: bool = True) -> dict[str, Any]:
    event = {
        "type": "app_mention",
        "ts": ts,
        "event_ts": ts,
        "channel": _CHANNEL,
        "user": "U_PERSON",
        "text": text,
    }
    if in_thread:
        event["thread_ts"] = _THREAD_TS
    return event


async def test_fresh_reseed_and_queued_messages_name_the_mentioned_account_as_the_responder(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    thread: _Thread,
    web_client: AsyncWebClient,
) -> None:
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    await provision_tenant(
        db_session_factory, platform="slack", workspace_id=_TEAM_ID, signup_credit=Decimal("10")
    )
    app, _ = make_orchestrate_app(
        db_session_factory,
        deployment_default=DeploymentDefault(agent_name=_AGENT_NAME, environment_name="default"),
    )
    # What the mention gate leaves behind after auth.test for this workspace.
    app._bot_user_ids[_TEAM_ID] = _BOT_USER_ID  # pyright: ignore[reportPrivateUsage]

    # (message kind, live card ts, user_message). The re-seed is built by
    # calling the closure dead-session recovery would call; the recovery
    # cycle itself is not run.
    sent: list[tuple[str, str, str]] = []
    real_run_prepared_turn = slack_app.run_prepared_turn

    async def record(*args: Any, **kwargs: Any) -> Any:
        card = thread.cards[-1]
        sent.append(("turn", card, kwargs["user_message"]))
        sent.append(("reseed", card, await kwargs["reseed_user_message"]()))
        return await real_run_prepared_turn(*args, **kwargs)

    async def answer(**kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="done")])
        await kwargs["lifecycle"].on_terminal_success(state)
        return state

    first = _event(_THREAD_TS, f"<@{_BOT_USER_ID}> first question", in_thread=False)
    queued = _event("1900000002.000001", f"<@{_BOT_USER_ID}> queued question")
    # Queued behind the first mention, as `_orchestrate` would have while it ran.
    app._pending[_THREAD_TS] = [queued]  # pyright: ignore[reportPrivateUsage]

    with (
        patch("daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock) as agent,
        patch("daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock) as env,
        patch("daimon.core.turn.prepare.create_session", new_callable=AsyncMock) as create,
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as run_turn,
        patch("daimon.adapters.slack.app.run_prepared_turn", side_effect=record),
    ):
        agent.return_value = "agent_custom_qa"
        env.return_value = "env_custom_qa"
        create.return_value = ma_session(
            id="sess_identity",
            agent=ma_session_agent(id="agent_custom_qa"),
            environment_id="env_custom_qa",
        )
        run_turn.side_effect = answer

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            first,
            team_id=_TEAM_ID,
            channel=_CHANNEL,
            event_ts=_THREAD_TS,
            web_client=web_client,
            tenant_id=tenant_id,
        )

    assert [path for path, _, _ in sent] == ["turn", "reseed", "turn", "reseed"], (
        "the first mention and the queued one must each run and build a recovery re-seed"
    )
    for path, _card, message in sent:
        responder = _controls(message)["responder"]
        assert responder == {
            "name": _AGENT_NAME,
            "ma_agent_id": "agent_custom_qa",
            "handle": "@daimon",
            "platform_user_id": _BOT_USER_ID,
            "mention": f"<@{_BOT_USER_ID}>",
        }, f"{path}: the mentioned bot account must be named as this custom responder"
        assert f"&lt;@{_BOT_USER_ID}&gt;" in message.split("<user_query", 1)[1], (
            f"{path}: the native mention keeps its XML escaping in the query"
        )

    first_history = _history(sent[0][2])
    assert sent[0][1] not in first_history and "Thinking" not in first_history, (
        "the first turn must not replay its own status card as another bot's post"
    )
    assert "Other bot note" in first_history, "other bots' messages stay in history"
    assert "Earlier channel answer" in first_history, (
        "an earlier answer from the same account stays in the channel context"
    )
    assert "first question" not in first_history, "the mention itself is the query, not history"
    assert _history(sent[1][2]) == first_history, (
        "the recovery re-seed rebuilds the same channel context"
    )

    queued_message = sent[2][2]
    assert "queued question" in queued_message.split("<user_query", 1)[1]
    queued_history = _history(queued_message)
    assert "Thinking" not in queued_history, "the queued turn omits its own card"
    assert f"Prior answer {sent[0][1]}" in queued_history, (
        "an earlier answer from the same account is kept, not taken for the live card"
    )
    assert "Thinking" not in _history(sent[3][2])
