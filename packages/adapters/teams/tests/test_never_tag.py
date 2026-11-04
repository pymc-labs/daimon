"""Teams honours never-tag: no completion mention, no activity notice."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from daimon.core.stores import tag_opt_outs as store
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from microsoft_teams.api import Account, MentionEntity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import AAD_OBJECT_ID, THREAD_ID, FakeSender
from .test_activity_feed import REPLY_ID, _Feed  # pyright: ignore[reportPrivateUsage]
from .test_lifecycle import _posted  # pyright: ignore[reportPrivateUsage]


async def _tenant_with_asker(session: AsyncSession, *, opted_out: bool) -> Any:
    tenant = await make_tenant(session, platform="teams", workspace_id="T_NT_1")
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform="teams", external_id="aad-1", tenant=tenant, account=account
    )
    if opted_out:
        await store.set_opt_out(session, account_id=account.id, tenant_id=tenant.id)
    await session.commit()
    return tenant


async def _answer_activity(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], *, opted_out: bool
) -> tuple[FakeSender, Any]:
    tenant = await _tenant_with_asker(session, opted_out=opted_out)
    sender = FakeSender()
    lifecycle = await _posted(
        sender,
        completion_ping=True,
        requester=Account(id="aad-1", name="Ada"),
        requester_user_id="aad-1",
        sessionmaker=factory,
        tenant_id=tenant.id,
    )
    await lifecycle.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="Done.")]))
    return sender, lifecycle


def _mentions(sender: FakeSender) -> list[str]:
    return [
        e.mentioned.id
        for activity in sender.activities
        for e in activity.entities or []
        if isinstance(e, MentionEntity)
    ]


async def test_completion_when_asker_opted_out_then_answer_edits_the_card_without_a_mention(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    sender, lifecycle = await _answer_activity(db_session, db_session_factory, opted_out=True)

    assert _mentions(sender) == [], "no mention entity anywhere"
    assert all("<at>" not in (a.text or "") for a in sender.activities), "no at-markup either"
    answer = sender.activities[1]
    assert answer.id == "m-1" and "Done." in (answer.text or ""), (
        "the answer edits the card in place instead of posting fresh"
    )
    assert lifecycle.final_message_id == "m-1"


async def test_completion_when_opt_out_lookup_fails_then_answer_lands_without_a_mention(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    with patch(
        "daimon.adapters.teams.lifecycle.tag_opt_outs", AsyncMock(side_effect=OSError("db"))
    ):
        sender, lifecycle = await _answer_activity(db_session, db_session_factory, opted_out=False)

    assert _mentions(sender) == [], "an unchecked asker is not tagged"
    assert any("Done." in (a.text or "") for a in sender.activities), "the answer is delivered"
    assert lifecycle.final_message_id is not None, "the turn settled on the answer"


async def test_completion_when_asker_not_opted_out_then_answer_still_mentions(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    sender, _ = await _answer_activity(db_session, db_session_factory, opted_out=False)

    assert _mentions(sender) == ["aad-1"], "everyone else still gets it"


async def test_activity_notice_when_recipient_opted_out_then_none_is_sent() -> None:
    fake = _Feed(opted_out=lambda recipient: _is(recipient == AAD_OBJECT_ID))
    fake.feed.notify(
        THREAD_ID,
        REPLY_ID,
        recipient_id=AAD_OBJECT_ID,
        text="Result",
        preview="ok",
        chain=None,
    )
    await fake.settle()

    assert fake.requests == [], "the opted-out person gets no notice"

    other = _Feed(opted_out=lambda recipient: _is(recipient == AAD_OBJECT_ID))
    other.feed.notify(
        THREAD_ID, REPLY_ID, recipient_id="someone-else", text="Result", preview="ok", chain=None
    )
    await other.settle()

    assert len(other.requests) == 1, "everyone else still does"


async def _is(value: bool) -> bool:
    return value
