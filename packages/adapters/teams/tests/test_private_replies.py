"""Commands and sender-only notices answered where only the sender sees them."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.card_actions import edit_origin_card, text_card
from daimon.adapters.teams.commands import (
    ANSWERED_IN_CHAT,
    NEW_IN_CHANNEL,
    parse_command,
)
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import send_error_details
from daimon.adapters.teams.private_replies import (
    NOT_YET_PRIVATE,
    UNSHOWN,
    PrivateReplies,
    reply_privately,
)
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from microsoft_teams.api import (
    Account,
    ApiClient,
    ConversationAccount,
    ConversationReference,
    MessageActivity,
    MessageActivityInput,
    TargetedMessageInfoEntity,
)
from microsoft_teams.apps import ActivityContext
from microsoft_teams.common import Client, ClientOptions
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    GROUP_CHAT_ID,
    SERVICE_URL,
    THREAD_ID,
    FakeSender,
    TeamsApiFake,
    bot_token,
    build_teams_runtime,
    make_channel_activity,
    make_group_activity,
    make_inbound,
    make_message_activity,
    shown_text,
)

TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
SENDER_ID = "29:sender"
REFUSALS = [400, 403, 404]


@dataclasses.dataclass
class _Direct:
    looked_up: int = 0

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        self.looked_up += 1
        return "29:member"

    async def open_chat(self, member_id: str) -> str:
        return "a:direct"

    async def post(self, conversation_id: str, text: str) -> None:
        raise AssertionError("commands answer through the sender")


def _channel(text: str, *, targeted: bool = False) -> TeamsInbound:
    inbound = make_inbound(text, conversation=THREAD_ID, kind="channel")
    return dataclasses.replace(
        inbound, channel_id=CHANNEL_ID, sender_id=SENDER_ID, is_targeted=targeted
    )


def _replies(
    sender: FakeSender,
    seen: list[TeamsInbound],
    *,
    runtime: TeamsRuntime | None = None,
    direct: _Direct | None = None,
) -> PrivateReplies:
    async def _answer(context: Any) -> None:
        seen.append(context.inbound)
        await context.send(MessageActivityInput(text="answer"))

    names = ("help", "memory", "setup", "new")
    return PrivateReplies(
        runtime=runtime or cast(TeamsRuntime, SimpleNamespace()),
        sender=sender,
        commands=dict.fromkeys(names, _answer),
        direct=cast(Any, direct),
        is_admin=AsyncMock(return_value=False),
    )


def _is_private(activity: MessageActivityInput) -> bool:
    return activity.recipient is not None and activity.recipient.is_targeted is True


async def test_a_notice_reaches_only_its_sender() -> None:
    sender = FakeSender()
    await _replies(sender, []).tell_sender(_channel("hi"), "only you")
    [(where, notice, _)] = sender.sent
    assert where == THREAD_ID and shown_text(notice) == "only you" and _is_private(notice)
    assert notice.recipient is not None and notice.recipient.id == SENDER_ID


@pytest.mark.parametrize("status", REFUSALS)
async def test_a_refused_notice_is_posted_in_the_thread(status: int) -> None:
    sender = FakeSender(refuse_on={0}, refuse_status=status)
    await _replies(sender, []).tell_sender(_channel("hi"), "only you")
    first, second = sender.activities
    assert _is_private(first) and not _is_private(second) and shown_text(second) == "only you"


async def test_a_timed_out_notice_is_not_sent_twice() -> None:
    sender = FakeSender(timeout_on={1})
    replies = _replies(sender, [])
    await replies.tell_sender(_channel("hi"), "first")
    with pytest.raises(httpx.TimeoutException):
        await replies.tell_sender(_channel("hi"), "second")
    assert len(sender.sent) == 2


async def test_a_channel_command_is_answered_privately_in_its_thread() -> None:
    sender, seen, direct = FakeSender(), [], _Direct()
    await _replies(sender, seen, direct=direct).run(_channel("help"), TENANT, ("help", ""))
    [(where, answer, _)] = sender.sent
    assert where == THREAD_ID and shown_text(answer) == "answer" and _is_private(answer)
    assert [inbound.kind for inbound in seen] == ["channel"] and direct.looked_up == 0
    assert not answer.entities, "an @mention gets no prompt preview"


CHAT = "19:group-chat-1@thread.v2"


def _group(text: str) -> TeamsInbound:
    inbound = make_inbound(text, conversation=CHAT, kind="group")
    return dataclasses.replace(inbound, sender_id=SENDER_ID)


@pytest.mark.parametrize("name", ["help", "new"])
async def test_a_group_chat_command_is_answered_privately_in_the_chat(name: str) -> None:
    sender, seen, direct = FakeSender(), [], _Direct()
    await _replies(sender, seen, direct=direct).run(_group(name), TENANT, (name, ""))
    [(where, answer, _)] = sender.sent
    assert where == CHAT and shown_text(answer) == "answer" and _is_private(answer)
    assert [inbound.kind for inbound in seen] == ["group"] and direct.looked_up == 0


async def test_a_refused_private_new_in_a_group_chat_restarts_that_chat() -> None:
    sender, seen, direct = FakeSender(refuse_on={0}), [], _Direct()
    await _replies(sender, seen, direct=direct).run(_group("new"), TENANT, ("new", ""))
    _, (where, answer, _) = sender.sent
    assert where == CHAT and not _is_private(answer), "not the 1:1 chat's session"
    assert [inbound.kind for inbound in seen] == ["group", "group"] and direct.looked_up == 0


async def test_a_group_chat_refusal_reaches_only_its_sender() -> None:
    send = AsyncMock()
    ctx = _ctx(make_group_activity(), send)
    await reply_privately(ctx)("no")
    (notice,) = send.await_args.args
    assert shown_text(notice) == "no" and _is_private(notice)
    ctx.reply.assert_not_awaited()


async def test_a_targeted_command_previews_its_prompt() -> None:
    sender, inbound = FakeSender(), _channel("/help", targeted=True)
    await _replies(sender, []).run(inbound, TENANT, ("help", ""))
    [answer] = sender.activities
    assert _is_private(answer)
    assert any(isinstance(entity, TargetedMessageInfoEntity) for entity in answer.entities or [])


@pytest.mark.usefixtures("provisioned_tenant")
@pytest.mark.parametrize("status", REFUSALS)
async def test_a_refused_private_answer_moves_to_the_1_1_chat(
    db_session_factory: async_sessionmaker[AsyncSession], status: int
) -> None:
    sender, seen = FakeSender(refuse_on={0}, refuse_status=status), []
    runtime = build_teams_runtime(db_session_factory)
    replies = _replies(sender, seen, runtime=runtime, direct=_Direct())
    await replies.run(_channel("memory"), TENANT, ("memory", ""))
    _, pointer, answer = sender.sent
    assert shown_text(pointer[1]) == ANSWERED_IN_CHAT.format(name="memory") and _is_private(
        pointer[1]
    )
    assert answer[0] == "a:direct" and not _is_private(answer[1])
    assert [inbound.kind for inbound in seen] == ["channel", "dm"]


async def test_a_refused_forget_is_not_run_again() -> None:
    """`memory forget-mine <code>` deletes before it answers: a refused answer never reruns it."""
    sender, seen = FakeSender(refuse_on={0}, refuse_status=403), []
    replies = _replies(sender, seen, direct=_Direct())
    await replies.run(
        _channel("memory forget-mine a1b2c3"), TENANT, ("memory", "forget-mine a1b2c3")
    )
    assert len(seen) == 1, "the deletion runs once"
    notice = sender.sent[-1][1]
    assert shown_text(notice) == UNSHOWN.format(name="memory forget-mine a1b2c3")
    assert len(sender.sent) == 2, "no 1:1 chat answer follows"


@pytest.mark.usefixtures("provisioned_tenant")
async def test_setup_in_a_channel_keeps_the_1_1_chat(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender, seen = FakeSender(), []
    runtime = build_teams_runtime(db_session_factory)
    await _replies(sender, seen, runtime=runtime, direct=_Direct()).run(
        _channel("setup"), TENANT, ("setup", "")
    )
    [(_, pointer, _), (where, _, _)] = sender.sent
    assert shown_text(pointer) == ANSWERED_IN_CHAT.format(name="setup") and _is_private(pointer)
    assert where == "a:direct" and [inbound.kind for inbound in seen] == ["dm"]


class _RefusesThread(FakeSender):
    """Bot Framework answering the channel thread 400 with an error body, the 1:1 chat fine."""

    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> Any:
        if conversation_id != THREAD_ID:
            return await super().send(conversation_id, activity, service_url=service_url)
        self.sent.append((conversation_id, activity.model_copy(deep=True), service_url))
        request = httpx.Request("POST", f"https://teams.test/conversations/{conversation_id}")
        body = {"error": {"code": "BadArgument", "message": "secret text"}}
        response = httpx.Response(400, json=body, request=request)
        raise httpx.HTTPStatusError("refused", request=request, response=response)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_refused_pointer_still_gets_the_1_1_answer(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender, seen = _RefusesThread(), list[TeamsInbound]()
    runtime = build_teams_runtime(db_session_factory)
    replies = _replies(sender, seen, runtime=runtime, direct=_Direct())
    with structlog.testing.capture_logs() as logs:
        await replies.run(_channel("setup"), TENANT, ("setup", ""))
    assert [inbound.kind for inbound in seen] == ["dm"]
    assert sender.sent[-1][0] == "a:direct" and shown_text(sender.sent[-1][1]) == "answer"
    [failed] = [log for log in logs if log["event"] == "teams.notice.pointer_failed"]
    assert failed["status"] == 400 and failed["error_code"] == "BadArgument"
    assert "secret text" not in json.dumps(logs, default=str)


async def test_a_refused_pointer_without_a_1_1_chat_still_raises() -> None:
    seen: list[TeamsInbound] = []
    for direct in (None, _NoChat()):
        replies = _replies(_RefusesThread(), seen, direct=cast(Any, direct))
        with pytest.raises(httpx.HTTPStatusError):
            await replies.run(_channel("setup"), TENANT, ("setup", ""))
    assert seen == []


class _NoChat(_Direct):
    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        raise httpx.ConnectError("no chat")


def _status_error(content: bytes | str, status: int = 400) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://teams.test")
    response = httpx.Response(status, content=content, request=request)
    return httpx.HTTPStatusError("x", request=request, response=response)


def _code(code: object) -> bytes:
    return json.dumps({"error": {"code": code}}).encode()


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"{}",
        b"[]",
        json.dumps({"error": "BadArgument"}).encode(),
        _code("has space"),
        _code("A" * 65),
        _code("1Starts"),
        _code(7),
    ],
)
def test_an_unusable_error_body_gives_no_code(content: bytes) -> None:
    assert send_error_details(_status_error(content)) == (400, None)


def test_a_code_and_status_are_read_from_the_body() -> None:
    assert send_error_details(_status_error(_code("BotNotInConversationRoster"), 403)) == (
        403,
        "BotNotInConversationRoster",
    )
    assert send_error_details(_status_error(_code("A" * 64)))[1] == "A" * 64


def test_a_non_http_error_has_no_details() -> None:
    assert send_error_details(httpx.ConnectError("x")) == (None, None)
    assert send_error_details(TimeoutError()) == (None, None)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_1_1_command_is_answered_plainly(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    sender, seen = FakeSender(), []
    runtime = build_teams_runtime(db_session_factory)
    await _replies(sender, seen, runtime=runtime).run(make_inbound("help"), TENANT, ("help", ""))
    [answer] = sender.activities
    assert not _is_private(answer) and [inbound.kind for inbound in seen] == ["dm"]


async def test_new_in_a_channel_is_told_privately() -> None:
    sender = FakeSender()
    await _replies(sender, []).run(_channel("new"), TENANT, ("new", ""))
    [notice] = sender.activities
    assert shown_text(notice) == NEW_IN_CHANNEL and _is_private(notice)


@pytest.mark.usefixtures("provisioned_tenant")
async def test_a_targeted_question_starts_no_turn(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    sender = FakeSender()
    teams = TeamsApp(
        runtime=build_teams_runtime(db_session_factory),
        sender=sender,
        commands={},
        bot_token=bot_token,
    )

    async def _no_turn(*_: object) -> None:
        raise AssertionError("a targeted question must not start a turn")

    monkeypatch.setattr(TeamsApp, "_orchestrate", _no_turn)
    inbound = dataclasses.replace(_channel("what changed?", targeted=True), bot_name="Daimon")
    await teams._handle(inbound)
    [notice] = sender.activities
    assert shown_text(notice) == NOT_YET_PRIVATE.format(bot="Daimon") and _is_private(notice)


@pytest.mark.parametrize(
    ("text", "parsed"), [("/help", ("help", "")), ("/memory mine", ("memory", "mine"))]
)
def test_the_slash_form_parses_as_the_command(text: str, parsed: tuple[str, str]) -> None:
    assert parse_command(text, {"help": AsyncMock(), "memory": AsyncMock()}) == parsed


def _ctx(payload: dict[str, object], send: AsyncMock) -> Any:
    return SimpleNamespace(
        activity=MessageActivity.model_validate(payload), send=send, reply=AsyncMock()
    )


@pytest.mark.parametrize("targeted", [False, True])
async def test_a_channel_refusal_reaches_only_its_sender(targeted: bool) -> None:
    send = AsyncMock()
    ctx = _ctx(make_channel_activity(targeted=targeted), send)
    await reply_privately(ctx)("no")
    (notice,) = send.await_args.args
    assert shown_text(notice) == "no" and _is_private(notice)
    assert bool(notice.entities) is targeted, "only a targeted prompt is previewed"
    ctx.reply.assert_not_awaited()


async def test_a_refused_channel_refusal_is_replied_as_before() -> None:
    request = httpx.Request("POST", "https://teams.test")
    refused = httpx.HTTPStatusError(
        "x", request=request, response=httpx.Response(403, request=request)
    )
    ctx = _ctx(make_channel_activity(), AsyncMock(side_effect=refused))
    await reply_privately(ctx)("no")
    (public,) = ctx.reply.await_args.args
    assert shown_text(public) == "no" and not _is_private(public)


class _RefusesTargeted(TeamsApiFake):
    """Bot Framework refusing every targeted activity, as for a sender it cannot reach."""

    async def send(self, context: Any, next: Any) -> httpx.Response:
        response = await super().send(context, next)
        if "isTargetedActivity=true" in context.url:
            request = httpx.Request("POST", context.url)
            refused = httpx.Response(403, json={}, request=request)
            raise httpx.HTTPStatusError("refused", request=request, response=refused)
        return response


@pytest.mark.parametrize("kind", ["channel", "groupChat"])
async def test_a_refused_refusal_to_a_targeted_message_is_posted_for_everyone(kind: str) -> None:
    """The SDK targets a plain reply to a targeted message: the fallback must not be one."""
    wire = _RefusesTargeted()
    client = Client(ClientOptions(token="offline-token", headers={}))
    client.use(wire)
    payload = make_message_activity(
        conversation_id=THREAD_ID if kind == "channel" else GROUP_CHAT_ID,
        conversation_type=kind,
        targeted=True,
    )
    ctx = ActivityContext.__new__(ActivityContext)
    ctx.activity = MessageActivity.model_validate(payload)
    ctx.api = ApiClient(SERVICE_URL, client)
    ctx.conversation_ref = ConversationReference(
        channel_id="msteams",
        service_url=SERVICE_URL,
        bot=Account(id="offline-bot"),
        conversation=ConversationAccount(id=ctx.activity.conversation.id),
    )
    await reply_privately(ctx)("no")
    targeted_try, public = wire.activity_requests
    assert "isTargetedActivity=true" in targeted_try.url, "the sender alone first"
    assert "isTargetedActivity" not in public.url and public.body["attachments"][0]["content"][
        "fallbackText"
    ].endswith("no")


async def test_a_1_1_refusal_is_replied_as_before() -> None:
    send = AsyncMock()
    ctx = _ctx(make_message_activity(), send)
    await reply_privately(ctx)("no")
    (notice,) = ctx.reply.await_args.args
    assert shown_text(notice) == "no" and not _is_private(notice), "an error card, not targeted"
    ctx.reply.assert_awaited_once()
    send.assert_not_awaited()


@pytest.mark.parametrize(
    ("kind", "private"), [("channel", True), ("groupChat", True), ("personal", False)]
)
async def test_a_panel_is_edited_for_whoever_saw_it(kind: str, private: bool) -> None:
    send = AsyncMock()
    activity = SimpleNamespace(
        reply_to_id="m-1",
        conversation=SimpleNamespace(conversation_type=kind),
        from_=Account(id=SENDER_ID, name="Ada"),
    )
    await edit_origin_card(cast(Any, SimpleNamespace(activity=activity, send=send)), text_card("x"))
    (edit,) = send.await_args.args
    assert edit.id == "m-1" and _is_private(edit) is private
