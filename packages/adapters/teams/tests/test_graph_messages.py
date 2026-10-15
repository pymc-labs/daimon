"""Messages Graph reports from private channels: parsed, claimed once, routed as Bot Framework's.

The turn and participation paths are recorded, not run; `handle_graph_message`
gets the message Graph would return, so only routing is under test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.adapters.teams.admissions import HOLDING, admission_channel
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.externals import ExternalParticipants, MemberFacts
from daimon.adapters.teams.identity import DENIED
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.domain import TeamsChannelSubscriptionRow
from daimon.core.stores.tenants import set_plain_text_mentions
from daimon.core.teams_graph import GraphMessage
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from microsoft_teams.api import MessageActivity
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_CLIENT_ID,
    ENTRA_TENANT_ID,
    SERVICE_URL,
    TEAM_GROUP_ID,
    FakeSender,
    bot_token,
    build_teams_runtime,
    make_channel_activity,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")


@pytest.fixture
def db_session_factory(
    db_session: AsyncSession, db_nullpool_engine: AsyncEngine
) -> async_sessionmaker[AsyncSession]:
    """A connection per session, as in production: admission claims overlap other writes.

    `db_session` wipes the schema first.
    """
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
PRIVATE = "19:private-1@thread.tacv2"
ROOT = "1700000000001"
FOREIGN_TENANT = "99999999-9999-9999-9999-999999999999"
CHANNEL = TeamsChannelSubscriptionRow(
    tenant_id=TENANT,
    channel_id=PRIVATE,
    team_id="19:team@thread.tacv2",
    group_id=TEAM_GROUP_ID,
    channel_type="private",
    channel_name="Secret",
    team_name="Team",
    bot_name="Daimon",
    service_url=SERVICE_URL,
    subscription_id="sub-1",
    client_state_sha256=b"h" * 32,
    expires_at=datetime(2026, 1, 4, tzinfo=UTC),
    last_message_id=None,
    last_message_at=None,
)


def _graph(
    id: str,
    html: str,
    *,
    reply_to: str | None = ROOT,
    sender: dict[str, Any] | None = None,
    **extra: Any,
) -> GraphMessage:
    """A Graph `chatMessage` by a person in `PRIVATE`, a reply under `ROOT` unless told."""
    user = {"id": AAD_OBJECT_ID, "displayName": "Ada", "tenantId": ENTRA_TENANT_ID}
    return GraphMessage.model_validate(
        {
            "id": id,
            "messageType": "message",
            "createdDateTime": "2026-01-01T00:00:00Z",
            "replyToId": reply_to,
            "from": sender if sender is not None else {"user": user},
            "body": {"contentType": "html", "content": html},
        }
        | extra
    )


def _app(factory: async_sessionmaker[AsyncSession], sender: FakeSender | None = None) -> TeamsApp:
    return TeamsApp(
        runtime=build_teams_runtime(factory),
        sender=sender or FakeSender(),
        commands={},
        bot_token=bot_token,
    )


class Routed:
    """What reached the turn and participation paths, in order."""

    def __init__(self, teams: TeamsApp) -> None:
        self.teams = teams
        self.paths: list[tuple[str, Any]] = []

    async def graph(self, message: GraphMessage) -> None:
        await self._run(self.teams.handle_graph_message(message, CHANNEL))

    async def boot(self) -> None:
        """Start the app, so it reclaims what an exited one held."""
        await self._run(self.teams.start())

    async def bot_framework(self, payload: dict[str, object]) -> None:
        activity = MessageActivity.model_validate(payload)
        ctx = SimpleNamespace(
            activity=activity,
            conversation_ref=SimpleNamespace(service_url=SERVICE_URL),
            reply=AsyncMock(),
        )
        await self._run(self.teams.handle_message(ctx))  # pyright: ignore[reportArgumentType]

    async def _run(self, delivery: Any) -> None:
        async def _handle(_self: TeamsApp, inbound: Any) -> None:
            self.paths.append(("turn", inbound))

        async def _observe(_self: TeamsApp, inbound: Any) -> None:
            self.paths.append(("participation", inbound))

        with (
            patch.object(TeamsApp, "_handle", _handle),
            patch.object(TeamsApp, "_observe", _observe),
        ):
            await delivery
            async with asyncio.timeout(10):
                while tasks := list(self.teams._tasks):  # pyright: ignore[reportPrivateUsage]
                    await asyncio.gather(*tasks)
                    await asyncio.sleep(0)  # lets a finished task's done callback drop it


async def _switch_on(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await set_plain_text_mentions(session, tenant_id=TENANT, enabled=True)


async def test_a_typed_bot_name_on_a_root_post_starts_a_thread_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _switch_on(db_session_factory)
    routed = Routed(_app(db_session_factory))
    await routed.graph(_graph(ROOT, "<p>@Daimon summarise the week</p>", reply_to=None))

    [(path, inbound)] = routed.paths
    assert path == "turn" and inbound.text == "summarise the week", "the name is dropped"
    assert inbound.conversation_id == f"{PRIVATE};messageid={ROOT}", "the post starts a thread"
    assert (inbound.channel_type, inbound.team_group_id, inbound.service_url) == (
        "private",
        TEAM_GROUP_ID,
        SERVICE_URL,
    ), "what Graph does not carry comes from the subscription"


async def test_a_reply_quoting_the_bot_is_addressed_to_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    routed = Routed(_app(db_session_factory))
    reference = {
        "messageId": "1700000000002",
        "messagePreview": "Here is the summary",
        "messageSender": {"application": {"id": BOT_CLIENT_ID, "displayName": "Daimon"}},
    }
    quote = {"id": "1700000000002", "contentType": "messageReference"}
    message = _graph(
        "1700000000003",
        '<attachment id="1700000000002"></attachment><p>why that?</p>',
        attachments=[quote | {"content": json.dumps(reference)}],
    )
    await routed.graph(message)

    [(path, inbound)] = routed.paths
    assert path == "turn" and not inbound.unprompted, "a quote of the bot is a mention"
    assert inbound.text == '[quoting Daimon: "Here is the summary"]\nwhy that?', (
        "the quote reaches the agent as a Bot Framework quote does"
    )


async def test_an_untagged_thread_reply_goes_to_participation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    routed = Routed(_app(db_session_factory))
    await routed.graph(_graph("1700000000003", "<p>any thoughts?</p>"))

    [(path, inbound)] = routed.paths
    assert path == "participation" and inbound.unprompted, "screened for a followed thread"
    assert inbound.user_id == AAD_OBJECT_ID and not inbound.is_external


async def test_the_bots_own_message_and_an_untagged_root_post_are_skipped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    routed = Routed(_app(db_session_factory))
    bot = {"application": {"id": BOT_CLIENT_ID, "displayName": "Daimon"}}
    await routed.graph(_graph("1700000000003", "<p>Here you go</p>", sender=bot))
    await routed.graph(_graph("1700000000004", "<p>new topic</p>", reply_to=None))
    assert routed.paths == [], "no app's message and no unaddressed root post is routed"


async def test_a_guest_is_classified_by_the_roster_not_by_graphs_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Graph may show a B2B guest's home tenant; the roster, as for Bot Framework, decides."""
    teams = _app(db_session_factory)
    lookups: list[tuple[str, str]] = []

    async def roster_member(conversation_id: str, user_id: str) -> MemberFacts | None:
        lookups.append((conversation_id, user_id))
        return MemberFacts(tenant_id=ENTRA_TENANT_ID, is_guest=True)

    async def channel_type(_team: str, _channel: str) -> str | None:
        return "private"

    async def no_members(*_args: object) -> dict[str, MemberFacts]:
        return {}

    async def no_listed_guests() -> frozenset[str]:
        return frozenset()

    teams.runtime = dataclasses.replace(
        teams.runtime,
        externals=ExternalParticipants(
            tenant_id=ENTRA_TENANT_ID,
            roster_member=roster_member,
            channel_type=channel_type,
            channel_members=no_members,
            member_guests=no_listed_guests,
        ),
    )
    routed = Routed(teams)
    user = {"id": AAD_OBJECT_ID, "displayName": "Bob", "tenantId": FOREIGN_TENANT}
    await routed.graph(_graph("1700000000003", "<p>hello</p>", sender={"user": user}))
    [(_path, inbound)] = routed.paths
    assert not inbound.is_external_known and inbound.home_tenant_id is None, (
        "Graph's sender tenant is not taken as evidence"
    )
    classified = await teams._classified(inbound)  # pyright: ignore[reportPrivateUsage]
    assert lookups == [(PRIVATE, AAD_OBJECT_ID)], "the Bot Framework roster is asked"
    assert classified.is_external and classified.home_tenant_id is None, (
        "a guest is held as one, not as a member of another organisation"
    )


async def test_while_draining_a_message_is_held_and_the_next_boot_runs_it_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Graph never resends a notice answered 202 and the watermark moves past the message,
    so the draining instance claims and holds it as a Bot Framework arrival."""
    await _switch_on(db_session_factory)
    sent = datetime.now(UTC).isoformat()
    message = _graph(ROOT, "<p>@Daimon summarise the week</p>", reply_to=None, createdDateTime=sent)
    chatter = _graph("1700000000003", "<p>any thoughts?</p>", createdDateTime=sent)
    sender = FakeSender()
    draining = Routed(_app(db_session_factory, sender))
    draining.teams.draining = True
    await draining.graph(message)
    await draining.graph(chatter)
    assert draining.paths == [], "nothing runs while draining"
    assert [a.text for _, a, _ in sender.sent] == [HOLDING], "the asker is told it is held"

    successor = Routed(_app(db_session_factory))
    await successor.boot()
    await successor.teams.drain(0)
    assert [(path, i.activity_id) for path, i in successor.paths] == [("turn", ROOT)], (
        "only the reclaim can run it: the next boot replays it once; chatter is not held"
    )


def _private_reply(activity_id: str, *, mention: bool) -> dict[str, object]:
    return make_channel_activity(
        conversation_id=f"{PRIVATE};messageid={ROOT}", activity_id=activity_id, mention_bot=mention
    )


async def test_a_message_from_both_sources_runs_once_whichever_comes_first(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bot Framework and Graph claim the same admission row, so a repeated notification,
    or one read again after a restart, runs nothing."""
    assert admission_channel(f"{PRIVATE};messageid={ROOT}") == PRIVATE, (
        "a thread reply's activity is keyed under its channel, as its Graph message is"
    )
    routed = Routed(_app(db_session_factory))
    await routed.bot_framework(_private_reply("1700000000003", mention=False))
    await routed.graph(_graph("1700000000003", "<p>any thoughts?</p>"))
    await routed.graph(_graph("1700000000004", "<p>and this?</p>"))
    await routed.graph(_graph("1700000000004", "<p>and this?</p>"))
    await routed.bot_framework(_private_reply("1700000000004", mention=False))
    assert [inbound.activity_id for _, inbound in routed.paths] == [
        "1700000000003",
        "1700000000004",
    ], "each message runs once, from whichever source claimed it"
    restarted = Routed(_app(db_session_factory))
    await restarted.graph(_graph("1700000000003", "<p>any thoughts?</p>"))
    assert restarted.paths == [], "the admission outlives the process"


async def test_a_refusal_from_graph_is_sent_once(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A refused Reply to the bot is answered after its receipt is recorded, once."""
    sender = FakeSender()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={},
        bot_token=bot_token,
    )
    reference = {"messageSender": {"application": {"id": BOT_CLIENT_ID}}}
    quote = {"id": "1700000000002", "contentType": "messageReference"}
    unverified = {"user": {"id": "not-a-uuid", "displayName": "Ada"}}
    message = _graph(
        "1700000000003",
        '<attachment id="1700000000002"></attachment><p>why?</p>',
        sender=unverified,
        attachments=[quote | {"content": json.dumps(reference)}],
    )
    for _ in range(2):
        await teams.handle_graph_message(message, CHANNEL)
    assert [(conversation, activity.text) for conversation, activity, _ in sender.sent] == [
        (f"{PRIVATE};messageid={ROOT}", DENIED)
    ], "one refusal, in the message's thread"


async def test_an_at_mention_from_graph_is_left_to_bot_framework(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Graph skips it without claiming it, so the Bot Framework delivery still runs."""
    routed = Routed(_app(db_session_factory))
    mention = {"id": 0, "mentioned": {"application": {"id": BOT_CLIENT_ID}}}
    html = '<p><at id="0">Daimon</at> help</p>'
    await routed.graph(_graph("1700000000003", html, mentions=[mention]))
    assert routed.paths == [], "Graph never runs an @mention"
    await routed.bot_framework(_private_reply("1700000000003", mention=True))
    assert [path for path, _ in routed.paths] == ["turn"], (
        "the mention runs once, from Bot Framework"
    )
