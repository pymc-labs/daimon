"""Removed orchestration cannot be configured, registered, or queued."""

from typing import cast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from daimon.core._models import Base
from daimon.core.agent_configuration import ConfigurationReference
from daimon.core.config import Settings
from daimon.core.stores.domain import AgentBackendRevision, ContinuationReason
from daimon.core.stores.task_continuations import record_continuation
from daimon.core.turn.gemini_deployment import GeminiPlan
from daimon.core.turn.openai_deployment import OpenAIDeployment
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from .test_agent_backend import owned as owned


def test_removed_configuration_and_runtime_fields_are_absent() -> None:
    assert "fleet" not in Settings.model_json_schema()["properties"]
    assert "fleet_grant" not in AgentBackendRevision.model_fields
    for plan in (GeminiPlan, OpenAIDeployment):
        assert "worker_materialization" not in plan.model_fields
    assert not {"fleet_run", "fleet_worker"} & Base.metadata.tables.keys()
    assert "fleet_grant" not in Base.metadata.tables["agent_backend_revision"].columns
    with pytest.raises(ValidationError):
        ConfigurationReference.model_validate(
            {"operation": "fleet_set", "expected_revision": 1, "preview_digest": "a" * 64}
        )


async def test_obsolete_completion_cannot_be_queued_even_before_schema_migration() -> None:
    session = MagicMock(spec=AsyncSession)
    with pytest.raises(ValueError, match="unsupported continuation reason"):
        await record_continuation(
            session,
            tenant_id=uuid4(),
            platform="discord",
            parent_channel_id="channel",
            thread_id="thread",
            requester_account_id=uuid4(),
            requester_external_user_id="requester",
            target_ma_agent_id="agent",
            target_name="agent",
            reason=cast(ContinuationReason, "worker_finished"),
            idempotency_key=uuid4(),
        )
    session.add.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.fresh_schema
async def test_existing_backend_revision_ignores_historical_grant_column(owned) -> None:
    from sqlalchemy import text

    from .test_agent_backend import enable

    service, agent, actor, sessions = owned
    await enable(service, agent, actor)
    saved = await service.set_backend(agent, "openai", 0, actor)
    async with sessions.begin() as db:
        await db.execute(
            text("ALTER TABLE agent_backend_revision ADD COLUMN IF NOT EXISTS fleet_grant JSONB")
        )
        await db.execute(
            text(
                "UPDATE agent_backend_revision SET fleet_grant = CAST(:grant AS jsonb) "
                "WHERE agent_id = :agent"
            ),
            {"agent": agent.agent_id, "grant": '{"legacy_only":true}'},
        )
    assert await service.current(agent) == saved


@pytest.mark.fresh_schema
async def test_usage_reports_read_retained_historical_origins(db_session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    from daimon.core._models import TurnOutcome
    from daimon.core.stores.turn_usage import list_turn_usage, usage_by_channel
    from daimon.testing.factories import make_tenant
    from sqlalchemy import insert, text

    # Recreate the old origin check only in this synthetic, isolated test schema.
    # No removal migration or existing database is changed by this test.
    await db_session.execute(
        text("ALTER TABLE turn_outcomes DROP CONSTRAINT ck_turn_outcomes_origin")
    )
    await db_session.execute(
        text(
            "ALTER TABLE turn_outcomes ADD CONSTRAINT ck_turn_outcomes_origin "
            "CHECK (origin IN ('chat', 'routine', 'relay', 'handoff', 'worker'))"
        )
    )
    tenant = await make_tenant(db_session)
    now = datetime.now(UTC)
    turn_id = uuid4()
    await db_session.execute(
        insert(TurnOutcome).values(
            id=turn_id,
            tenant_id=tenant.id,
            platform="discord",
            origin="worker",
            reason="completed",
            started_at=now,
            ended_at=now,
            duration_ms=0,
            recovered=False,
            release="historical",
            usage_refs=[],
        )
    )
    rows = await list_turn_usage(db_session, tenant_id=tenant.id, since=now - timedelta(days=1))
    assert [(row.id, row.origin) for row in rows] == [(turn_id, "worker")]
    groups = await usage_by_channel(db_session, tenant_id=tenant.id, since=now - timedelta(days=1))
    assert [(group.origin, group.turns) for group in groups] == [("worker", 1)]


@pytest.mark.fresh_schema
async def test_historical_completions_cannot_wake_or_block_ordinary_timers(
    db_session: AsyncSession,
) -> None:
    from datetime import UTC, datetime, timedelta

    from daimon.core.stores import task_continuations as queue
    from daimon.testing.factories import make_tenant
    from sqlalchemy import text

    # Seed a pre-removal queue in this private test schema, without running 0141.
    await db_session.execute(
        text("ALTER TABLE task_continuations DROP CONSTRAINT ck_task_continuations_reason")
    )
    await db_session.execute(
        text(
            "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
            "CHECK (reason IN ('task_handoff', 'private_input_applied', 'timer', "
            "'github_access_ready', 'worker_finished'))"
        )
    )
    tenant = await make_tenant(db_session)
    now = datetime.now(UTC)
    obsolete, ordinary = uuid4(), uuid4()
    for key, age in ((obsolete, 2), (ordinary, 1)):
        await record_continuation(
            db_session,
            tenant_id=tenant.id,
            platform="discord",
            parent_channel_id="channel",
            thread_id="thread",
            requester_account_id=uuid4(),
            requester_external_user_id="requester",
            target_ma_agent_id="agent",
            target_name="agent",
            reason="timer",
            requested_work="continue",
            available_at=now - timedelta(hours=age),
            idempotency_key=key,
        )
    await db_session.execute(
        text("UPDATE task_continuations SET reason='worker_finished' WHERE idempotency_key=:key"),
        {"key": obsolete},
    )
    assert await queue.get_continuation(db_session, idempotency_key=obsolete) is None
    assert not await queue.claim_continuation(db_session, idempotency_key=obsolete, now=now)
    assert (
        await queue.claim_wake_row(
            db_session,
            idempotency_key=obsolete,
            owner="test",
            now=now,
            lease=timedelta(minutes=1),
            max_attempts=3,
        )
        is None
    )
    pending = await queue.list_pending_continuations(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread",
    )
    due = await queue.list_due_wake_rows(
        db_session,
        platform="discord",
        now=now,
        max_attempts=3,
        limit=1,
    )
    assert [row.idempotency_key for row in pending] == [ordinary]
    assert [row.idempotency_key for row in due] == [ordinary]
    assert await queue.claim_continuation(db_session, idempotency_key=ordinary, now=now)
