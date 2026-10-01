"""DB-backed tests for `daimon.core.stores.session_preparations`.

A preparation exists so a replacement that costs a billed turn can be resumed
rather than repeated, so the tests are about identity (retrying the same target
finds the same row) and about not losing what a completed stage produced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from daimon.core._models import SessionPreparation
from daimon.core.stores.session_preparations import (
    advance_stage,
    delete_preparation,
    fail_preparation,
    get_preparation,
    upsert_preparation,
)
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.testing.factories import make_tenant
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def mapping_id(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    row = await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="prep-thread",
        account_id=uuid.uuid4(),
        ma_session_id="sess_prep",
    )
    return row.id


async def test_upserting_the_same_target_twice_returns_one_row_and_counts_the_retry(
    db_session: AsyncSession,
    mapping_id: uuid.UUID,
) -> None:
    first = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-a"
    )
    second = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-a"
    )

    assert second.id == first.id, (
        "retrying the same replacement must resume one row, never queue a second"
    )
    assert first.attempts == 0, "the first claim is not a retry"
    assert second.attempts == 1, "a repeat claim counts as a retry, which is what backoff reads"
    assert second.stage == "decided", "a retry must not rewind a stage the first attempt reached"


async def test_a_different_target_starts_its_own_preparation(
    db_session: AsyncSession,
    mapping_id: uuid.UUID,
) -> None:
    first = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-a"
    )
    second = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-b"
    )

    assert second.id != first.id, (
        "a target that moved under us must not resume into the configuration nobody asked for"
    )


async def test_a_duplicate_preparation_row_is_refused_by_the_database(
    db_session: AsyncSession,
    mapping_id: uuid.UUID,
) -> None:
    await upsert_preparation(db_session, mapping_id=mapping_id, target_fingerprint="ident-a")
    db_session.add(
        SessionPreparation(
            mapping_id=mapping_id,
            target_fingerprint="ident-a",
            stage="decided",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_advancing_a_stage_keeps_what_an_earlier_stage_produced(
    db_session: AsyncSession,
    mapping_id: uuid.UUID,
) -> None:
    prepared = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-a"
    )
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    await advance_stage(
        db_session,
        id=prepared.id,
        stage="uploaded",
        now=now,
        transfer_file_id="file_bundle",
        transfer_kind="full",
    )
    await advance_stage(db_session, id=prepared.id, stage="created", now=now)

    stored = await get_preparation(db_session, mapping_id=mapping_id, target_fingerprint="ident-a")
    assert stored is not None, "the preparation must still be readable by its target"
    assert stored.stage == "created", "the latest stage wins"
    assert stored.transfer_file_id == "file_bundle", (
        "a later stage must not erase the bundle an earlier one uploaded"
    )
    assert stored.transfer_kind == "full", "nor how complete that bundle was"


async def test_a_failed_preparation_records_why_and_keeps_its_transfer(
    db_session: AsyncSession,
    mapping_id: uuid.UUID,
) -> None:
    prepared = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-a"
    )
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    await advance_stage(
        db_session,
        id=prepared.id,
        stage="uploaded",
        now=now,
        transfer_file_id="file_bundle",
        transfer_kind="full",
    )

    await fail_preparation(db_session, id=prepared.id, reason="sessions.create timed out", now=now)

    stored = await get_preparation(db_session, mapping_id=mapping_id, target_fingerprint="ident-a")
    assert stored is not None, "a failed preparation is kept so the next attempt can back off"
    assert stored.stage == "failed", "the stage says the attempt stopped"
    assert stored.failure_reason == "sessions.create timed out", (
        "the reason is what the caller is told, so it must survive"
    )
    assert stored.transfer_file_id == "file_bundle", (
        "a failure must not discard the workspace that was already saved"
    )


async def test_deleting_a_preparation_frees_the_target_to_be_prepared_again(
    db_session: AsyncSession,
    mapping_id: uuid.UUID,
) -> None:
    prepared = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-a"
    )

    await delete_preparation(db_session, id=prepared.id)

    assert (
        await get_preparation(db_session, mapping_id=mapping_id, target_fingerprint="ident-a")
        is None
    ), "a deleted preparation must be gone"
    reclaimed = await upsert_preparation(
        db_session, mapping_id=mapping_id, target_fingerprint="ident-a"
    )
    assert reclaimed.attempts == 0, "the same target may be prepared again from scratch"
