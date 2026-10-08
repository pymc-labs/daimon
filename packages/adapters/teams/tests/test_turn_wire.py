"""Real turns through `/api/messages`, down to the Bot Framework calls they make.

The real turn driver consumes a scripted Managed Agents stream (`build_turn_router`); only
session creation, MSAL and the Bot Framework transport are faked. Every activity a turn
sends must be one Teams accepts (`assert_teams_accepts`).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from anthropic.types.beta.sessions import (
    BetaManagedAgentsAgentMessageEvent,
    BetaManagedAgentsAgentToolResultEvent,
    BetaManagedAgentsAgentToolUseEvent,
    BetaManagedAgentsSessionEndTurn,
    BetaManagedAgentsSessionStatusIdleEvent,
    BetaManagedAgentsTextBlock,
)
from daimon.adapters.teams import app, card, lifecycle
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.core import output_delivery
from daimon.core.config import SupportSettings
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.termination import TerminationReason
from daimon.testing import list_response, ma_session
from daimon.testing.ma import (
    MARouter,
    build_fake_anthropic,
    make_fake_memory_store_handler,
    sse_response,
)
from daimon.testing.ma_models import DEFAULT_AGENT_NAME, DEFAULT_ENV_NAME
from daimon.testing.turn_router import AGENT_ID, AGENT_TEXT, ENV_ID, build_turn_router, turn_events
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    THREAD_ID,
    SentRequest,
    TeamsApiFake,
    assert_teams_accepts,
    build_teams_runtime,
    make_channel_activity,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "funded_tenant")

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
# The agent and environment `build_turn_router` resolves.
TURN_DEFAULT = DeploymentDefault(agent_name=DEFAULT_AGENT_NAME, environment_name=DEFAULT_ENV_NAME)
SESSION_ID = "sesn_teams_wire"
STREAM = rf"/v1/sessions/{SESSION_ID}/events/stream"
CONVERSATIONS = f"{httpx.URL(SERVICE_URL).path}/v3/conversations"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def funded_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Provisioned with credit, so the balance gate admits the turn."""
    await provision_tenant(
        db_session_factory,
        platform="teams",
        workspace_id=ENTRA_TENANT_ID,
        signup_credit=Decimal("100"),
    )


@asynccontextmanager
async def _service(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    router: MARouter,
    *,
    one_session: bool = True,
    support: bool = False,
) -> AsyncIterator[TeamsHttpService]:
    """The started service over `router`; drained on exit, then every activity is checked.

    `one_session` hands every turn `SESSION_ID`; without it `router` creates sessions.
    `support` turns human support on, posting to a Teams channel."""
    runtime = build_teams_runtime(
        db_factory,
        anthropic=build_fake_anthropic(router.dispatch),
        deployment_default=TURN_DEFAULT,
    )
    if support:
        runtime.settings.support = SupportSettings(escalation_channel_id="19:ops@thread.tacv2")
    session = ma_session(id=SESSION_ID, agent_id=AGENT_ID, environment_id=ENV_ID)
    create = patch("daimon.core.turn.prepare.create_session", return_value=session)
    with create if one_session else nullcontext():
        async with running_service(runtime, fake) as service:
            yield service
            await service.turns.drain(timeout=30)
    for request in fake.activity_requests:
        assert_teams_accepts(request)


async def _turn(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    router: MARouter,
    activity: dict[str, object],
    *,
    support: bool = False,
) -> None:
    async with _service(db_factory, fake, router, support=support) as service:
        await post_activity(service, activity)


def _held_stream(
    held: asyncio.Event,
    release: asyncio.Event,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]] | None = None,
) -> httpx.Response:
    """An SSE stream that sends `before`, then stays open until `release`, then sends `after`."""

    async def body() -> AsyncIterator[bytes]:
        if before:
            yield sse_response(before).content
        held.set()
        await release.wait()
        yield sse_response(after or []).content

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def _dropped_stream(events: list[dict[str, Any]]) -> httpx.Response:
    """An SSE stream whose connection drops after `events`."""

    async def body() -> AsyncIterator[bytes]:
        if events:
            yield sse_response(events).content
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message")

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def _path(request: SentRequest) -> str:
    return httpx.URL(request.url).path


def _card(request: SentRequest) -> dict[str, Any]:
    [attachment] = cast(list[dict[str, Any]], request.body["attachments"])
    return attachment["content"]


def _texts(request: SentRequest) -> list[str]:
    """The card's TextBlocks, in order."""
    return [b["text"] for b in _card(request)["body"] if b["type"] == "TextBlock"]


def _actions(request: SentRequest) -> list[dict[str, Any]]:
    sets = [b for b in _card(request)["body"] if b["type"] == "ActionSet"]
    return [action for s in sets for action in s["actions"]]


def _feedback(request: SentRequest) -> object:
    return cast(dict[str, Any], request.body.get("channelData") or {}).get("feedbackLoop")


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(10):
        while not condition():
            await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    ("activity", "conversation"),
    [(make_message_activity(), CONVERSATION_ID), (make_channel_activity(), THREAD_ID)],
    ids=["personal", "channel_thread"],
)
async def test_answer_replaces_the_status_card_in_the_conversation_it_came_from(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    activity: dict[str, object],
    conversation: str,
) -> None:
    router = build_turn_router(str(TENANT), session_id=SESSION_ID)
    await _turn(db_session_factory, teams_api_fake, router, activity)

    status, answer = teams_api_fake.activity_requests
    card_url = f"{CONVERSATIONS}/{conversation}/activities"
    assert (status.method, _path(status)) == ("POST", card_url), "the card, in that conversation"
    [cancel] = _actions(status)
    assert cancel["verb"] == card.CANCEL_VERB, "the live card offers Cancel"
    assert (answer.method, _path(answer)) == (
        "PUT",
        f"{CONVERSATIONS}/{conversation}/activities/m-1",
    ), "the answer replaces the card in place"
    assert answer.body["text"] == AGENT_TEXT, "the answer alone, no usage footer"
    assert "attachments" not in answer.body, "no card left under the answer"
    assert _feedback(answer) == {"type": "custom"}, "Teams' thumbs, answered by our own form"
    assert "AIGeneratedContent" in str(answer.body["entities"]), "labelled AI generated"


async def test_with_support_on_the_answer_carries_an_ask_a_human_button_teams_accepts(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    router = build_turn_router(str(TENANT), session_id=SESSION_ID)
    await _turn(db_session_factory, teams_api_fake, router, make_message_activity(), support=True)

    _status, answer = teams_api_fake.activity_requests
    assert answer.body["text"] == AGENT_TEXT, "the answer stays markdown text"
    [button] = _actions(answer)
    assert (button["type"], button["title"]) == ("Action.Submit", card.ASK_HUMAN)
    assert button["data"]["msteams"]["type"] == "task/fetch", "it opens a dialog"
    assert _feedback(answer) == {"type": "custom"}, "the thumbs stay beside it"


def _echo_sessions(router: MARouter) -> None:
    """Sessions created from the request, as MA does, so a reused one matches its config,
    memory mount included."""
    sessions: dict[str, dict[str, Any]] = {}
    memory = make_fake_memory_store_handler()
    for method in ("GET", "POST"):
        router.add(method, r"/v1/memory_stores(/.*)?", lambda r, m: memory(r))

    def create(request: httpx.Request, _match: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        session = ma_session(
            id=f"sesn_{len(sessions) + 1}",
            agent_id=AGENT_ID,
            environment_id=ENV_ID,
            resources=body.get("resources", []),
            metadata=body.get("metadata") or {},
        ).model_dump(mode="json")
        sessions[session["id"]] = session
        return httpx.Response(200, json=session)

    router.add("POST", r"/v1/sessions", create)
    router.add(
        "GET",
        r"/v1/sessions/(?P<id>[^/]+)",
        lambda r, m: httpx.Response(200, json=sessions[m["id"]]),
    )


NEW_THREAD = f"{CHANNEL_ID};messageid=1700000000002"


@pytest.mark.parametrize(
    ("activities", "expected"),
    [
        (
            [make_message_activity(), make_message_activity(activity_id="activity-2")],
            ["sesn_1", "sesn_1"],
        ),
        (
            [
                make_channel_activity(),
                make_channel_activity(activity_id="activity-2"),
                make_channel_activity(activity_id="activity-3", conversation_id=NEW_THREAD),
            ],
            ["sesn_1", "sesn_1", "sesn_2"],
        ),
    ],
    ids=["personal", "channel"],
)
async def test_a_follow_up_reuses_its_session_and_a_new_thread_starts_one(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
    activities: list[dict[str, object]],
    expected: list[str],
) -> None:
    monkeypatch.setattr(output_delivery, "_POLL_DELAYS_S", (0.0,))  # One listing settles.
    streams: list[str] = []
    router = build_turn_router(str(TENANT), fresh_event_ids=True, stream_hits=streams)
    router.add("GET", r"/v1/files", lambda r, m: list_response([]))  # The output sweep.
    _echo_sessions(router)

    def answers() -> list[SentRequest]:
        return [r for r in teams_api_fake.activity_requests if "text" in r.body]

    async with _service(db_session_factory, teams_api_fake, router, one_session=False) as service:
        for count, activity in enumerate(activities, start=1):
            await post_activity(service, activity)
            # Tests share one DB connection: a turn must finish before the next starts.
            await _until(
                lambda count=count: len(answers()) == count and not service.turns.in_flight
            )

    assert streams == expected, "one session per chat or thread"
    texts = [str(r.body["text"]) for r in answers()]
    assert all(t.startswith(AGENT_TEXT) for t in texts), "no continuity notice on a reused session"


async def test_long_answer_splits_into_ordered_parts_and_keeps_its_code_block_whole(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    prose = "\n\n".join(f"Paragraph {i}: {'words ' * 60}" for i in range(10))
    code = "```python\n" + "\n".join(f"print({i})" for i in range(80)) + "\n```"
    notes = "\n\n".join(f"Note {i}: {'more ' * 70}" for i in range(12))
    answer = f"{prose}\n\n{code}\n\n{notes}"
    assert len(prose) < card.TEAMS_LIMIT < len(prose) + len(code), "the block straddles a cut"
    router = build_turn_router(str(TENANT), session_id=SESSION_ID, agent_text=answer)
    await _turn(db_session_factory, teams_api_fake, router, make_message_activity())

    _status, *parts = teams_api_fake.activity_requests
    assert [(r.method, _path(r).rsplit("/", 1)[-1]) for r in parts] == [
        ("PUT", "m-1"),
        ("POST", "activities"),
        ("POST", "activities"),
    ], "the first part replaces the card, the rest follow"
    texts = [str(r.body["text"]) for r in parts]
    assert "\n\n".join(texts) == answer, "the parts are the answer, in order, no usage footer"
    whole = [code in str(r.body["text"]) for r in parts]
    assert whole == [False, True, False], "the code block stays whole in one part"
    feedback = [_feedback(r) for r in parts]
    assert feedback == [None, None, {"type": "custom"}], "only the last part asks for feedback"
    labels = ["AIGeneratedContent" in str(r.body["entities"]) for r in parts]
    assert all(labels), "every part is labelled AI generated"


_RUNNING_BASH = "🖋️ Running a command"


async def test_tool_use_edits_the_status_card_before_the_answer_replaces_it(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The driver renders each change once; one inside the debounce window is not retried.
    monkeypatch.setattr(lifecycle, "_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(output_delivery, "_POLL_DELAYS_S", (0.0,))  # One listing settles.
    tool_use = BetaManagedAgentsAgentToolUseEvent(
        id="evt_tool", type="agent.tool_use", name="bash", input={"command": "ls"}, processed_at=NOW
    )
    result = BetaManagedAgentsAgentToolResultEvent(
        id="evt_result", type="agent.tool_result", tool_use_id="evt_tool", processed_at=NOW
    )
    held, release = asyncio.Event(), asyncio.Event()
    router = MARouter()
    router.add(
        "GET",
        STREAM,
        lambda r, m: _held_stream(
            held,
            release,
            [tool_use.model_dump(mode="json")],
            [result.model_dump(mode="json"), *turn_events(now=NOW)],
        ),
    )
    router.add("GET", r"/v1/files", lambda r, m: list_response([]))  # The output sweep.
    build_turn_router(str(TENANT), session_id=SESSION_ID, router=router)

    def shows_tool() -> bool:
        edits = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
        return any(_RUNNING_BASH in _texts(r) for r in edits)

    async with _service(db_session_factory, teams_api_fake, router) as service:
        await post_activity(service, make_message_activity())
        await _until(shows_tool)
        release.set()

    status, *edits, answer = teams_api_fake.activity_requests
    assert status.method == "POST" and edits, "the card is posted, then edited"
    targets = {(r.method, _path(r).rsplit("/", 1)[-1]) for r in [*edits, answer]}
    assert targets == {("PUT", "m-1")}, "every edit lands on the card"
    progress = next(r for r in edits if _RUNNING_BASH in _texts(r))
    assert _texts(progress)[0].startswith("**Working** · "), "the phase follows the turn"
    assert [a["verb"] for a in _actions(progress)] == [card.CANCEL_VERB], "Cancel stays"
    assert str(answer.body["text"]).startswith(AGENT_TEXT), "then the answer replaces it"


async def test_stream_that_drops_mid_answer_leaves_the_failure_notice_and_no_answer(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    partial = BetaManagedAgentsAgentMessageEvent(
        id="evt_partial",
        type="agent.message",
        processed_at=NOW,
        content=[BetaManagedAgentsTextBlock(type="text", text="The first half of an answer")],
    ).model_dump(mode="json")
    # The drop, and the one reconnect the driver makes after replaying the log.
    streams = [_dropped_stream([partial]), _dropped_stream([])]
    router = MARouter()
    router.add("GET", STREAM, lambda r, m: streams.pop(0))
    router.add("GET", rf"/v1/sessions/{SESSION_ID}/events", lambda r, m: list_response([partial]))
    build_turn_router(str(TENANT), session_id=SESSION_ID, router=router)
    await _turn(db_session_factory, teams_api_fake, router, make_message_activity())

    assert streams == [], "the driver reconnected once"
    status, notice = teams_api_fake.activity_requests
    assert (notice.method, _path(notice)) == ("PUT", _path(status) + "/m-1"), "the card is closed"
    [text] = _texts(notice)
    lost = render_termination_notice(TerminationReason.CONNECTION_LOST)
    assert lost is not None and text.startswith(f"❌ {lost.headline}: "), "the drop, named"
    assert "first half" not in text, "no partial answer"
    assert _actions(notice) == [] and "text" not in notice.body, "no Cancel, no answer message"


async def test_cancel_from_the_author_interrupts_the_session_and_closes_the_card(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    idle = BetaManagedAgentsSessionStatusIdleEvent(
        id="evt_idle",
        type="session.status_idle",
        processed_at=NOW,
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
    ).model_dump(mode="json")
    held, release = asyncio.Event(), asyncio.Event()
    # A turn that never ends on its own, then the stream the interrupt is acknowledged on.
    streams = [_held_stream(held, release, []), sse_response([idle])]
    sent: list[dict[str, Any]] = []
    router = MARouter()
    router.add("GET", STREAM, lambda r, m: streams.pop(0))
    build_turn_router(str(TENANT), session_id=SESSION_ID, sent_event_bodies=sent, router=router)

    async with _service(db_session_factory, teams_api_fake, router) as service:
        await post_activity(service, make_message_activity())
        await asyncio.wait_for(held.wait(), 10)
        [status] = teams_api_fake.activity_requests
        [cancel] = _actions(status)
        # Teams echoes the button's verb and data, from the card's own message.
        action = {"type": "Action.Execute", "verb": cancel["verb"], "data": cancel["data"]}
        value: dict[str, object] = {"action": action, "trigger": "manual"}
        click = make_invoke("adaptiveCard/action", value) | {"replyToId": "m-1"}
        other = make_invoke("adaptiveCard/action", value, user=OTHER_AAD_OBJECT_ID)

        refused = await post_activity(service, other | {"replyToId": "m-1"})
        await asyncio.sleep(0.2)  # Time for a wrongly set cancel to reach MA.
        sent_after_refusal = len(sent)
        accepted = await post_activity(service, click)

    message = "application/vnd.microsoft.activity.message"
    assert refused == {"statusCode": 200, "type": message, "value": app._CANCEL_NOT_AUTHOR}  # pyright: ignore[reportPrivateUsage]
    assert sent_after_refusal == 1, "another person's click sends nothing to MA"
    assert accepted == {"statusCode": 200, "type": message, "value": app._CANCELLING}  # pyright: ignore[reportPrivateUsage]
    events = [e["type"] for body in sent for e in body["events"]]
    assert events == ["user.message", "user.interrupt"], "one interrupt reaches MA"
    assert streams == [] and not release.is_set(), "the interrupt ends the turn, not the stream"
    _status, closed = teams_api_fake.activity_requests
    assert (closed.method, _path(closed)) == ("PUT", _path(status) + "/m-1"), "the card is closed"
    assert _texts(closed) == [card.CANCELLED_NOTICE] and _actions(closed) == [], "and says so"
