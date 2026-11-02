"""What a restart's gap lost, read back from Graph at boot and admitted once.

Graph is a `MockTransport` and the admission table the real test database. The
turn and participation paths are recorded, not run: only which messages reach
them, how often and in what order is under test.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from daimon.adapters.teams import restart_catch_up
from daimon.adapters.teams.admissions import TeamsAdmissions
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.identity import DENIED, GraphSource, TeamsInbound
from daimon.adapters.teams.restart_catch_up import CATCH_UP_WINDOW, RestartCatchUp
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import TeamsInstallationRow
from daimon.core.stores.teams_channel_subscriptions import insert_teams_channel_subscription
from daimon.core.stores.teams_message_admissions import forget_teams_messages_for_user
from daimon.core.stores.tenants import set_plain_text_mentions
from daimon.core.teams_graph import GraphClient, GraphMessage
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from microsoft_teams.api import MessageActivity
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_CLIENT_ID,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    GROUP_CHAT_ID,
    SERVICE_URL,
    TEAM_GROUP_ID,
    FakeSender,
    bot_token,
    build_teams_runtime,
    make_channel_activity,
    make_group_activity,
    make_message_activity,
    shown_text,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
# The team's General channel: its id is the team's.
TEAM = "19:team@thread.tacv2"
OTHER_CHANNEL = "19:channel-2@thread.tacv2"
PRIVATE = "19:private-1@thread.tacv2"
DIRECT = "a:conversation-1"
BOT = {"application": {"id": BOT_CLIENT_ID, "displayName": "Daimon"}}
MENTION = {"id": 0, "mentioned": {"application": {"id": BOT_CLIENT_ID}}}


@pytest.fixture(autouse=True)
def no_restart_catch_up() -> None:
    """Overrides the conftest fixture that stubs the catch-up: these tests run it."""


@pytest.fixture
def db_session_factory(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> async_sessionmaker[AsyncSession]:
    """A connection per session, as in production: admission claims overlap other writes.

    `db_session` wipes the schema first.
    """
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


def _ago(minutes: float) -> str:
    sent = datetime.now(UTC) - timedelta(minutes=minutes)
    return sent.isoformat().replace("+00:00", "Z")


def _graph(
    id: str,
    minutes_ago: float,
    html: str = "<p>any thoughts?</p>",
    *,
    reply_to: str | None = None,
    sender: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A Graph `chatMessage` by a person, `minutes_ago` old; a reply under `reply_to`."""
    user = {"id": AAD_OBJECT_ID, "displayName": "Ada", "tenantId": ENTRA_TENANT_ID}
    return {
        "id": id,
        "messageType": "message",
        "createdDateTime": _ago(minutes_ago),
        "replyToId": reply_to,
        "from": sender if sender is not None else {"user": user},
        "body": {"contentType": "html", "content": html},
    } | extra


def _mention(id: str, minutes_ago: float, text: str = "what is up", **extra: Any) -> dict[str, Any]:
    html = f'<p><at id="0">Daimon</at> {text}</p>'
    return _graph(id, minutes_ago, html, mentions=[MENTION], **extra)


class FakeGraph:
    """Graph's channel and chat listings, recording each request.

    `channels` and `chats` map an id to the page it lists; `refused` ids answer 403.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.channels: dict[str, list[dict[str, Any]]] = {}
        self.chats: dict[str, list[dict[str, Any]]] = {}
        self.refused: set[str] = set()

    def read(self, conversation_id: str) -> list[httpx.Request]:
        return [r for r in self.requests if f"/{conversation_id}/" in r.url.path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if found := re.fullmatch(r"/v1.0/teams/[^/]+/channels/([^/]+)/messages", path):
            pages = self.channels
        elif found := re.fullmatch(r"/v1.0/chats/([^/]+)/messages", path):
            pages = self.chats
        else:
            return httpx.Response(404)
        if found[1] in self.refused:
            return httpx.Response(403)
        return httpx.Response(200, json={"value": pages.get(found[1], [])})


async def _graph_token() -> str:
    return "graph-token"


class Boot:
    """A `TeamsApp` whose boot reads back from `graph`, and what reached its intake."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        graph: FakeGraph,
        *,
        sender: FakeSender | None = None,
        standard: dict[str, str | None] | None = None,
        fence: bool = True,
        serving_at: datetime | None = None,
    ) -> None:
        # When the service began accepting activities: the catch-up reads up to it.
        self.serving_at = serving_at or datetime.now(UTC)
        # Whether a turn starting marks its message handled; off, it is left held for a replay.
        self._fence = fence
        self.factory = factory
        self.paths: list[tuple[str, TeamsInbound]] = []
        listed: dict[str, str | None] = (
            {CHANNEL_ID: "Ops", OTHER_CHANNEL: "Dev"} if standard is None else standard
        )

        async def teams() -> list[TeamsInstallationRow]:
            return [
                TeamsInstallationRow(
                    tenant_id=TENANT, team_id=TEAM, group_id=TEAM_GROUP_ID, name="Team"
                )
            ]

        async def standard_channels(_team_id: str) -> dict[str, str | None]:
            return listed

        async def deliver(source: GraphSource, messages: list[GraphMessage]) -> None:
            await self.app.catch_up_graph_messages(source, messages)

        async def serving_from() -> datetime:
            return self.serving_at

        self.app = TeamsApp(
            runtime=build_teams_runtime(factory),
            sender=sender or FakeSender(),
            commands={},
            bot_token=bot_token,
            catch_up=RestartCatchUp(
                sessionmaker=factory,
                tenant_id=TENANT,
                graph=GraphClient(
                    httpx.AsyncClient(transport=httpx.MockTransport(graph.handler)), _graph_token
                ),
                list_teams=teams,
                standard_channels=standard_channels,
                deliver=deliver,
                serving_from=serving_from,
                bot_app_id=BOT_CLIENT_ID,
                should_stop=lambda: self.app.draining,
                service_url=SERVICE_URL,
            ),
        )

    async def boot(self) -> None:
        """Start the app: it reclaims what an exited one held, then reads back."""
        await self._run(self.app.start())

    async def bot_framework(self, payload: dict[str, object]) -> None:
        ctx = SimpleNamespace(
            activity=MessageActivity.model_validate(payload),
            conversation_ref=SimpleNamespace(service_url=SERVICE_URL),
            reply=AsyncMock(),
        )
        await self._run(self.app.handle_message(ctx))  # pyright: ignore[reportArgumentType]

    async def _run(self, delivery: Any) -> None:
        async def _handle(_self: TeamsApp, inbound: TeamsInbound) -> None:
            self.paths.append(("turn", inbound))
            if self._fence:
                await self.app._admissions.finish([inbound])  # pyright: ignore[reportPrivateUsage]

        async def _observe(_self: TeamsApp, inbound: TeamsInbound) -> None:
            self.paths.append(("participation", inbound))

        with (
            patch.object(TeamsApp, "_handle", _handle),
            patch.object(TeamsApp, "_observe", _observe),
        ):
            await delivery
            async with asyncio.timeout(10):
                while tasks := list(self.app._tasks):  # pyright: ignore[reportPrivateUsage]
                    await asyncio.gather(*tasks)
                    await asyncio.sleep(0)  # lets a finished task's done callback drop it

    def ran(self, conversation: str) -> list[tuple[str, str]]:
        """`(path, activity id)` of what ran in `conversation` (a channel or chat id), in order."""
        return [
            (path, inbound.activity_id)
            for path, inbound in self.paths
            if inbound.channel_id == conversation
        ]


async def _admitted_before(
    factory: async_sessionmaker[AsyncSession],
    conversation_id: str,
    message_id: str = "1700000000000",
) -> None:
    """A message admitted and handled earlier: what makes `conversation_id` recent."""
    admissions = TeamsAdmissions(factory, TENANT, owner_key=1)
    await admissions.claim_receipt(conversation_id, message_id, now=datetime.now(UTC))


async def test_a_mention_and_a_thread_reply_from_the_gap_are_delivered_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    old_root = _graph("1700000000001", 30, "<p>planning</p>")
    old_root["replies"] = [
        _graph("1700000000020", 1, "<p>one more thing</p>", reply_to=old_root["id"])
    ]
    graph.channels[TEAM] = [old_root]
    graph.channels[CHANNEL_ID] = [
        _mention("1700000000010", 3, "what is up"),
        _graph("1700000000009", 40, "<p>too old</p>"),
    ]
    graph.channels[CHANNEL_ID][0]["replies"] = [
        _graph("1700000000011", 2, "<p>and tomorrow?</p>", reply_to="1700000000010"),
        _graph("1700000000012", 1, "<p>Here you go</p>", reply_to="1700000000010", sender=BOT),
    ]
    await _admitted_before(db_session_factory, TEAM)
    await _admitted_before(db_session_factory, CHANNEL_ID)

    boot = Boot(db_session_factory, graph)
    await boot.boot()

    assert boot.ran(CHANNEL_ID) == [
        ("turn", "1700000000010"),
        ("participation", "1700000000011"),
    ], "the root mention then its reply, in order; the bot's own message is skipped"
    assert boot.ran(TEAM) == [("participation", "1700000000020")], (
        "a reply under a root older than the window is a thread follow-up all the same"
    )
    mention = next(i for _, i in boot.paths if i.activity_id == "1700000000010")
    assert mention.text == "what is up", "the bot's own mention is dropped, as in Bot Framework"
    assert mention.conversation_id == f"{CHANNEL_ID};messageid=1700000000010"
    assert (mention.team_group_id, mention.channel_type, mention.channel_name) == (
        TEAM_GROUP_ID,
        "standard",
        "Ops",
    ), "the channel's team comes from the team's record"
    assert not mention.unprompted and mention.service_url == SERVICE_URL

    successor = Boot(db_session_factory, graph)
    await successor.boot()
    assert successor.paths == [], "the next boot finds them admitted"


async def test_a_message_already_admitted_is_not_run_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Keys match across sources: a root mention, a reply and a group chat message that Bot
    Framework delivered, or a predecessor held for replay, are not run a second time."""
    graph = FakeGraph()
    root, reply = "1700000000010", "1700000000011"
    graph.channels[CHANNEL_ID] = [_mention(root, 3)]
    graph.channels[CHANNEL_ID][0]["replies"] = [_graph(reply, 2, reply_to=root)]
    graph.chats[GROUP_CHAT_ID] = [_mention("1700000000030", 1)]

    predecessor = Boot(db_session_factory, graph, fence=False)
    thread = f"{CHANNEL_ID};messageid={root}"
    await predecessor.bot_framework(make_channel_activity(conversation_id=thread, activity_id=root))
    await predecessor.bot_framework(
        make_channel_activity(conversation_id=thread, activity_id=reply, mention_bot=False)
    )
    await predecessor.bot_framework(make_group_activity(activity_id="1700000000030"))
    assert len(predecessor.paths) == 3, "Bot Framework delivered all three before the restart"

    # The predecessor exited with the three unhandled: its rows are the successor's to replay.
    boot = Boot(db_session_factory, graph)
    await boot.boot()
    assert sorted(i.activity_id for _, i in boot.paths) == sorted([root, "1700000000030"]), (
        "the two mentions run once, from the reclaim; the reply was handled; the catch-up "
        "found all three admitted"
    )
    assert graph.read(CHANNEL_ID) and graph.read(GROUP_CHAT_ID), "and did read both"


async def test_a_message_handled_before_the_restart_is_not_run_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [_mention("1700000000010", 3)]
    await _admitted_before(db_session_factory, CHANNEL_ID, "1700000000010")

    boot = Boot(db_session_factory, graph)
    await boot.boot()
    assert boot.paths == [] and graph.read(CHANNEL_ID), "read, found admitted, and run nothing"


async def test_only_the_window_and_people_count(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [
        _mention("1700000000013", CATCH_UP_WINDOW.total_seconds() / 60 + 1),
        _mention("1700000000012", 1, sender=BOT),
        _graph("1700000000011", 1, "<p>a new topic</p>"),
        _mention("1700000000010", CATCH_UP_WINDOW.total_seconds() / 60 - 1),
    ]
    await _admitted_before(db_session_factory, CHANNEL_ID)

    boot = Boot(db_session_factory, graph)
    await boot.boot()
    assert boot.ran(CHANNEL_ID) == [("turn", "1700000000010")], (
        "not the older mention, not the bot's, not an untagged root post"
    )


async def test_a_group_chat_message_is_caught_up(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.chats[GROUP_CHAT_ID] = [  # newest first, as Graph lists a chat
        _graph("1700000000032", 1, "<p>lunch?</p>"),
        _mention("1700000000031", 2, "summarise this"),
        _mention("1700000000030", 20, "too old"),
    ]
    await _admitted_before(db_session_factory, GROUP_CHAT_ID)

    boot = Boot(db_session_factory, graph)
    await boot.boot()
    assert boot.ran(GROUP_CHAT_ID) == [("turn", "1700000000031")], "only the mention, in the window"
    [inbound] = [i for _, i in boot.paths]
    assert (inbound.kind, inbound.conversation_id, inbound.text) == (
        "group",
        GROUP_CHAT_ID,
        "summarise this",
    )
    [listing] = graph.read(GROUP_CHAT_ID)
    assert listing.url.params["$orderby"] == "createdDateTime desc", "one page of the newest"


async def test_a_one_to_one_chat_is_not_read(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Graph cannot read a 1:1 chat back, so there is nothing to attempt."""
    graph = FakeGraph()
    boot = Boot(db_session_factory, graph)
    await boot.bot_framework(
        make_message_activity(conversation_id=DIRECT, activity_id="1700000000040")
    )
    boot.paths.clear()
    await boot.boot()
    assert graph.requests == [] and boot.paths == [], "no read, nothing run"


async def test_a_graph_failure_for_one_conversation_does_not_stop_the_others(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.refused = {CHANNEL_ID}
    graph.channels[OTHER_CHANNEL] = [_mention("1700000000010", 3)]
    graph.chats[GROUP_CHAT_ID] = [_mention("1700000000030", 1)]
    for conversation in (CHANNEL_ID, OTHER_CHANNEL, GROUP_CHAT_ID):
        await _admitted_before(db_session_factory, conversation)

    boot = Boot(db_session_factory, graph)
    with structlog.testing.capture_logs() as logs:
        await boot.boot()
    assert sorted(i.activity_id for _, i in boot.paths) == ["1700000000010", "1700000000030"], (
        "the channel that answered 403 is skipped, the rest are delivered"
    )
    [failed] = [e for e in logs if e["event"] == "teams.restart_catch_up.read_failed"]
    assert failed["status"] == 403
    [done] = [e for e in logs if e["event"] == "teams.restart_catch_up.done"]
    assert (done["conversations"], done["messages"]) == (3, 2), "the count is logged"


async def test_a_subscribed_or_unlisted_channel_is_left_to_its_own_catch_up(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [_mention("1700000000010", 3)]
    graph.channels[PRIVATE] = [_mention("1700000000050", 3)]
    graph.channels[OTHER_CHANNEL] = [_mention("1700000000060", 3)]
    async with db_session_factory.begin() as session:
        await insert_teams_channel_subscription(
            session,
            tenant_id=TENANT,
            channel_id=CHANNEL_ID,
            team_id=TEAM,
            group_id=TEAM_GROUP_ID,
            channel_type="shared",
            channel_name="Shared",
            team_name=None,
            bot_name="Daimon",
            service_url=None,
            subscription_id="sub-1",
            client_state_sha256=b"h" * 32,
            expires_at=datetime.now(UTC) + timedelta(days=3),
        )
    for conversation in (CHANNEL_ID, PRIVATE, OTHER_CHANNEL):
        await _admitted_before(db_session_factory, conversation)

    boot = Boot(db_session_factory, graph, standard={OTHER_CHANNEL: "Dev"})
    await boot.boot()
    assert graph.read(CHANNEL_ID) == [], "a subscribed channel is read by its subscription"
    assert graph.read(PRIVATE) == [], "a channel no team lists as standard cannot be read here"
    assert boot.ran(OTHER_CHANNEL) == [("turn", "1700000000060")], "a standard one is"


async def test_the_most_recently_active_conversations_are_read_up_to_a_cap(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [_mention("1700000000010", 3)]
    graph.channels[OTHER_CHANNEL] = [_mention("1700000000060", 3)]
    await _admitted_before(db_session_factory, CHANNEL_ID)
    await _admitted_before(db_session_factory, OTHER_CHANNEL)
    monkeypatch.setattr(restart_catch_up, "MAX_CONVERSATIONS", 1)

    boot = Boot(db_session_factory, graph)
    await boot.boot()
    assert [i.activity_id for _, i in boot.paths] == ["1700000000060"], "only the newest is read"
    assert graph.read(CHANNEL_ID) == []


async def test_nothing_is_admitted_once_the_process_drains(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.chats[GROUP_CHAT_ID] = [_mention("1700000000030", 1)]
    await _admitted_before(db_session_factory, GROUP_CHAT_ID)

    draining = Boot(db_session_factory, graph)
    draining.app.draining = True
    await draining.boot()
    assert draining.paths == [] and graph.requests == [], (
        "a draining process reads and admits nothing"
    )

    successor = Boot(db_session_factory, graph)
    await successor.boot()
    assert successor.ran(GROUP_CHAT_ID) == [("turn", "1700000000030")], (
        "and, as nothing was claimed, the next boot still reads it"
    )


async def test_a_refused_mention_is_answered_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    unverified = {"user": {"id": "not-a-uuid", "displayName": "Ada"}}
    graph.chats[GROUP_CHAT_ID] = [_mention("1700000000030", 1, sender=unverified)]
    await _admitted_before(db_session_factory, GROUP_CHAT_ID)

    sender = FakeSender()
    await Boot(db_session_factory, graph, sender=sender).boot()
    await Boot(db_session_factory, graph, sender=sender).boot()
    assert [(c, shown_text(a)) for c, a, _ in sender.sent] == [(GROUP_CHAT_ID, DENIED)], (
        "answered in the chat, and not again by the next boot"
    )


async def test_a_message_after_the_service_began_serving_is_left_to_bot_framework(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bot Framework delivers it live, with its full parse; Graph's must not claim first."""
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [
        _mention("1700000000012", 1),
        _mention("1700000000011", 3),
    ]
    await _admitted_before(db_session_factory, CHANNEL_ID)

    boot = Boot(db_session_factory, graph, serving_at=datetime.now(UTC) - timedelta(minutes=2))
    await boot.boot()
    assert boot.ran(CHANNEL_ID) == [("turn", "1700000000011")], "only what came before serving"
    await boot.bot_framework(
        make_channel_activity(
            conversation_id=f"{CHANNEL_ID};messageid=1700000000012", activity_id="1700000000012"
        )
    )
    assert boot.ran(CHANNEL_ID)[-1] == ("turn", "1700000000012"), "and the live one still runs"


async def test_a_typed_bot_name_in_the_gap_is_a_mention_when_the_switch_is_on(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The bot's name comes from its own earlier message in what was read."""
    async with db_session_factory.begin() as session:
        await set_plain_text_mentions(session, tenant_id=TENANT, enabled=True)
    graph = FakeGraph()
    thread = _graph("1700000000001", 30, "<p>planning</p>")
    thread["replies"] = [
        _graph("1700000000002", 25, "<p>Noted</p>", reply_to=thread["id"], sender=BOT),
        _graph("1700000000003", 1, "<p>@Daimon what changed?</p>", reply_to=thread["id"]),
    ]
    graph.channels[CHANNEL_ID] = [
        _graph("1700000000004", 2, "<p>@Daimon summarise the week</p>"),
        thread,
    ]
    await _admitted_before(db_session_factory, CHANNEL_ID)

    boot = Boot(db_session_factory, graph)
    await boot.boot()
    # Different threads run side by side: only each thread's own order is kept.
    assert sorted(boot.ran(CHANNEL_ID)) == [("turn", "1700000000003"), ("turn", "1700000000004")], (
        "a root post and a thread reply that name the bot run as mentions"
    )
    assert sorted(i.text for _, i in boot.paths) == ["summarise the week", "what changed?"]


async def test_a_typed_bot_name_is_not_known_when_nothing_read_names_the_bot(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory.begin() as session:
        await set_plain_text_mentions(session, tenant_id=TENANT, enabled=True)
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [_graph("1700000000004", 2, "<p>@Daimon summarise</p>")]
    await _admitted_before(db_session_factory, CHANNEL_ID)

    boot = Boot(db_session_factory, graph)
    await boot.boot()
    assert boot.paths == [], "without the bot's name a typed one is just chatter"


async def test_a_channel_of_an_older_team_is_caught_up(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    old = "19:old-channel@thread.skype"
    graph = FakeGraph()
    graph.channels[old] = [_mention("1700000000010", 3)]
    await _admitted_before(db_session_factory, old)

    boot = Boot(db_session_factory, graph, standard={old: "Legacy"})
    await boot.boot()
    assert boot.ran(old) == [("turn", "1700000000010")]


async def test_one_conversations_unexpected_failure_leaves_the_others_and_claimed_rows(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [_mention("1700000000010", 3), _mention("1700000000011", 2)]
    graph.channels[OTHER_CHANNEL] = [_mention("1700000000060", 3)]
    graph.chats[GROUP_CHAT_ID] = [_mention("1700000000030", 1)]
    for conversation in (CHANNEL_ID, OTHER_CHANNEL, GROUP_CHAT_ID):
        await _admitted_before(db_session_factory, conversation)

    boot = Boot(db_session_factory, graph)
    admit = TeamsApp.catch_up_graph_messages
    parse = TeamsApp._parse_graph  # pyright: ignore[reportPrivateUsage]

    async def failing_channel(self: TeamsApp, source: GraphSource, messages: list[Any]) -> None:
        if source.channel_id == OTHER_CHANNEL:
            raise RuntimeError("boom")
        await admit(self, source, messages)

    def failing_message(self: TeamsApp, message: GraphMessage, *args: Any, **kwargs: Any) -> Any:
        if message.id == "1700000000010":
            raise RuntimeError("boom")
        return parse(self, message, *args, **kwargs)

    with (
        patch.object(TeamsApp, "catch_up_graph_messages", failing_channel),
        patch.object(TeamsApp, "_parse_graph", failing_message),
        structlog.testing.capture_logs() as logs,
    ):
        await boot.boot()
    assert sorted(i.activity_id for _, i in boot.paths) == ["1700000000011", "1700000000030"], (
        "a failing conversation and a failing message leave the rest running"
    )
    events = [e["event"] for e in logs]
    assert "teams.restart_catch_up.deliver_failed" in events
    assert "teams.restart_catch_up.message_failed" in events


async def test_a_privacy_delete_keeps_the_guard_so_the_gap_read_runs_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ada's mention is answered, she deletes her data, a restart follows: the erased words
    must not come back through a catch-up of that same message."""
    graph = FakeGraph()
    graph.channels[CHANNEL_ID] = [_mention("1700000000010", 3)]
    first = Boot(db_session_factory, graph)
    await first.bot_framework(
        make_channel_activity(
            conversation_id=f"{CHANNEL_ID};messageid=1700000000010", activity_id="1700000000010"
        )
    )
    assert [p for p, _ in first.paths] == ["turn"], "answered"
    async with db_session_factory.begin() as session:
        forgotten = await forget_teams_messages_for_user(
            session, tenant_id=TENANT, user_id=AAD_OBJECT_ID
        )
    assert forgotten == 1

    successor = Boot(db_session_factory, graph)
    await successor.boot()
    assert graph.read(CHANNEL_ID), "the channel was read back"
    assert successor.paths == [], "and the mention is still known as answered"
