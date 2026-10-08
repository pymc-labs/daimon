"""The `support` form, through the real SDK route: a credit per request, the row kept on failure."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.teams import support
from daimon.adapters.teams.commands import parse_command
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.support import SupportCommand
from daimon.core._models import SupportEscalation
from daimon.core.config import SupportSettings
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    OTHER_AAD_OBJECT_ID,
    THREAD_ID,
    TeamsApiFake,
    build_teams_runtime,
    make_card_action,
    make_channel_activity,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
OPS = "19:ops@thread.tacv2"


def _running(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    *,
    channel: str = OPS,
    credits: int | None = 3,
    http: httpx.AsyncClient | None = None,
) -> AbstractAsyncContextManager[TeamsHttpService]:
    runtime = build_teams_runtime(db_factory, http_client=http)
    runtime.settings.support = (
        SupportSettings(escalation_channel_id=channel)
        if credits is None
        else SupportSettings(escalation_channel_id=channel, credits_per_user=credits)
    )
    cast(Any, runtime.settings).discord = SimpleNamespace(bot_token=SecretStr("discord-token"))
    return running_service(runtime, fake)


async def _form(service: TeamsHttpService, fake: TeamsApiFake, activity: dict[str, object]) -> str:
    """Send `support`; the JSON of its reply in the 1:1 chat (a channel only gets a pointer)."""
    seen = len(fake.activity_requests)
    await post_activity(service, activity)
    async with asyncio.timeout(10):
        while not (new := [r for r in fake.activity_requests[seen:] if THREAD_ID not in r.url]):
            await asyncio.sleep(0.01)
    return json.dumps(new[0].body, ensure_ascii=False)


def _token(card: str) -> str:
    found = re.search(r'"ask": "([^"]+)"', card)
    assert found is not None, card
    return found.group(1)


async def _send(
    service: TeamsHttpService, token: str, note: str, *, user: str = AAD_OBJECT_ID
) -> str:
    click = make_card_action("support", "send", user=user, ask=token, note=note)
    return json.dumps(await post_activity(service, click), ensure_ascii=False)


async def _rows(db_factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with db_factory() as session:
        return list((await session.scalars(select(SupportEscalation))).all())


def _posts_to(fake: TeamsApiFake, conversation: str) -> list[str]:
    return [
        json.dumps(r.body, ensure_ascii=False)
        for r in fake.activity_requests
        if f"/conversations/{conversation}/" in r.url
    ]


async def test_the_default_allowance_is_twenty_requests(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake, credits=None) as service:
        card = await _form(service, teams_api_fake, make_channel_activity(text="support"))
        reply = await _send(service, _token(card), "help")

    assert "20 requests left" in card
    assert support.RECEIVED.format(remaining=19) in reply


def test_support_form_uses_singular_request_count() -> None:
    assert "1 request left" in support.form_card("token", 1).model_dump_json()


async def test_a_request_asked_in_a_channel_links_back_to_it(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_channel_activity(text="support"))
        reply = await _send(service, _token(card), "the routine broke")

    assert "3 requests left" in card
    assert support.RECEIVED.format(remaining=2) in reply
    [posted] = _posts_to(teams_api_fake, OPS)
    assert "the routine broke" in posted and "teams.microsoft.com/l/message/" in posted
    [row] = await _rows(db_session_factory)
    assert (row.platform_user_id, row.channel_id, row.delivered_at is not None) == (
        AAD_OBJECT_ID,
        THREAD_ID,
        True,
    )


def test_prose_starting_with_support_is_a_turn_not_a_request() -> None:
    names = {"support": SupportCommand(MagicMock(), None).command}
    assert parse_command("support", names) == ("support", "")
    assert parse_command("support vector machines?", names) is None


async def test_someone_elses_form_names_only_their_own_chat(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_channel_activity(text="support"))
        await _send(service, _token(card), "help", user=OTHER_AAD_OBJECT_ID)

    [row] = await _rows(db_session_factory)
    assert row.platform_user_id == OTHER_AAD_OBJECT_ID and row.channel_id != THREAD_ID
    [posted] = _posts_to(teams_api_fake, OPS)
    assert "sent in the 1:1 chat" in posted


async def test_requests_stop_when_the_credits_run_out(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake, credits=1) as service:
        card = await _form(service, teams_api_fake, make_message_activity(text="support"))
        await _send(service, _token(card), "one")
        stale = await _send(service, _token(card), "two")
        again = await _form(
            service, teams_api_fake, make_message_activity(text="support", activity_id="a-2")
        )

    assert support.OUT_OF_CREDITS in stale and support.OUT_OF_CREDITS in again
    assert len(_posts_to(teams_api_fake, OPS)) == 1 and len(await _rows(db_session_factory)) == 1


async def test_an_empty_request_is_refused_and_records_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_message_activity(text="support"))
        reply = await _send(service, _token(card), " ")
    assert support.USAGE in reply and await _rows(db_session_factory) == []


async def test_a_discord_channel_gets_the_request_through_the_discord_bot(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    sent: list[httpx.Request] = []

    def discord(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(discord))
    async with _running(db_session_factory, teams_api_fake, channel="123456789", http=http) as svc:
        card = await _form(svc, teams_api_fake, make_message_activity(text="support"))
        await _send(svc, _token(card), "@everyone help")
    [request] = sent
    assert str(request.url) == "https://discord.com/api/v10/channels/123456789/messages"
    assert json.loads(request.content)["allowed_mentions"] == {"parse": []}, "a note pings no one"


async def test_a_failed_post_keeps_the_request_undelivered(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    async with _running(db_session_factory, teams_api_fake, channel="123456789", http=http) as svc:
        card = await _form(svc, teams_api_fake, make_message_activity(text="support"))
        reply = await _send(svc, _token(card), "help")
    assert support.RECORDED_UNDELIVERED in reply
    [row] = await _rows(db_session_factory)
    assert row.delivered_at is None


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
