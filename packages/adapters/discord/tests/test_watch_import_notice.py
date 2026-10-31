"""Imported watch notices use the existing private member-notice transport."""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest
from daimon.adapters.discord.budget_notice import send_member_notice, send_watch_notice
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.watch_imports import UNMAPPED_NOTICE
from daimon.core.stores.watches import REFUSED, UNCLEAR, NoticeAttempt, SentNotice


@pytest.mark.parametrize("closed,allowed", [(False, True), (True, True), (False, False)])
async def test_import_notice_is_owner_only_dm_with_no_channel_fallback(closed, allowed):
    tenant_id = uuid.uuid4()
    policy = DirectMessagePolicy() if allowed else DirectMessagePolicy(mode="disabled")
    runtime = SimpleNamespace(settings=SimpleNamespace(direct_message_policies={tenant_id: policy}))
    dm = SimpleNamespace(send=AsyncMock())
    open_dm = AsyncMock(return_value=dm)
    result = await send_member_notice(
        runtime,
        open_dm,
        lambda: closed,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text=UNMAPPED_NOTICE,
    )
    assert result is (allowed and not closed)
    if result:
        open_dm.assert_awaited_once_with(100, 123)
        assert dm.send.await_args.args == (UNMAPPED_NOTICE,)
        assert dm.send.await_args.kwargs["allowed_mentions"].everyone is False
    else:
        open_dm.assert_not_called()


def _attempt(since: datetime | None = None) -> NoticeAttempt:
    return NoticeAttempt(uuid.uuid4(), "expired", 1, 1, "n" * 25, since)


def _runtime(tenant_id: uuid.UUID) -> Any:
    policies = {tenant_id: DirectMessagePolicy()}
    return SimpleNamespace(settings=SimpleNamespace(direct_message_policies=policies))


def _history(
    *messages: SimpleNamespace, error: Exception | None = None
) -> Callable[..., AsyncIterator[SimpleNamespace]]:
    def history(**_kwargs: object) -> AsyncIterator[SimpleNamespace]:
        async def walk() -> AsyncIterator[SimpleNamespace]:
            if error is not None:
                raise error
            for message in messages:
                yield message

        return walk()

    return history


async def test_watch_notice_sends_with_a_stable_nonce_and_reports_the_message() -> None:
    tenant_id = uuid.uuid4()
    sent = SimpleNamespace(id=901, channel=SimpleNamespace(id=900))
    send = AsyncMock(return_value=sent)
    dm = SimpleNamespace(send=send, history=_history())
    result = await send_watch_notice(
        _runtime(tenant_id),
        AsyncMock(return_value=dm),
        lambda: False,
        bot_user_id=7,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text="Watch expired.",
        attempt=_attempt(),
    )
    assert result == SentNotice("900", "901")
    assert send.await_args is not None
    assert send.await_args.kwargs["nonce"] == "n" * 25
    assert send.await_args.kwargs["allowed_mentions"].everyone is False


@pytest.mark.parametrize(
    "author,content,resent",
    [(7, "Watch expired.", False), (8, "Watch expired.", True), (7, "other", True)],
)
async def test_resumed_watch_notice_reuses_the_bot_message_it_finds(
    author: int, content: str, resent: bool
) -> None:
    tenant_id = uuid.uuid4()
    earlier = SimpleNamespace(
        id=555, channel=SimpleNamespace(id=900), author=SimpleNamespace(id=author), content=content
    )
    new = SimpleNamespace(id=901, channel=SimpleNamespace(id=900))
    send = AsyncMock(return_value=new)
    dm = SimpleNamespace(send=send, history=_history(earlier))
    result = await send_watch_notice(
        _runtime(tenant_id),
        AsyncMock(return_value=dm),
        lambda: False,
        bot_user_id=7,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text="Watch expired.",
        attempt=_attempt(datetime.now(UTC)),
    )
    assert result is not None
    assert result.message_id == ("901" if resent else "555")
    assert send.await_count == int(resent)


@pytest.mark.parametrize("bot_user_id,error", [(7, True), (None, False)])
async def test_resumed_watch_notice_sends_nothing_when_it_cannot_check(
    bot_user_id: int | None, error: bool
) -> None:
    tenant_id = uuid.uuid4()
    response: Any = SimpleNamespace(status=500, reason="x")
    failure = discord.HTTPException(response, "x") if error else None
    send = AsyncMock()
    dm = SimpleNamespace(send=send, history=_history(error=failure))
    result = await send_watch_notice(
        _runtime(tenant_id),
        AsyncMock(return_value=dm),
        lambda: False,
        bot_user_id=bot_user_id,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text="Watch expired.",
        attempt=_attempt(datetime.now(UTC)),
    )
    assert result is None
    send.assert_not_awaited()


@pytest.mark.parametrize(
    "status,error,expected",
    [(500, False, UNCLEAR), (403, False, None), (None, True, UNCLEAR)],
)
async def test_watch_notice_send_that_may_have_landed_is_unclear(
    status: int | None, error: bool, expected: object
) -> None:
    tenant_id = uuid.uuid4()
    response: Any = SimpleNamespace(status=status, reason="x")
    failure: Exception = TimeoutError() if error else discord.HTTPException(response, "x")
    dm = SimpleNamespace(send=AsyncMock(side_effect=failure), history=_history())
    result = await send_watch_notice(
        _runtime(tenant_id),
        AsyncMock(return_value=dm),
        lambda: False,
        bot_user_id=7,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text="Watch expired.",
        attempt=_attempt(),
    )
    assert result == expected


def _discord_error(kind: type[discord.HTTPException], status: int, code: int) -> Exception:
    response: Any = SimpleNamespace(status=status, reason="x")
    return kind(response, {"code": code, "message": "x"})


@pytest.mark.parametrize(
    "open_error,expected",
    [
        (LookupError("the routine's creator is not a human member"), REFUSED),
        (_discord_error(discord.NotFound, 404, 10007), REFUSED),
        (_discord_error(discord.NotFound, 404, 10013), REFUSED),
        (_discord_error(discord.NotFound, 404, 10004), None),
        (_discord_error(discord.HTTPException, 503, 0), None),
    ],
)
async def test_watch_notice_to_an_owner_discord_cannot_reach_is_refused(
    open_error: Exception, expected: object
) -> None:
    tenant_id = uuid.uuid4()
    result = await send_watch_notice(
        _runtime(tenant_id),
        AsyncMock(side_effect=open_error),
        lambda: False,
        bot_user_id=7,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text="Watch expired.",
        attempt=_attempt(),
    )
    assert result == expected


@pytest.mark.parametrize("code,expected", [(50007, REFUSED), (50001, None)])
async def test_watch_notice_to_an_owner_with_closed_dms_is_refused(
    code: int, expected: object
) -> None:
    tenant_id = uuid.uuid4()
    send = AsyncMock(side_effect=_discord_error(discord.Forbidden, 403, code))
    dm = SimpleNamespace(send=send, history=_history())
    result = await send_watch_notice(
        _runtime(tenant_id),
        AsyncMock(return_value=dm),
        lambda: False,
        bot_user_id=7,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text="Watch expired.",
        attempt=_attempt(),
    )
    assert result == expected
