"""The `support` command: a credit spent per request, the row kept when the post fails."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from daimon.adapters.teams import support
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.support import SupportCommand
from daimon.core._models import SupportEscalation
from daimon.core.config import SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from microsoft_teams.api import MessageActivityInput, SentActivity
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    THREAD_ID,
    build_teams_runtime,
    make_inbound,
)

pytestmark = pytest.mark.usefixtures("provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
OPS = "19:ops@thread.tacv2"


@dataclasses.dataclass
class _Direct:
    fail: bool = False
    posts: list[tuple[str, str]] = dataclasses.field(default_factory=list[tuple[str, str]])

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        return None

    async def open_chat(self, member_id: str) -> str:
        return "a:direct"

    async def post(self, conversation_id: str, text: str) -> None:
        if self.fail:
            raise httpx.ConnectError("unreachable")
        self.posts.append((conversation_id, text))


async def _ask(
    db_factory: async_sessionmaker[AsyncSession],
    note: str,
    *,
    direct: _Direct | None = None,
    channel: str = OPS,
    credits: int = 3,
    http: httpx.AsyncClient | None = None,
) -> list[str]:
    runtime = build_teams_runtime(db_factory, http_client=http)
    runtime.settings.support = SupportSettings(
        escalation_channel_id=channel, credits_per_user=credits
    )
    cast(Any, runtime.settings).discord = SimpleNamespace(bot_token=SecretStr("discord-token"))
    replies: list[str] = []

    async def send(activity: MessageActivityInput) -> SentActivity:
        replies.append(activity.text or "")
        return SentActivity(id="r-1", activity_params=activity)

    asked = dataclasses.replace(
        make_inbound(f"support {note}", conversation=THREAD_ID, kind="channel"),
        channel_id=CHANNEL_ID,
    )
    await SupportCommand(direct).command(
        CommandContext(
            inbound=make_inbound(f"support {note}", conversation="a:direct"),
            tenant_id=TENANT,
            args=note,
            is_admin=False,
            runtime=runtime,
            send=send,
            asked_in=asked,
        )
    )
    return replies


async def _rows(db_factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with db_factory() as session:
        return list((await session.scalars(select(SupportEscalation))).all())


async def test_a_request_is_posted_with_a_link_to_where_it_was_asked(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    direct = _Direct()
    replies = await _ask(db_session_factory, "the routine broke", direct=direct)
    assert replies == [support.RECEIVED.format(remaining=2)]
    [(where, body)] = direct.posts
    assert where == OPS and "the routine broke" in body and "teams.microsoft.com/l/message/" in body
    [row] = await _rows(db_session_factory)
    assert (row.platform_user_id, row.channel_id, row.delivered_at is not None) == (
        AAD_OBJECT_ID,
        THREAD_ID,
        True,
    )


async def test_requests_stop_when_the_credits_run_out(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    direct = _Direct()
    await _ask(db_session_factory, "one", direct=direct, credits=1)
    assert await _ask(db_session_factory, "two", direct=direct, credits=1) == [
        support.OUT_OF_CREDITS
    ]
    assert len(direct.posts) == 1 and len(await _rows(db_session_factory)) == 1


async def test_a_failed_post_keeps_the_request_undelivered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    replies = await _ask(db_session_factory, "help", direct=_Direct(fail=True))
    assert replies == [support.RECORDED_UNDELIVERED]
    [row] = await _rows(db_session_factory)
    assert row.delivered_at is None


async def test_a_discord_channel_gets_the_request_through_the_discord_bot(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sent: list[httpx.Request] = []

    def discord(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(discord))
    await _ask(db_session_factory, "@everyone help", channel="123456789", http=http)
    [request] = sent
    assert str(request.url) == "https://discord.com/api/v10/channels/123456789/messages"
    assert json.loads(request.content)["allowed_mentions"] == {"parse": []}, "a note pings no one"


async def test_an_empty_request_is_shown_how_to_ask(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    assert await _ask(db_session_factory, " ", direct=_Direct()) == [support.USAGE]
    assert await _rows(db_session_factory) == []


@pytest.mark.parametrize(
    ("channel", "credits", "discord", "on"),
    [
        (None, 3, True, False),
        (OPS, 0, True, False),
        ("123", 3, False, False),
        ("123", 3, True, True),
        (OPS, 3, False, True),
    ],
)
def test_the_command_exists_only_when_a_request_can_reach_someone(
    channel: str | None, credits: int, discord: bool, on: bool
) -> None:
    settings: Any = SimpleNamespace(
        support=SupportSettings(escalation_channel_id=channel, credits_per_user=credits),
        discord=object() if discord else None,
    )
    assert support.enabled(settings) is on
