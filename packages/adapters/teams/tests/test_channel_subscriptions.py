"""Graph subscriptions to private and shared channels: creation, renewal, lifecycle, ingress.

Graph is a `MockTransport` (`FakeGraph`) and the store the real test database;
the HTTP tests go through the real FastAPI service.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.channel_subscriptions import (
    LIFECYCLE_PATH,
    NOTIFICATIONS_PATH,
    RENEW_MARGIN,
    RETRY_DELAY,
    ChannelSubscriptions,
    SeenChannel,
    channel_resource,
    client_state_hash,
    client_state_matches,
    parse_resource,
    renewal_due_at,
    seconds_until,
)
from daimon.adapters.teams.http_service import TeamsHttpService, create_teams_http_service
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import TeamsChannelSubscriptionRow
from daimon.core.stores.teams_channel_subscriptions import (
    get_teams_channel_subscription,
    record_teams_channel_watermark,
    upsert_teams_channel_subscription,
)
from daimon.core.teams_graph import (
    ChangeNotification,
    GraphClient,
    GraphMessage,
    GraphUnavailable,
    TeamGroups,
)
from microsoft_teams.api import (
    ConversationUpdateActivity,
    MessageActivityInput,
    UninstalledActivity,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    ENTRA_TENANT_ID,
    TEAM_GROUP_ID,
    FakeSender,
    TeamsApiFake,
    bot_token,
    build_teams_client,
    build_teams_runtime,
    make_channel_activity,
    post_activity,
    running_service,
    teams_settings,
)

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
PRIVATE = "19:private-1@thread.tacv2"
TEAM = "19:team@thread.tacv2"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
PUBLIC_URL = "https://bot.example"
SECRET = "the-client-state"
CHANNEL = SeenChannel(
    team_id=TEAM,
    team_group_id=TEAM_GROUP_ID,
    channel_id=PRIVATE,
    channel_type="private",
    channel_name="Secret",
    bot_name="Daimon",
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")


def _message(id: str, created: datetime, *, reply_to: str | None = None) -> dict[str, Any]:
    return {
        "id": id,
        "messageType": "message",
        "createdDateTime": created.isoformat().replace("+00:00", "Z"),
        "replyToId": reply_to,
        "from": {"user": {"id": AAD_OBJECT_ID, "displayName": "Ada"}},
        "body": {"contentType": "html", "content": "<p>hi</p>"},
    }


class FakeGraph:
    """Graph's subscription and channel-message endpoints, recording each request.

    `status` answers a method with that status instead; `messages` are what
    message reads find, `page` what a channel listing returns.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.page: list[dict[str, Any]] = []
        self._ids = 0

    def calls(self, method: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.method == method]

    def client_state(self) -> str:
        return json.loads(self.calls("POST")[-1].content)["clientState"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (status := self.status.get(request.method)) is not None:
            return httpx.Response(status)
        path = request.url.path
        if request.method == "POST" and path == "/v1.0/subscriptions":
            self._ids += 1
            expiry = json.loads(request.content)["expirationDateTime"]
            return httpx.Response(
                201, json={"id": f"sub-{self._ids}", "expirationDateTime": expiry}
            )
        if request.method == "PATCH":
            expiry = json.loads(request.content)["expirationDateTime"]
            sub = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": sub, "expirationDateTime": expiry})
        if request.method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/messages"):
            return httpx.Response(200, json={"value": self.page})
        found = self.messages.get(path.rsplit("/", 1)[1])
        return httpx.Response(200, json=found) if found else httpx.Response(404)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _graph_token() -> str:
    return "graph-token"


async def _no_group(_team_id: str) -> str | None:
    return None


class FlakyDatabase:
    """The test sessionmaker, refusing reads or writes while told to, as a lost database would."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory
        self.fail_reads = False
        self.fail_writes = False

    def __call__(self) -> AsyncSession:
        if self.fail_reads:
            raise OperationalError("SELECT", None, ConnectionError("database down"))
        return self._factory()

    def begin(self) -> Any:
        if self.fail_writes:
            raise OperationalError("UPDATE", None, ConnectionError("database down"))
        return self._factory.begin()


Deliver = Callable[[GraphMessage, TeamsChannelSubscriptionRow], Awaitable[bool]]


class Harness:
    """`ChannelSubscriptions` over the test DB and `FakeGraph`, with what it delivered.

    `member` is what the membership check answers, or the error it raises;
    `taking` is what delivery answers (False while the app drains).
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        graph: FakeGraph,
        *,
        channel_type: str | None = "private",
        deliver: Deliver | None = None,
    ) -> None:
        self.delivered: list[GraphMessage] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.now = NOW
        self.member: bool | Exception = True
        self.membership_checks: list[str] = []
        self.taking = True
        self.database = FlakyDatabase(factory)

        async def lookup(_team: str, _channel: str) -> str | None:
            return channel_type

        async def is_member(channel_id: str) -> bool:
            self.membership_checks.append(channel_id)
            if isinstance(self.member, Exception):
                raise self.member
            return self.member

        async def record(message: GraphMessage, _row: TeamsChannelSubscriptionRow) -> bool:
            if self.taking:
                self.delivered.append(message)
            return self.taking

        def spawn(coro: Any, *, name: str) -> asyncio.Task[None]:
            task = asyncio.create_task(coro, name=name)
            self.tasks.append(task)
            return task

        self.subscriptions = ChannelSubscriptions(
            sessionmaker=self.database,  # pyright: ignore[reportArgumentType]
            graph=GraphClient(graph.http(), _graph_token),
            groups=TeamGroups(_no_group),
            channel_type=lookup,
            is_member=is_member,
            deliver=deliver or record,
            spawn=spawn,
            tenant_id=TENANT,
            public_url=PUBLIC_URL,
            now=lambda: self.now,
            clock=lambda: 1000.0,
            new_secret=lambda: SECRET,
        )
        self._factory = factory

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks)

    async def row(self, channel_id: str = PRIVATE) -> TeamsChannelSubscriptionRow | None:
        async with self._factory() as session:
            return await get_teams_channel_subscription(
                session, tenant_id=TENANT, channel_id=channel_id
            )


async def _stored(
    factory: async_sessionmaker[AsyncSession],
    *,
    expires_at: datetime,
    channel_id: str = PRIVATE,
    subscription_id: str = "sub-0",
) -> None:
    """A subscription of `channel_id` in `TEAM` whose client state is `SECRET`."""
    async with factory.begin() as session:
        await upsert_teams_channel_subscription(
            session,
            tenant_id=TENANT,
            channel_id=channel_id,
            team_id=TEAM,
            group_id=TEAM_GROUP_ID,
            channel_type="private",
            channel_name="Secret",
            team_name=None,
            bot_name="Daimon",
            service_url=None,
            subscription_id=subscription_id,
            client_state_sha256=client_state_hash(SECRET),
            expires_at=expires_at,
        )


def _notice(**fields: Any) -> ChangeNotification:
    return ChangeNotification.model_validate(
        {"subscriptionId": "sub-0", "clientState": SECRET} | fields
    )


def test_a_resource_names_a_post_or_a_reply_under_its_root() -> None:
    post = parse_resource(f"teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('100')")
    reply = parse_resource(
        f"/teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('100')/replies('101')"
    )
    assert post is not None and (post.message_id, post.root_id) == ("100", None)
    assert reply is not None and (reply.message_id, reply.root_id) == ("101", "100"), (
        "a reply is read under its root"
    )
    assert parse_resource("users('x')/messages('1')") is None, "only channel messages"


def test_a_subscription_resource_refuses_ids_that_could_move_it() -> None:
    assert channel_resource(TEAM_GROUP_ID.upper(), PRIVATE) == (
        f"/teams/{TEAM_GROUP_ID}/channels/{PRIVATE}/messages"
    )
    for group, channel in ((TEAM_GROUP_ID, "19:x/../../users"), ("not-a-group", PRIVATE)):
        with pytest.raises(GraphUnavailable, match="not a"):
            channel_resource(group, channel)


def test_client_state_is_compared_against_its_hash() -> None:
    stored = client_state_hash(SECRET)
    assert client_state_matches(stored, SECRET)
    assert not client_state_matches(stored, "guess") and not client_state_matches(stored, None)
    assert SECRET.encode() not in stored, "only the hash is kept"


def test_renewal_is_due_a_margin_before_expiry_and_waits_out_a_retry() -> None:
    expiry = NOW + timedelta(days=3)
    assert renewal_due_at(expiry) == expiry - RENEW_MARGIN
    retry = expiry - RENEW_MARGIN + RETRY_DELAY
    assert renewal_due_at(expiry, not_before=retry) == retry, "a failed renewal waits to retry"
    assert renewal_due_at(expiry, not_before=NOW) == expiry - RENEW_MARGIN
    due = [NOW + timedelta(hours=5), NOW + timedelta(hours=2)]
    assert seconds_until(due, NOW) == 2 * 3600, "it sleeps until the earliest"
    assert seconds_until([NOW - timedelta(hours=1)], NOW) == 0.0, "an overdue one runs now"
    assert seconds_until([], NOW) is None, "with nothing to renew it sleeps until woken"


async def test_the_first_sight_of_a_private_channel_subscribes_it_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    harness.subscriptions.notice(CHANNEL)
    harness.subscriptions.notice(CHANNEL)
    await harness.settle()
    await harness.subscriptions.ensure(CHANNEL)

    [create] = graph.calls("POST")
    body = json.loads(create.content)
    assert body["resource"] == f"/teams/{TEAM_GROUP_ID}/channels/{PRIVATE}/messages"
    assert body["notificationUrl"] == PUBLIC_URL + NOTIFICATIONS_PATH
    assert body["lifecycleNotificationUrl"] == PUBLIC_URL + LIFECYCLE_PATH
    row = await harness.row()
    assert row is not None and row.subscription_id == "sub-1", "the subscription is recorded"
    assert client_state_matches(row.client_state_sha256, body["clientState"]), (
        "the stored hash verifies the secret Graph was given"
    )
    assert row.expires_at == NOW + timedelta(minutes=4260), "Graph's expiry, under its ceiling"


async def test_a_standard_channel_is_never_subscribed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph, channel_type="standard")
    harness.subscriptions.notice(
        SeenChannel(team_id=TEAM, team_group_id=None, channel_id=PRIVATE, channel_type=None)
    )
    await harness.settle()
    assert graph.requests == [], "standard channels get every post from Bot Framework"


async def test_a_refused_subscription_is_logged_and_not_retried_at_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.status["POST"] = 403
    harness = Harness(db_session_factory, graph)
    with structlog.testing.capture_logs() as logs:
        harness.subscriptions.notice(CHANNEL)
        await harness.settle()
    [failed] = [entry for entry in logs if entry["event"].endswith("create_failed")]
    assert failed["status"] == 403, "the missing consent shows as a 403"
    harness.subscriptions.notice(CHANNEL)
    assert harness.tasks[1:] == [] and len(graph.calls("POST")) == 1, "backed off, not retried"
    assert await harness.row() is None


async def test_removing_the_bot_deletes_the_row_and_the_graph_subscription(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    await harness.subscriptions.ensure(CHANNEL)
    activity = ConversationUpdateActivity.model_validate(
        {
            "type": "conversationUpdate",
            "id": "u-1",
            "channelId": "msteams",
            "serviceUrl": "https://smba.example/",
            "from": {"id": f"29:{AAD_OBJECT_ID}"},
            "recipient": {"id": BOT_ACCOUNT_ID, "name": "Daimon"},
            "conversation": {"id": PRIVATE, "conversationType": "channel"},
            "membersRemoved": [{"id": BOT_ACCOUNT_ID}],
            "channelData": {"channel": {"id": PRIVATE}, "team": {"id": TEAM}},
        }
    )
    ctx = AsyncMock()
    ctx.activity = activity
    await harness.subscriptions.on_conversation_update(ctx)

    assert [r.url.path for r in graph.calls("DELETE")] == ["/v1.0/subscriptions/sub-1"]
    assert await harness.row() is None, "the row goes with the bot"
    ctx.next.assert_awaited_once()


async def test_a_new_message_is_read_once_delivered_and_marks_the_watermark(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.messages["101"] = _message("101", NOW, reply_to="100")
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    row = await harness.row()
    assert row is not None
    resource = f"teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('100')/replies('101')"

    await harness.subscriptions.on_change(_notice(changeType="updated", resource=resource), row)
    assert graph.requests == [] and harness.delivered == [], "an edit never runs anything again"
    await harness.subscriptions.on_change(_notice(changeType="created", resource=resource), row)

    [read] = graph.calls("GET")
    assert read.url.path.endswith("/messages/100/replies/101"), "one GET, under the root"
    assert [message.id for message in harness.delivered] == ["101"]
    stored = await harness.row()
    assert stored is not None and stored.last_message_id == "101", "catch-up starts after it"


async def test_lifecycle_notices_renew_recreate_and_catch_up(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(hours=1))
    row = await harness.row()
    assert row is not None

    await harness.subscriptions.on_lifecycle(_notice(lifecycleEvent="reauthorizationRequired"), row)
    assert [r.url.path for r in graph.calls("PATCH")] == ["/v1.0/subscriptions/sub-0"]
    renewed = await harness.row()
    assert renewed is not None and renewed.expires_at == NOW + timedelta(minutes=4260)

    await harness.subscriptions.on_lifecycle(_notice(lifecycleEvent="subscriptionRemoved"), row)
    recreated = await harness.row()
    assert recreated is not None and recreated.subscription_id == "sub-1", "a new subscription"
    assert client_state_matches(recreated.client_state_sha256, graph.client_state()), (
        "with a fresh secret"
    )


async def test_missed_reads_only_what_came_after_the_watermark(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    old = _message("100", NOW - timedelta(minutes=5))
    old["replies"] = [_message("102", NOW + timedelta(minutes=2), reply_to="100")]
    graph.page = [_message("103", NOW + timedelta(minutes=3)), old]
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    async with db_session_factory.begin() as session:
        await record_teams_channel_watermark(
            session, tenant_id=TENANT, channel_id=PRIVATE, message_id="101", at=NOW
        )
    row = await harness.row()
    assert row is not None

    await harness.subscriptions.on_lifecycle(_notice(lifecycleEvent="missed"), row)
    assert [m.id for m in harness.delivered] == ["102", "103"], (
        "only newer messages, oldest first; the handled root is not delivered again"
    )
    [listing] = graph.calls("GET")
    assert listing.url.params["$expand"] == "replies", "one page read, replies included"


async def test_the_renewal_task_renews_what_is_due_then_sleeps(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(hours=1))
    task = asyncio.create_task(harness.subscriptions.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if graph.calls("PATCH"):
                break
        await asyncio.sleep(0.05)
        assert len(graph.calls("PATCH")) == 1, "renewed once, then asleep until the next is due"
        assert not task.done(), "the task sleeps rather than exits"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_stale_row_is_not_renewed_or_recreated_after_forget(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row read before `forget` took the lock must not bring the channel back."""
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(hours=1))
    stale = await harness.row()
    assert stale is not None
    await harness.subscriptions.forget(channel_id=PRIVATE)

    await harness.subscriptions.on_lifecycle(
        _notice(lifecycleEvent="reauthorizationRequired"), stale
    )
    await harness.subscriptions.on_lifecycle(_notice(lifecycleEvent="subscriptionRemoved"), stale)
    assert graph.calls("PATCH") == [] and graph.calls("POST") == [], "nothing renewed or created"
    assert await harness.row() is None, "the forgotten channel stays forgotten"


async def test_a_repeated_subscription_removed_notice_recreates_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The second notice names the replaced subscription: acting on it would orphan one."""
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    row = await harness.row()
    assert row is not None
    removed = _notice(lifecycleEvent="subscriptionRemoved")
    await asyncio.gather(
        harness.subscriptions.on_lifecycle(removed, row),
        harness.subscriptions.on_lifecycle(removed, row),
    )
    assert len(graph.calls("POST")) == 1, "one replacement, no orphan"
    current = await harness.row()
    assert current is not None and current.subscription_id == "sub-1"


@pytest.mark.parametrize("event", ["reauthorizationRequired", "subscriptionRemoved"])
async def test_a_channel_the_bot_left_loses_its_subscription_on_refresh(
    db_session_factory: async_sessionmaker[AsyncSession], event: str
) -> None:
    """The removal event was lost: the membership check on refresh catches it."""
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    harness.member = False
    await _stored(db_session_factory, expires_at=NOW + timedelta(hours=1))
    row = await harness.row()
    assert row is not None

    await harness.subscriptions.on_lifecycle(_notice(lifecycleEvent=event), row)
    assert graph.calls("PATCH") == [] and graph.calls("POST") == [], "neither renewed nor recreated"
    assert [r.url.path for r in graph.calls("DELETE")] == ["/v1.0/subscriptions/sub-0"]
    assert await harness.row() is None, "the row is dropped"


async def test_a_failed_membership_lookup_keeps_and_renews_the_subscription(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    harness.member = httpx.ConnectError("unreachable")
    await _stored(db_session_factory, expires_at=NOW + timedelta(hours=1))
    row = await harness.row()
    assert row is not None
    await harness.subscriptions.on_lifecycle(_notice(lifecycleEvent="reauthorizationRequired"), row)
    assert len(graph.calls("PATCH")) == 1, "an unknown answer never drops a channel"
    assert await harness.row() is not None


async def test_a_channel_send_refused_with_403_drops_a_channel_the_bot_left(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    harness.member = False
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))

    class Refusing:
        async def send(self, conversation_id: str, activity: Any, *, service_url: Any) -> Any:
            request = httpx.Request("POST", "https://smba.example/v3/conversations")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError(
                "BotNotInConversationRoster", request=request, response=response
            )

    sender = harness.subscriptions.watch(Refusing())
    with pytest.raises(httpx.HTTPStatusError):
        await sender.send(
            f"{PRIVATE};messageid=1", MessageActivityInput(text="hi"), service_url=None
        )
    await harness.settle()
    assert harness.membership_checks == [PRIVATE], "the refusal asks whether the bot left"
    assert await harness.row() is None and len(graph.calls("DELETE")) == 1, "it had: dropped"


async def test_catch_up_overlapping_a_live_notification_runs_each_message_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both routes hand the same message to the app; its delivery claim runs it once."""
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=FakeSender(),
        commands={},
        bot_token=bot_token,
    )
    observed: list[str] = []

    async def observe(_self: TeamsApp, inbound: Any) -> None:
        observed.append(inbound.activity_id)

    graph = FakeGraph()
    graph.messages["102"] = _message("102", NOW + timedelta(minutes=2), reply_to="100")
    graph.page = [_message("103", NOW + timedelta(minutes=3), reply_to="100")]
    graph.page[0]["replies"] = []
    root = _message("100", NOW - timedelta(minutes=5))
    root["replies"] = [graph.messages["102"]]
    graph.page.append(root)
    harness = Harness(db_session_factory, graph, deliver=teams.handle_graph_message)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    async with db_session_factory.begin() as session:
        await record_teams_channel_watermark(
            session, tenant_id=TENANT, channel_id=PRIVATE, message_id="101", at=NOW
        )
    row = await harness.row()
    assert row is not None
    resource = f"teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('100')/replies('102')"

    with patch.object(TeamsApp, "_observe", observe):
        # The live read lands first; the catch-up, from the watermark it read before, sees it too.
        await harness.subscriptions.on_change(_notice(changeType="created", resource=resource), row)
        await harness.subscriptions.on_lifecycle(_notice(lifecycleEvent="missed"), row)
        await asyncio.gather(*teams._tasks)  # pyright: ignore[reportPrivateUsage]
    assert sorted(observed) == ["102", "103"], "each message runs once"


async def test_a_message_not_taken_while_draining_leaves_the_watermark(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.messages["102"] = _message("102", NOW + timedelta(minutes=2), reply_to="100")
    harness = Harness(db_session_factory, graph)
    harness.taking = False
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    row = await harness.row()
    assert row is not None
    resource = f"teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('100')/replies('102')"
    await harness.subscriptions.on_change(_notice(changeType="created", resource=resource), row)
    stored = await harness.row()
    assert stored is not None and stored.last_message_id is None, (
        "a catch-up after the restart still reads it"
    )


async def test_a_failed_watermark_write_does_not_stop_the_batch(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.page = [
        _message("103", NOW + timedelta(minutes=3)),
        _message("102", NOW + timedelta(minutes=2)),
    ]
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    async with db_session_factory.begin() as session:
        await record_teams_channel_watermark(
            session, tenant_id=TENANT, channel_id=PRIVATE, message_id="101", at=NOW
        )
    row = await harness.row()
    assert row is not None
    harness.database.fail_writes = True
    with structlog.testing.capture_logs() as logs:
        await harness.subscriptions.catch_up(row)
    assert [m.id for m in harness.delivered] == ["102", "103"], "every message is still handled"
    assert [e for e in logs if e["event"] == "teams.channel_watermark.write_failed"], "logged"


async def test_a_failed_read_in_ensure_lets_the_next_sight_try_again(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    harness.database.fail_reads = True
    harness.subscriptions.notice(CHANNEL)
    await harness.settle()
    assert graph.calls("POST") == [], "nothing created without the read"
    harness.database.fail_reads = False
    harness.subscriptions.notice(CHANNEL)
    await harness.settle()
    assert len(graph.calls("POST")) == 1 and await harness.row() is not None, "subscribed now"


async def test_a_subscription_that_cannot_be_recorded_is_deleted_and_retried(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    harness.database.fail_writes = True
    harness.subscriptions.notice(CHANNEL)
    await harness.settle()
    assert [r.url.path for r in graph.calls("DELETE")] == ["/v1.0/subscriptions/sub-1"], (
        "the unrecorded subscription is not left to run unrenewed"
    )
    harness.database.fail_writes = False
    harness.subscriptions.notice(CHANNEL)
    await harness.settle()
    row = await harness.row()
    assert row is not None and row.subscription_id == "sub-2", "the next sight subscribes"


def _removal(kind: str, channel_id: str) -> AsyncMock:
    """A handler context for the bot's removal: a conversation update or an uninstall."""
    payload: dict[str, Any] = {
        "id": "u-1",
        "channelId": "msteams",
        "serviceUrl": "https://smba.example/",
        "from": {"id": f"29:{AAD_OBJECT_ID}"},
        "recipient": {"id": BOT_ACCOUNT_ID, "name": "Daimon"},
        "conversation": {"id": channel_id, "conversationType": "channel"},
        "channelData": {"channel": {"id": channel_id}, "team": {"id": TEAM}},
    }
    ctx = AsyncMock()
    if kind == "update":
        payload |= {"type": "conversationUpdate", "membersRemoved": [{"id": BOT_ACCOUNT_ID}]}
        ctx.activity = ConversationUpdateActivity.model_validate(payload)
    else:
        payload |= {"type": "installationUpdate", "action": "remove"}
        ctx.activity = UninstalledActivity.model_validate(payload)
    return ctx


@pytest.mark.parametrize("kind", ["update", "uninstall"])
async def test_a_failed_forget_still_passes_the_activity_on(
    db_session_factory: async_sessionmaker[AsyncSession], kind: str
) -> None:
    """The handlers after it (the installs record) must run whatever the DB says."""
    harness = Harness(db_session_factory, FakeGraph())
    harness.database.fail_writes = True
    ctx = _removal(kind, PRIVATE)
    with structlog.testing.capture_logs() as logs:
        if kind == "update":
            await harness.subscriptions.on_conversation_update(ctx)
        else:
            await harness.subscriptions.on_uninstall(ctx, ours=True)
    ctx.next.assert_awaited_once()
    assert [e for e in logs if e["event"] == "teams.channel_subscription.forget_failed"]


async def test_an_uninstall_from_one_channel_leaves_the_teams_other_channels(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    other = "19:private-2@thread.tacv2"
    graph = FakeGraph()
    harness = Harness(db_session_factory, graph)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    await _stored(
        db_session_factory,
        expires_at=NOW + timedelta(days=3),
        channel_id=other,
        subscription_id="s2",
    )

    await harness.subscriptions.on_uninstall(_removal("uninstall", PRIVATE), ours=True)
    assert await harness.row() is None, "the channel it left is dropped"
    assert await harness.row(other) is not None, "the team's other channels keep theirs"

    await harness.subscriptions.on_uninstall(_removal("uninstall", TEAM), ours=True)
    assert await harness.row(other) is None, "a team uninstall (General) drops them all"


# --- HTTP ingress -----------------------------------------------------------------


@pytest.fixture
def no_renewals() -> Iterator[None]:
    """The renewal task would share the test connection with the request under test."""
    with patch.object(ChannelSubscriptions, "run", new_callable=AsyncMock):
        yield


def _service(
    factory: async_sessionmaker[AsyncSession], graph: FakeGraph, *, on: bool
) -> TeamsHttpService:
    settings = teams_settings(public_url=PUBLIC_URL)
    runtime = build_teams_runtime(
        factory, teams=settings, http_client=graph.http(), private_channel_messages=on
    )
    return create_teams_http_service(
        settings=settings, runtime=runtime, client=build_teams_client(TeamsApiFake())
    )


async def _post(service: TeamsHttpService, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, **kwargs)


@pytest.mark.usefixtures("entra_env", "stub_bot_token", "no_renewals")
async def test_graph_validation_echoes_the_token_as_plain_text(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = _service(db_session_factory, FakeGraph(), on=True)
    for path in (NOTIFICATIONS_PATH, LIFECYCLE_PATH):
        response = await _post(service, path, params={"validationToken": "a b&c"})
        assert response.status_code == 200 and response.text == "a b&c", "verbatim"
        assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.usefixtures("entra_env", "stub_bot_token", "no_renewals")
async def test_a_notification_with_the_wrong_client_state_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    graph = FakeGraph()
    graph.messages["100"] = _message("100", NOW)
    service = _service(db_session_factory, graph, on=True)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    resource = f"teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('100')"
    entry = {"subscriptionId": "sub-0", "changeType": "created", "resource": resource}

    forged = await _post(
        service, NOTIFICATIONS_PATH, json={"value": [entry | {"clientState": "guess"}]}
    )
    assert forged.status_code == 401 and graph.requests == [], "nothing is read for a forgery"
    with patch.object(TeamsApp, "handle_graph_message", new_callable=AsyncMock) as handled:
        genuine = await _post(
            service, NOTIFICATIONS_PATH, json={"value": [entry | {"clientState": SECRET}]}
        )
        await asyncio.gather(*service.turns._tasks)  # pyright: ignore[reportPrivateUsage]
    assert genuine.status_code == 202, "accepted, then handled in the background"
    [(message, _row)] = [call.args for call in handled.await_args_list]
    assert message.id == "100", "the message is read and handed to the adapter"


def _private_mention() -> dict[str, object]:
    payload = make_channel_activity(conversation_id=f"{PRIVATE};messageid=1700000000001")
    channel_data = {
        "tenant": {"id": ENTRA_TENANT_ID},
        "channel": {"id": PRIVATE, "type": "private", "name": "Secret"},
        "team": {"id": TEAM, "aadGroupId": TEAM_GROUP_ID},
    }
    return payload | {"channelData": channel_data}


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.usefixtures("entra_env", "stub_bot_token", "no_renewals")
async def test_a_private_channel_mention_subscribes_only_with_the_setting_on(
    db_session_factory: async_sessionmaker[AsyncSession], on: bool
) -> None:
    """Off: the routes are absent (404) and a private channel is never subscribed."""
    graph = FakeGraph()
    settings = teams_settings(public_url=PUBLIC_URL)
    runtime = build_teams_runtime(
        db_session_factory, teams=settings, http_client=graph.http(), private_channel_messages=on
    )
    with patch.object(TeamsApp, "_handle", new_callable=AsyncMock):
        async with running_service(runtime, TeamsApiFake()) as service:
            await post_activity(service, _private_mention())
            await asyncio.gather(*service.turns._tasks)  # pyright: ignore[reportPrivateUsage]
            for path in (NOTIFICATIONS_PATH, LIFECYCLE_PATH):
                response = await _post(service, path, params={"validationToken": "t"})
                assert response.status_code == (200 if on else 404), "routes exist only when on"
    subscribed = [r for r in graph.calls("POST") if r.url.path == "/v1.0/subscriptions"]
    assert len(subscribed) == (1 if on else 0), "subscribed on first sight only when on"


@pytest.mark.usefixtures("entra_env", "stub_bot_token", "no_renewals")
async def test_a_draining_instance_answers_503_and_reads_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Graph retries a 5xx, so the batch reaches the next instance; nothing was claimed here."""
    graph = FakeGraph()
    graph.messages["100"] = _message("100", NOW)
    service = _service(db_session_factory, graph, on=True)
    await _stored(db_session_factory, expires_at=NOW + timedelta(days=3))
    service.turns.draining = True
    resource = f"teams('{TEAM_GROUP_ID}')/channels('{PRIVATE}')/messages('100')"
    entry = {"subscriptionId": "sub-0", "clientState": SECRET, "changeType": "created"}
    for path in (NOTIFICATIONS_PATH, LIFECYCLE_PATH):
        response = await _post(service, path, json={"value": [entry | {"resource": resource}]})
        assert response.status_code == 503, "refused so Graph retries elsewhere"
    assert graph.requests == [], "nothing read"
    row = await get_row(db_session_factory)
    assert row is not None and row.last_message_id is None, "the watermark has not moved"


async def get_row(factory: async_sessionmaker[AsyncSession]) -> TeamsChannelSubscriptionRow | None:
    async with factory() as session:
        return await get_teams_channel_subscription(session, tenant_id=TENANT, channel_id=PRIVATE)
