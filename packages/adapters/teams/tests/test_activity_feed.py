"""Activity feed notices: the Graph request, the deep link, skip rules and failure isolation."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import structlog
from daimon.adapters.teams.activity_feed import ActivityFeed, ChannelHomes, chain_id
from daimon.adapters.teams.provider_actions import TeamsProviderActions
from daimon.adapters.teams.tool_confirmation import APPROVAL_NOTICE, TeamsConfirmationCards
from daimon.adapters.teams.turn_hooks import turn_hooks
from daimon.core.config import TurnSettings
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.stores.domain import TeamsInstallationRow
from daimon.core.teams_graph import GraphClient
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.provider_actions import ProviderActionPrompt

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    TEAM_GROUP_ID,
    THREAD_ID,
    FakeSender,
    make_inbound,
)
from .test_provider_actions import SESSION, prompt

TEAM_ID = "19:general@thread.tacv2"
REPLY_ID = "1700000000555"


def question(thread_id: str) -> ProviderActionPrompt:
    """A provider question from `thread_id` that expires unanswered at once."""
    value = prompt()
    requester = dataclasses.replace(value.context.requester, thread_id=thread_id)
    expires_at = datetime.now(UTC) + timedelta(milliseconds=10)
    return dataclasses.replace(
        value,
        context=dataclasses.replace(value.context, requester=requester, expires_at=expires_at),
    )


class _Feed:
    """An `ActivityFeed` over a fake Graph answering `status`, recording requests and tasks."""

    def __init__(
        self,
        status: int = 204,
        *,
        channels: dict[str, str] | None = None,
        on_request: Callable[[httpx.Request], Coroutine[Any, Any, None]] | None = None,
        timeout_s: float = 5.0,
        stored: dict[str, str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.tasks: list[asyncio.Task[None]] = []
        self.channel_lists: list[str] = []
        self.stored_reads: list[str] = []
        self.team_reads = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if on_request is not None:
                await on_request(request)
            return httpx.Response(status)

        async def token() -> str:
            return "graph-token"

        async def teams() -> list[TeamsInstallationRow]:
            self.team_reads += 1
            row = {"tenant_id": uuid.uuid4(), "team_id": TEAM_ID, "group_id": TEAM_GROUP_ID}
            return [TeamsInstallationRow.model_validate(row | {"name": "Research"})]

        async def team_channels(team_id: str) -> dict[str, str]:
            self.channel_lists.append(team_id)
            return channels if channels is not None else {CHANNEL_ID: "Weekly reports"}

        async def stored_group(channel_id: str) -> str | None:
            self.stored_reads.append(channel_id)
            return (stored or {}).get(channel_id)

        def spawn(coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
            task = asyncio.create_task(coro, name=name)
            self.tasks.append(task)
            return task

        graph = GraphClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), token)
        self.feed = ActivityFeed(
            graph,
            ChannelHomes(
                teams,
                team_channels,
                stored_group=stored_group,
                **({"clock": clock} if clock is not None else {}),
            ),
            entra_tenant_id=ENTRA_TENANT_ID,
            spawn=spawn,
            timeout_s=timeout_s,
        )

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks)


async def test_a_thread_reply_notice_posts_the_documented_body() -> None:
    """systemDefault on the team, the reason in systemDefaultText, a link to the reply."""
    fake = _Feed()
    fake.feed.notify(
        THREAD_ID,
        REPLY_ID,
        recipient_id=AAD_OBJECT_ID,
        text="Result from the weekly routine",
        preview="All   green.\nNothing failed.",
        chain=chain_id(uuid.UUID(int=7)),
    )
    await fake.settle()

    [request] = fake.requests
    assert request.method == "POST", "one notice"
    assert str(request.url) == (
        f"https://graph.microsoft.com/v1.0/teams/{TEAM_GROUP_ID}/sendActivityNotification"
    ), "the team endpoint, by the team's Entra group"
    assert request.headers["Authorization"] == "Bearer graph-token", "app-only token"
    body = json.loads(request.content)
    assert body["activityType"] == "systemDefault", "the reserved type needs no manifest entry"
    assert body["templateParameters"] == [
        {"name": "systemDefaultText", "value": "Result from the weekly routine"}
    ], "the free text rides in systemDefaultText"
    assert body["previewText"] == {"content": "All green. Nothing failed."}, "one line"
    assert body["recipient"] == {
        "@odata.type": "microsoft.graph.aadUserNotificationRecipient",
        "userId": AAD_OBJECT_ID,
    }, "the person, by Entra id"
    assert body["chainId"] == 7, "the chain id given"
    topic = body["topic"]
    assert (topic["source"], topic["value"]) == ("text", "Weekly reports"), "named by channel"
    link = urlsplit(str(topic["webUrl"]))
    assert (link.scheme, link.netloc) == ("https", "teams.microsoft.com"), "a Teams domain"
    assert link.path == f"/l/message/{CHANNEL_ID}/{REPLY_ID}", "the reply in its channel"
    assert parse_qs(link.query) == {
        "tenantId": [ENTRA_TENANT_ID],
        "groupId": [TEAM_GROUP_ID],
        "parentMessageId": ["1700000000001"],
        "teamName": ["Research"],
        "channelName": ["Weekly reports"],
        "createdTime": [REPLY_ID],
    }, "every parameter of the documented channel-message link"


async def test_a_new_post_is_its_own_parent_and_general_needs_no_channel_list() -> None:
    """A root post links to itself; General's id is the team's, so nothing is listed."""
    fake = _Feed()
    fake.feed.notify(TEAM_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
    await fake.settle()

    body = json.loads(fake.requests[0].content)
    query = parse_qs(urlsplit(str(body["topic"]["webUrl"])).query)
    assert query["parentMessageId"] == [REPLY_ID], "a root post is its own thread"
    assert query["channelName"] == ["General"], "General named"
    assert "chainId" not in body, "no chain unless asked"
    assert fake.channel_lists == [], "no channel list for General"


async def test_a_chat_gets_no_notice() -> None:
    """A 1:1 chat notifies by itself: nothing is spawned or sent."""
    fake = _Feed()
    fake.feed.notify(CONVERSATION_ID, "m-1", recipient_id=AAD_OBJECT_ID, text="t", preview="p")
    assert fake.tasks == [] and fake.requests == [], "chats are skipped"


async def test_a_refused_notice_is_logged_once_and_never_raised() -> None:
    """Graph's 403 (no consent) becomes one log line with status and reason only."""
    fake = _Feed(403)
    with structlog.testing.capture_logs() as logs:
        fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
        await fake.settle()
    failed = [entry for entry in logs if entry["event"] == "teams.activity_notice.failed"]
    assert failed == [
        {
            "event": "teams.activity_notice.failed",
            "status": 403,
            "reason": "http error",
            "log_level": "info",
        }
    ], "status and reason, no ids or tokens"
    assert len(fake.requests) == 1, "never retried"


async def test_a_slow_graph_is_cut_off_by_the_timeout() -> None:
    """A notice that hangs ends at its timeout, logged."""

    async def hang(_: httpx.Request) -> None:
        await asyncio.sleep(5)

    fake = _Feed(on_request=hang, timeout_s=0.05)
    with structlog.testing.capture_logs() as logs:
        fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
        await fake.settle()
    reasons = [e.get("reason") for e in logs if e["event"] == "teams.activity_notice.failed"]
    assert reasons == ["timed out"], "the timeout ends the notice"


async def test_a_channel_no_recorded_team_holds_is_skipped() -> None:
    """A channel in no recorded team: logged, no Graph call."""
    fake = _Feed(channels={})
    with structlog.testing.capture_logs() as logs:
        fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
        await fake.settle()
    assert fake.requests == [], "nothing sent"
    assert any(e.get("reason") == "team not found" for e in logs), "logged"


async def test_a_channel_not_found_is_not_looked_up_again_until_the_miss_expires() -> None:
    """A miss is cached for its TTL: repeated notices cost no installs read or channel lists."""
    now = [0.0]
    fake = _Feed(channels={}, clock=lambda: now[0])
    for _ in range(3):
        fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
        await fake.settle()
    assert (fake.team_reads, fake.channel_lists) == (1, [TEAM_ID]), "one walk for three notices"
    now[0] = 601.0
    fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
    await fake.settle()
    assert fake.team_reads == 2, "looked up again once the miss expires"
    assert fake.requests == [], "never sent"


async def test_a_stored_channel_row_names_the_team_without_walking_every_team() -> None:
    """A private channel the lists leave out is found by its stored row's group."""
    fake = _Feed(channels={}, stored={CHANNEL_ID: "group-private"})
    fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
    await fake.settle()
    [request] = fake.requests
    assert request.url.path == "/v1.0/teams/group-private/sendActivityNotification", "its team"
    assert fake.channel_lists == [], "no team's channel list is walked"


async def test_a_stored_group_of_a_recorded_team_lists_only_that_team() -> None:
    """The recorded team gives the names, from its one channel list."""
    fake = _Feed(stored={CHANNEL_ID: TEAM_GROUP_ID})
    fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
    await fake.settle()
    body = json.loads(fake.requests[0].content)
    assert body["topic"]["value"] == "Weekly reports", "named by its channel"
    assert fake.channel_lists == [TEAM_ID], "only its own team's list"


async def test_a_known_group_skips_the_stored_read_and_overrides_a_cached_miss() -> None:
    """A caller that knows the team (an inbound activity's) is believed at once."""
    fake = _Feed(channels={})
    fake.feed.notify(THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p")
    await fake.settle()
    assert fake.requests == [], "a miss first"
    fake.feed.notify(
        THREAD_ID, REPLY_ID, recipient_id=AAD_OBJECT_ID, text="t", preview="p", group_id="g-2"
    )
    await fake.settle()
    [request] = fake.requests
    assert request.url.path == "/v1.0/teams/g-2/sendActivityNotification", "the given team"
    assert fake.stored_reads == [CHANNEL_ID], "the stored rows are read only for the miss"


def test_chain_id_is_a_stable_positive_int64() -> None:
    """One routine keeps one feed entry: the same id gives the same chain."""
    key = uuid.uuid4()
    assert chain_id(key) == chain_id(key), "stable"
    assert 0 < chain_id(key) < 2**63, "a positive Int64"


async def _confirm_once(
    fake: _Feed, conversation_id: str, *, team_group_id: str | None = None
) -> FakeSender:
    sender = FakeSender()
    cards = TeamsConfirmationCards(sender, feed=fake.feed)
    hook = cards.hook(
        conversation_id=conversation_id, service_url=None, team_group_id=team_group_id
    )
    call = ToolCall(tool_use_id="tu_1", server_name="linear", tool_name="create_issue", input={})
    now = datetime.now(UTC)
    prompt = prompt_for_tool_call(call, requester_platform_user_id=AAD_OBJECT_ID, now=now)
    expiring = prompt.model_copy(update={"expires_at": now + timedelta(milliseconds=10)})
    await hook(expiring)
    await fake.settle()
    return sender


async def test_an_approval_card_in_a_thread_notifies_its_requester() -> None:
    """The person who must click hears of the card, linked to it."""
    fake = _Feed()
    await _confirm_once(fake, THREAD_ID)
    [request] = fake.requests
    body = json.loads(request.content)
    assert body["recipient"]["userId"] == AAD_OBJECT_ID, "the requester"
    assert body["templateParameters"][0]["value"] == APPROVAL_NOTICE, "an approval notice"
    assert "/m-1?" in body["topic"]["webUrl"], "linked to the posted card"


async def test_an_approval_card_passes_the_inbound_team_to_its_notice() -> None:
    """The turn's team comes from the inbound activity: no channel list is needed."""
    fake = _Feed(channels={})
    await _confirm_once(fake, THREAD_ID, team_group_id="g-inbound")
    [request] = fake.requests
    assert request.url.path == "/v1.0/teams/g-inbound/sendActivityNotification", "that team"
    assert fake.stored_reads == [], "no stored read"


@pytest.mark.parametrize("card", ["confirmation", "question"])
async def test_a_turns_cards_notify_the_inbound_team_without_a_lookup(card: str) -> None:
    """A turn's hooks pass its activity's team: neither the stored rows nor a list is read."""
    fake = _Feed(channels={})
    sender = FakeSender()
    runtime = MagicMock()
    runtime.settings.turn = TurnSettings()
    inbound = make_inbound(kind="channel", conversation=THREAD_ID)
    hooks = turn_hooks(
        TeamsConfirmationCards(sender, feed=fake.feed),
        TeamsProviderActions(sender, feed=fake.feed),
        runtime=runtime,
        inbound=dataclasses.replace(inbound, team_group_id="g-inbound"),
        prepared=SimpleNamespace(session_ref=SESSION),
        admission=SimpleNamespace(account_id=uuid.uuid4()),
        tenant_id=uuid.uuid4(),
    )
    now = datetime.now(UTC)
    if card == "confirmation":
        call = ToolCall(tool_use_id="tu_1", server_name="linear", tool_name="x", input={})
        prompt = prompt_for_tool_call(call, requester_platform_user_id=AAD_OBJECT_ID, now=now)
        await hooks["confirm_write"](
            prompt.model_copy(update={"expires_at": now + timedelta(milliseconds=10)})
        )
    else:
        await hooks["provider_action"](question(THREAD_ID))
    await fake.settle()
    [request] = fake.requests
    assert request.url.path == "/v1.0/teams/g-inbound/sendActivityNotification", "that team"
    assert fake.stored_reads == [] and fake.channel_lists == [], "no lookup"


async def test_an_approval_card_in_a_chat_sends_no_notice() -> None:
    """A 1:1 chat's card notifies by itself."""
    fake = _Feed()
    sender = await _confirm_once(fake, CONVERSATION_ID)
    assert sender.sent, "the card is posted"
    assert fake.requests == [], "no notice"
