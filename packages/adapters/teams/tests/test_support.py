"""The `support` form, through the real SDK route: a credit per request, the row kept on failure."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.teams import card, support
from daimon.adapters.teams.answer_access import IN_DIRECT_CHAT, NOT_ALLOWED
from daimon.adapters.teams.commands import parse_command
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.support import SupportCommand
from daimon.core._models import SupportEscalation
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.config import SupportSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_platform_principal
from microsoft_teams.common.http.client import MiddlewareContext
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    CONVERSATION_ID,
    DIRECT_CHAT_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    THREAD_ID,
    TeamsApiFake,
    assert_card_renders,
    build_teams_runtime,
    make_card_action,
    make_channel_activity,
    make_invoke,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
OPS = "19:ops@thread.tacv2"
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
LEAD = "00000000-0000-0000-0000-00000000000d"


def _running(
    db_factory: async_sessionmaker[AsyncSession],
    fake: TeamsApiFake,
    *,
    channel: str = OPS,
    credits: int | None = 3,
) -> AbstractAsyncContextManager[TeamsHttpService]:
    runtime = build_teams_runtime(db_factory)
    runtime.settings.support = (
        SupportSettings(teams_escalation_channel_id=channel)
        if credits is None
        else SupportSettings(teams_escalation_channel_id=channel, credits_per_user=credits)
    )
    return running_service(runtime, fake)


@dataclasses.dataclass
class _OpsFake(TeamsApiFake):
    """A post in the support channel sets `posting`, waits for `release`, fails when `fail`."""

    posting: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    release: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    fail: bool = False

    async def send(
        self, context: MiddlewareContext, next: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        if context.method == "POST" and f"/conversations/{OPS}/activities" in context.url:
            self.posting.set()
            await self.release.wait()
            if self.fail:
                return httpx.Response(500, request=httpx.Request(context.method, context.url))
        return await super().send(context, next)


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


async def _delivered(service: TeamsHttpService) -> None:
    """Wait for background deliveries, so the next form's reply is not one of their posts."""
    async with asyncio.timeout(10):
        while service.turns.in_flight:
            await asyncio.sleep(0.01)


async def _send(
    service: TeamsHttpService, token: str, note: str, *, user: str = AAD_OBJECT_ID
) -> str:
    click = make_card_action("support", "send", user=user, ask=token, note=note)
    reply = json.dumps(await post_activity(service, click), ensure_ascii=False)
    await _delivered(service)
    return reply


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
    names = {"support": SupportCommand(MagicMock(), None, spawn=MagicMock()).command}
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


async def test_a_failed_post_keeps_the_request_undelivered(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = _OpsFake(fail=True)
    fake.release.set()
    async with _running(db_session_factory, fake) as svc:
        card = await _form(svc, fake, make_message_activity(text="support"))
        reply = await _send(svc, _token(card), "help")
    assert fake.posting.is_set(), "the post was tried, and failed"
    assert support.RECEIVED.format(remaining=2) in reply, "recorded, whatever delivery does"
    [row] = await _rows(db_session_factory)
    assert row.delivered_at is None, "kept as undelivered"


async def test_the_request_is_answered_before_its_delivery_lands(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = _OpsFake()
    async with _running(db_session_factory, fake) as svc:
        card = await _form(svc, fake, make_message_activity(text="support"))
        click = make_card_action("support", "send", ask=_token(card), note="help")
        reply = json.dumps(await post_activity(svc, click), ensure_ascii=False)
        async with asyncio.timeout(10):
            await fake.posting.wait()  # the policy read is done; the post is in flight
        [pending] = await _rows(db_session_factory)
        fake.release.set()
        await _delivered(svc)

    assert support.RECEIVED.format(remaining=2) in reply, "the invoke does not wait for the post"
    assert pending.delivered_at is None, "answered while the post was still in flight"
    [row] = await _rows(db_session_factory)
    assert row.delivered_at is not None, "stamped once the post lands"


@pytest.mark.parametrize(
    ("teams_channel", "discord_channel", "credits", "on"),
    [
        (None, None, 3, False),
        (OPS, None, 0, False),
        (None, "123", 3, False),
        (OPS, "123", 3, True),
        (OPS, None, 3, True),
    ],
)
def test_the_command_exists_only_with_a_teams_channel_and_credits(
    teams_channel: str | None, discord_channel: str | None, credits: int, on: bool
) -> None:
    """Discord's channel is Discord's: Teams never posts there, as Slack never does."""
    settings: Any = SimpleNamespace(
        support=SupportSettings(
            teams_escalation_channel_id=teams_channel,
            escalation_channel_id=discord_channel,
            credits_per_user=credits,
        )
    )
    assert support.enabled(settings) is on


def test_routed_feedback_needs_the_teams_channel() -> None:
    """A tenant's 👎 forms never go to Discord's channel, which Teams once posted to."""
    tenant = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    routed = {tenant: True}
    discord_only: Any = SimpleNamespace(
        support=SupportSettings(escalation_channel_id="123", feedback_to_support=routed)
    )
    teams: Any = SimpleNamespace(
        support=SupportSettings(teams_escalation_channel_id=OPS, feedback_to_support=routed)
    )
    assert not support.routes_feedback(discord_only, tenant)
    assert support.routes_feedback(teams, tenant)


def _ask(op: str, **data: object) -> dict[str, object]:
    """Ask a human on the answer `m-7`: the dialog's fetch, or its submit."""
    if op == "open":
        value: dict[str, object] = {"data": {"dialog_id": card.ASK_HUMAN_DIALOG}}
        return make_invoke("task/fetch", value)
    return make_invoke("task/submit", {"data": {"action": card.ASK_HUMAN_DIALOG} | data})


async def test_ask_a_human_on_an_answer_spends_a_credit_and_links_the_answer(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        opened = await post_activity(service, _ask("open"))
        form = opened["task"]["value"]["card"]["content"]
        [send] = form["actions"]
        sent = await post_activity(service, _ask("send", **send["data"], note="wrong totals"))
        await _delivered(service)

    assert_card_renders(form)
    assert "3 requests left" in json.dumps(form), "the support form, in a dialog"
    assert send["data"]["message"] == "m-7", "the form carries the answer it was opened on"
    assert sent["task"]["value"] == support.RECEIVED.format(remaining=2)
    [posted] = _posts_to(teams_api_fake, OPS)
    assert "wrong totals" in posted and IN_DIRECT_CHAT in posted, "the note, and where it was"
    [row] = await _rows(db_session_factory)
    assert (row.message_id, row.channel_id) == ("m-7", CONVERSATION_ID)


async def test_ask_a_human_twice_on_one_answer_spends_one_credit(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        await post_activity(service, _ask("send", message="m-7", note="wrong totals"))
        await _delivered(service)
        again = await post_activity(service, _ask("send", message="m-7", note="still wrong"))

    assert again["task"]["value"] == support.ALREADY_REQUESTED, "told the first is in hand"
    assert len(await _rows(db_session_factory)) == 1, "one request, one credit"
    assert len(_posts_to(teams_api_fake, OPS)) == 1, "the support team hears it once"


async def test_a_typed_request_is_refused_when_the_policy_changed_before_send(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_channel_activity(text="support"))
        policy = TenantAccessPolicy(invoker_user_ids=(OTHER_AAD_OBJECT_ID,))
        async with db_session_factory.begin() as session:
            await set_access_policy(session, tenant_id=TENANT, policy=policy)
        reply = await _send(service, _token(card), "help")

    assert support.NOT_ALLOWED_THERE in reply, "decided again, in the spending transaction"
    assert await _rows(db_session_factory) == [] and _posts_to(teams_api_fake, OPS) == []


async def test_ask_a_human_names_the_answer_teams_replied_to_over_the_forms_copy(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        await post_activity(service, _ask("send", message="forged", note="help"))
        await _delivered(service)

    [row] = await _rows(db_session_factory)
    assert row.message_id == "m-7", "the invoke's replyToId, not the client-built form's id"


async def test_ask_a_human_with_no_note_shows_the_form_again(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        again = await post_activity(service, _ask("send", message="m-7", note=" "))
    assert support.USAGE in json.dumps(again["task"]["value"]["card"]), "fixable, not lost"
    assert await _rows(db_session_factory) == []


async def test_ask_a_human_is_refused_to_someone_who_could_not_ask_there(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    policy = TenantAccessPolicy(invoker_user_ids=(OTHER_AAD_OBJECT_ID,))
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    async with _running(db_session_factory, teams_api_fake) as service:
        opened = await post_activity(service, _ask("open"))
        sent = await post_activity(service, _ask("send", message="m-7", note="help"))
    assert opened["task"]["value"] == sent["task"]["value"] == NOT_ALLOWED
    assert await _rows(db_session_factory) == [] and _posts_to(teams_api_fake, OPS) == []


async def _grant_lead(db_factory: async_sessionmaker[AsyncSession]) -> None:
    """`LEAD` admins the channel `make_channel_activity` posts in, and has chatted before."""
    async with db_factory.begin() as session:
        tenant = await get_tenant(session, TENANT)
        await make_platform_principal(session, platform="teams", external_id=LEAD, tenant=tenant)
        await set_channel_admins(
            session,
            tenant_id=TENANT,
            platform="teams",
            channel_id=CHANNEL_ID,
            role_ids=[],
            user_ids=[LEAD],
            actor_account_id=None,
        )


@pytest.mark.parametrize("on_roster", [True, False])
async def test_a_channel_with_its_own_admins_sends_the_request_to_them_first(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    on_roster: bool,
) -> None:
    await _grant_lead(db_session_factory)
    if not on_roster:
        teams_api_fake.absent.add(LEAD)
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_channel_activity(text="support"))
        reply = await _send(service, _token(card), "the routine broke")

    assert support.RECEIVED.format(remaining=2) in reply, "delivered either way"
    lookups = [r.url for r in teams_api_fake.requests if f"/members/{LEAD}" in r.url]
    assert lookups and CHANNEL_ID in lookups[0], "the admin is looked up on the channel's roster"
    chats = [p for p in _posts_to(teams_api_fake, DIRECT_CHAT_ID) if "Human support requested" in p]
    ops = _posts_to(teams_api_fake, OPS)
    if on_roster:
        assert len(chats) == 1 and "the routine broke" in chats[0], "the admin's 1:1 chat"
        assert ops == [], "the escalation channel only when no admin got it"
    else:
        assert chats == [] and len(ops) == 1, "no admin reachable: the escalation channel"


async def test_a_database_error_picking_admins_still_reaches_the_support_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
    teams_api_fake: TeamsApiFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unreachable(*args: object, **kwargs: object) -> list[frozenset[str]]:
        raise OperationalError("SELECT", {}, OSError("connection refused"))

    await _grant_lead(db_session_factory)
    monkeypatch.setattr(support, "support_recipient_tiers", unreachable)
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_channel_activity(text="support"))
        reply = await _send(service, _token(card), "the routine broke")

    assert support.RECEIVED.format(remaining=2) in reply, "the spent credit is not a failure"
    [posted] = _posts_to(teams_api_fake, OPS)
    assert "the routine broke" in posted, "the escalation channel is still tried"
    [row] = await _rows(db_session_factory)
    assert row.delivered_at is not None, "and stamped once it lands"


async def test_a_protected_support_channel_gets_no_post(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    policy = TenantAccessPolicy(protected_channel_ids=(OPS,))
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_message_activity(text="support"))
        await _send(service, _token(card), "help")

    assert _posts_to(teams_api_fake, OPS) == [], "the bot does not post where it may not"
    [row] = await _rows(db_session_factory)
    assert row.delivered_at is None, "kept as undelivered"


async def test_a_sealed_channel_warns_in_the_form_and_marks_the_post(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    policy = TenantAccessPolicy(channel_rules={CHANNEL_ID: ChannelRule(readers="inside")})
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    async with _running(db_session_factory, teams_api_fake) as service:
        card = await _form(service, teams_api_fake, make_channel_activity(text="support"))
        await _send(service, _token(card), "help")
        chat = await _form(service, teams_api_fake, make_message_activity(text="support"))

    assert support.SEALED_HINT in card, "told the note leaves the channel before sending it"
    assert support.SEALED_HINT not in chat, "the 1:1 chat is nobody's sealed channel"
    [posted] = _posts_to(teams_api_fake, OPS)
    assert support.SEALED_LINE in posted, "whoever picks it up knows to answer there"


async def test_support_from_a_channel_is_refused_to_someone_who_could_not_ask_there(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    policy = TenantAccessPolicy(invoker_user_ids=(OTHER_AAD_OBJECT_ID,))
    async with db_session_factory.begin() as session:
        await set_access_policy(session, tenant_id=TENANT, policy=policy)
    async with _running(db_session_factory, teams_api_fake) as service:
        reply = await _form(service, teams_api_fake, make_channel_activity(text="support"))

    assert support.NOT_ALLOWED_THERE in reply and '"ask"' not in reply, "no form to send"
    assert await _rows(db_session_factory) == []
