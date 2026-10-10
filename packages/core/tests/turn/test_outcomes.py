"""Content-free outcomes are once-only and never put database I/O on a turn."""

import asyncio
from dataclasses import asdict
from types import ModuleType
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from daimon.core._models import TurnOutcome
from daimon.core.errors import TurnError
from daimon.core.stores.turn_outcomes import list_for_tenant, record
from daimon.core.turn.driver import run_turn
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes, observe_turn, record_refusal
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.termination import TerminationReason
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from .conftest import make_agent_message, make_end_turn, make_status_idle


async def test_delivery_failure_after_agent_completion_is_recorded_as_failure(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    class FailedDelivery(RecordingLifecycle):
        async def on_terminal_success(self, state):
            await super().on_terminal_success(state)
            raise TurnError(kind="delivery_failed", cause=OSError("platform unavailable"))

    tenant = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine)
    client, lifecycle = FakeAnthropic(), FailedDelivery()
    client.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_agent_message(event_id="answer", text="The answer is ready.")),
            YieldEvent(make_status_idle(event_id="done", stop_reason=make_end_turn())),
        ]
    ]
    with observe_turn(sm, tenant_id=tenant.id, platform="teams") as observation:
        result = await run_turn(
            anthropic=cast(AsyncAnthropic, client),
            session_id="sesn_delivery",
            user_message="answer please",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=BillingExempt(reason="test"),
        )
        observation.finish(state=result)
    await drain_outcomes()
    async with sm() as session:
        rows = await list_for_tenant(session, tenant.id)
    assert len(rows) == 1 and rows[0].reason == TerminationReason.DELIVERY_FAILED
    assert rows[0].error_class == "TurnError"
    assert result.content[0].text == "The answer is ready."
    assert lifecycle.terminal_failures == [], "the delivery adapter already handled its notice"


@pytest.mark.parametrize("platform", ["discord", "slack", "teams", "scheduler", "headless"])
async def test_refusal_written_once_and_no_content_columns(
    db_session: AsyncSession, db_engine: AsyncEngine, platform: str
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    with observe_turn(
        sm, tenant_id=tenant.id, platform=platform, channel_id="channel", thread_id="thread"
    ):
        record_refusal(
            sm,
            tenant_id=tenant.id,
            platform=platform,
            channel_id="channel",
            reason=TerminationReason.ADMISSION_CAP_EXCEEDED,
        )
    await drain_outcomes()
    async with sm() as session:
        rows = await list_for_tenant(session, tenant.id)
    assert len(rows) == 1 and rows[0].reason == TerminationReason.ADMISSION_CAP_EXCEEDED
    assert rows[0].model_calls == 0 and rows[0].input_tokens == 0
    assert rows[0].cost_usd == 0 and rows[0].billing_posture == "none"
    async with sm() as session, session.begin():
        await record(session, rows[0])
    async with sm() as session:
        assert len(await list_for_tenant(session, tenant.id)) == 1
    assert set(asdict(rows[0])) == set(TurnOutcome.__table__.columns.keys())
    assert not {"content", "message", "prompt", "response", "error_message", "tool_input"} & set(
        TurnOutcome.__table__.columns.keys()
    )


async def test_database_failure_is_logged_without_error_message_and_turn_does_not_wait(
    db_session: AsyncSession, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.core.turn.outcomes as outcomes

    tenant = await make_tenant(db_session)
    await db_session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    async def failing_record(*args, **kwargs):
        started.set()
        await release.wait()
        raise RuntimeError("PRIVATE VALUE MUST NOT BE LOGGED")

    monkeypatch.setattr(outcomes, "record", failing_record)
    observation = TurnObservation(async_sessionmaker(db_engine), tenant.id, "discord")
    with capture_logs() as logs:
        observation.finish(reason=TerminationReason.COMPLETED)
        # finish returned while the DB operation has not even started.
        assert not started.is_set()
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()
        await drain_outcomes()
    assert any(log["event"] == "turn.outcome_write_failed" for log in logs)
    assert "PRIVATE VALUE" not in str(logs)


async def test_error_between_pipeline_stages_is_recorded(
    db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    sm = async_sessionmaker(db_engine)
    with (
        pytest.raises(ValueError),
        observe_turn(sm, tenant_id=tenant.id, platform="slack", origin="handoff"),
    ):
        raise ValueError("unpersisted message")
    await drain_outcomes()
    async with sm() as session:
        rows = await list_for_tenant(session, tenant.id)
    assert len(rows) == 1
    assert rows[0].reason == TerminationReason.UNKNOWN
    assert rows[0].error_class == "ValueError" and rows[0].origin == "handoff"
    assert "unpersisted" not in str(rows[0])


async def test_writer_timeout_and_queue_pressure_do_not_hold_turns(
    db_session: AsyncSession, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import daimon.core.turn.outcomes as outcomes

    tenant = await make_tenant(db_session)
    await db_session.commit()
    started = asyncio.Event()

    async def hung_record(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(outcomes, "record", hung_record)
    monkeypatch.setattr(outcomes, "_WRITE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(outcomes, "_MAX_PENDING", 1)
    sm = async_sessionmaker(db_engine)
    with capture_logs() as logs:
        TurnObservation(sm, tenant.id, "discord").finish()
        TurnObservation(sm, tenant.id, "slack").finish()
        await asyncio.wait_for(drain_outcomes(), timeout=2)
    assert started.is_set()
    assert any(log["event"] == "turn.outcome_queue_full" for log in logs)
    assert any(log.get("error_class") == "TimeoutError" for log in logs)


def test_model_and_migration_reason_constraints_match_the_enum() -> None:
    import ast
    import re
    from pathlib import Path

    from sqlalchemy import CheckConstraint

    expected = {reason.value for reason in TerminationReason}
    model = next(
        c
        for c in TurnOutcome.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_turn_outcomes_reason"
    )
    assert set(re.findall(r"'([^']+)'", str(model.sqltext))) == expected
    migration = Path(__file__).parents[2] / "alembic/versions/0029_sys081_turn_outcomes.py"
    tree = ast.parse(migration.read_text())
    checks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "CheckConstraint"
        and any(
            kw.arg == "name" and ast.literal_eval(kw.value) == "ck_turn_outcomes_reason"
            for kw in node.keywords
        )
    ]
    assert len(checks) == 1
    created = set(re.findall(r"'([^']+)'", ast.literal_eval(checks[0].args[0])))
    latest = _migration("0077_delivery_failure_outcome.py").OUTCOME_REASONS
    assert created < expected, "0029 created the constraint with a subset of today's reasons"
    assert set(latest) == expected, "the latest migration that widens the constraint matches"


def _migration(name: str) -> ModuleType:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[2] / "alembic/versions" / name
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
