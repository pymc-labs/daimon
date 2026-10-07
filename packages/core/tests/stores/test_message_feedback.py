"""Integration tests for the message_feedback store — real Postgres.

Covers the upsert-with-previous-vote-report, the free-text ownership gate,
and the erasure-scoped delete/count pair (including the account_id=NULL
regression this pair exists to close).
"""

from __future__ import annotations

import uuid

from daimon.core._models import MessageFeedback
from daimon.core.stores import message_feedback as store
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def test_first_record_vote_inserts_one_row_and_reports_no_previous_vote(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    result = await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-1",
        channel_id="chan-1",
        platform_user_id="voter-1",
        account_id=account.id,
        ma_session_id="sess_a",
        vote="up",
    )

    assert result.previous_vote is None, "a first-ever vote must report no previous vote"
    assert result.row.vote == "up"

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant.id, MessageFeedback.message_id == "msg-1")
        )
    ).scalar_one()
    assert count == 1


async def test_re_vote_with_opposite_value_leaves_one_row_and_reports_old_value(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-2",
        channel_id="chan-1",
        platform_user_id="voter-1",
        account_id=account.id,
        ma_session_id="sess_a",
        vote="up",
    )

    result = await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-2",
        channel_id="chan-1",
        platform_user_id="voter-1",
        account_id=account.id,
        ma_session_id="sess_a",
        vote="down",
    )

    assert result.previous_vote == "up", "must report the voter's prior vote"
    assert result.row.vote == "down", "the row must now hold the new vote"

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant.id, MessageFeedback.message_id == "msg-2")
        )
    ).scalar_one()
    assert count == 1, "a re-vote must leave exactly one row"


async def test_re_vote_does_not_clear_previously_attached_feedback_text(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    first = await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-3",
        channel_id="chan-1",
        platform_user_id="voter-1",
        account_id=account.id,
        ma_session_id="sess_a",
        vote="down",
    )
    await store.attach_feedback_text(
        db_session,
        feedback_id=first.row.id,
        platform_user_id="voter-1",
        feedback_text="the chart axes were mislabeled",
    )

    result = await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-3",
        channel_id="chan-1",
        platform_user_id="voter-1",
        account_id=account.id,
        ma_session_id="sess_a",
        vote="up",
    )

    assert result.row.feedback_text == "the chart axes were mislabeled", (
        "a re-vote must not erase text the voter already wrote"
    )


async def test_two_different_platform_user_ids_on_same_message_produce_two_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-4",
        channel_id="chan-1",
        platform_user_id="voter-a",
        account_id=account.id,
        ma_session_id=None,
        vote="up",
    )
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-4",
        channel_id="chan-1",
        platform_user_id="voter-b",
        account_id=account.id,
        ma_session_id=None,
        vote="down",
    )

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant.id, MessageFeedback.message_id == "msg-4")
        )
    ).scalar_one()
    assert count == 2, "two different voters on one message must produce two rows"


async def test_same_message_and_voter_under_two_tenants_produce_two_rows(
    db_session: AsyncSession,
) -> None:
    tenant_a = await make_tenant(db_session, workspace_id="tenant-a")
    tenant_b = await make_tenant(db_session, workspace_id="tenant-b")
    account_a = await make_account(db_session, tenant=tenant_a)
    account_b = await make_account(db_session, tenant=tenant_b)
    await store.record_vote(
        db_session,
        tenant_id=tenant_a.id,
        platform="discord",
        message_id="msg-shared",
        channel_id="chan-1",
        platform_user_id="voter-cross-tenant",
        account_id=account_a.id,
        ma_session_id=None,
        vote="up",
    )
    await store.record_vote(
        db_session,
        tenant_id=tenant_b.id,
        platform="discord",
        message_id="msg-shared",
        channel_id="chan-1",
        platform_user_id="voter-cross-tenant",
        account_id=account_b.id,
        ma_session_id=None,
        vote="up",
    )

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageFeedback)
            .where(MessageFeedback.message_id == "msg-shared")
        )
    ).scalar_one()
    assert count == 2, "the same (message_id, platform_user_id) under two tenants must not collide"


async def test_record_vote_with_no_account_and_no_session_succeeds(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    result = await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-5",
        channel_id="chan-1",
        platform_user_id="voter-no-account",
        account_id=None,
        ma_session_id=None,
        vote="up",
    )

    assert result.row.account_id is None
    assert result.row.ma_session_id is None


async def test_attach_feedback_text_with_right_id_and_right_user_updates_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    voted = await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-6",
        channel_id="chan-1",
        platform_user_id="voter-1",
        account_id=account.id,
        ma_session_id=None,
        vote="down",
    )

    updated = await store.attach_feedback_text(
        db_session,
        feedback_id=voted.row.id,
        platform_user_id="voter-1",
        feedback_text="the numbers looked off",
    )

    assert updated is not None
    assert updated.feedback_text == "the numbers looked off"


async def test_attach_feedback_text_with_different_platform_user_returns_none_and_leaves_row(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    voted = await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-7",
        channel_id="chan-1",
        platform_user_id="voter-1",
        account_id=account.id,
        ma_session_id=None,
        vote="down",
    )

    result = await store.attach_feedback_text(
        db_session,
        feedback_id=voted.row.id,
        platform_user_id="someone-else",
        feedback_text="attempted hijack",
    )

    assert result is None, "a mismatched platform_user_id must write nothing"

    orm = (
        await db_session.execute(select(MessageFeedback).where(MessageFeedback.id == voted.row.id))
    ).scalar_one()
    assert orm.feedback_text is None, "the row's feedback_text must be unchanged"


async def test_attach_feedback_text_for_deleted_row_returns_none(
    db_session: AsyncSession,
) -> None:
    result = await store.attach_feedback_text(
        db_session,
        feedback_id=uuid.uuid4(),
        platform_user_id="voter-1",
        feedback_text="ghost row",
    )

    assert result is None


async def test_delete_for_account_with_empty_keys_deletes_only_account_keyed_rows(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-8",
        channel_id="chan-1",
        platform_user_id="voter-account-keyed",
        account_id=account.id,
        ma_session_id=None,
        vote="up",
    )
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-9",
        channel_id="chan-1",
        platform_user_id="voter-null-account",
        account_id=None,
        ma_session_id=None,
        vote="up",
    )

    deleted = await store.delete_message_feedback_for_account(
        db_session, account_id=account.id, platform_user_keys=[]
    )

    assert deleted == 1, "an empty key list must delete only the account-keyed row"


async def test_delete_for_account_with_matching_platform_user_key_also_deletes_null_account_row(
    db_session: AsyncSession,
) -> None:
    """The erasure-gap regression test."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-10",
        channel_id="chan-1",
        platform_user_id="voter-pre-account",
        account_id=None,
        ma_session_id=None,
        vote="up",
    )

    deleted = await store.delete_message_feedback_for_account(
        db_session,
        account_id=account.id,
        platform_user_keys=[(tenant.id, "voter-pre-account")],
    )

    assert deleted == 1, (
        "a matching (tenant_id, platform_user_id) key must reach a row whose account_id is NULL"
    )


async def test_delete_for_platform_user_ignores_account_id_and_stays_tenant_scoped(
    db_session: AsyncSession,
) -> None:
    """The principal-scoped delete must not reach the account's rows elsewhere.

    Purging ONE principal erases that person's votes in that tenant only —
    including the null-account ones — while the same account's rows under
    another tenant survive for that tenant's own principal to carry.
    """
    tenant_a = await make_tenant(db_session, workspace_id="mf-store-guild-a")
    tenant_b = await make_tenant(db_session, workspace_id="mf-store-guild-b")
    account = await make_account(db_session, tenant=tenant_a)
    for tenant_id, message_id, account_id in (
        (tenant_a.id, "msg-13", account.id),
        (tenant_a.id, "msg-14", None),
        (tenant_b.id, "msg-15", account.id),
    ):
        await store.record_vote(
            db_session,
            tenant_id=tenant_id,
            platform="discord",
            message_id=message_id,
            channel_id="chan-1",
            platform_user_id="voter-shared",
            account_id=account_id,
            ma_session_id=None,
            vote="down",
        )

    deleted = await store.delete_message_feedback_for_platform_user(
        db_session, tenant_id=tenant_a.id, platform_user_id="voter-shared"
    )

    assert deleted == 2, "both of tenant A's rows must go, account-keyed and null-account alike"
    surviving = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant_b.id)
        )
    ).scalar_one()
    assert surviving == 1, (
        "the same account's row under another tenant must survive a per-principal delete"
    )


async def test_count_for_account_matches_subsequent_delete(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-11",
        channel_id="chan-1",
        platform_user_id="voter-count-a",
        account_id=account.id,
        ma_session_id=None,
        vote="up",
    )
    await store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="msg-12",
        channel_id="chan-1",
        platform_user_id="voter-count-b",
        account_id=None,
        ma_session_id=None,
        vote="down",
    )
    keys = [(tenant.id, "voter-count-b")]

    count_before = await store.count_message_feedback_for_account(
        db_session, account_id=account.id, platform_user_keys=keys
    )
    deleted = await store.delete_message_feedback_for_account(
        db_session, account_id=account.id, platform_user_keys=keys
    )

    assert count_before == deleted == 2


async def _down_vote(session: AsyncSession, *, message_id: str) -> uuid.UUID:
    tenant = await make_tenant(session)
    voted = await store.record_vote(
        session,
        tenant_id=tenant.id,
        platform="slack",
        message_id=message_id,
        channel_id="C1",
        platform_user_id="voter-1",
        account_id=None,
        ma_session_id=None,
        vote="down",
    )
    return voted.row.id


async def test_attach_feedback_details_stores_text_and_reasons(db_session: AsyncSession) -> None:
    row_id = await _down_vote(db_session, message_id="msg-d1")

    updated = await store.attach_feedback_details(
        db_session,
        feedback_id=row_id,
        platform_user_id="voter-1",
        feedback_text="wrong totals",
        feedback_reasons=["inaccurate", "incomplete"],
    )

    assert updated is not None
    assert updated.feedback_text == "wrong totals"
    assert updated.feedback_reasons == ["inaccurate", "incomplete"]


async def test_attach_feedback_details_replaces_both_and_stores_no_reasons_as_null(
    db_session: AsyncSession,
) -> None:
    row_id = await _down_vote(db_session, message_id="msg-d2")
    await store.attach_feedback_details(
        db_session,
        feedback_id=row_id,
        platform_user_id="voter-1",
        feedback_text="first",
        feedback_reasons=["too_slow"],
    )

    updated = await store.attach_feedback_details(
        db_session,
        feedback_id=row_id,
        platform_user_id="voter-1",
        feedback_text=None,
        feedback_reasons=[],
    )

    assert updated is not None
    assert updated.feedback_text is None
    assert updated.feedback_reasons is None


async def test_attach_feedback_details_for_another_user_writes_nothing(
    db_session: AsyncSession,
) -> None:
    row_id = await _down_vote(db_session, message_id="msg-d3")

    result = await store.attach_feedback_details(
        db_session,
        feedback_id=row_id,
        platform_user_id="someone-else",
        feedback_text="hijack",
        feedback_reasons=["other"],
    )

    assert result is None
    orm = (
        await db_session.execute(select(MessageFeedback).where(MessageFeedback.id == row_id))
    ).scalar_one()
    assert orm.feedback_text is None and orm.feedback_reasons is None
