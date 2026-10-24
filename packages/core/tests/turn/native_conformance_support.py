"""Unchanged native host-case assertions shared by the separate provider cuts."""

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn import run as run_module
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.running import RunningTurns
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_conformance import (
    TerminationCase,
    WakeEnvelope,
    termination_evidence,
    wake_evidence,
)
from daimon.testing.turn_fakes import RecordingLifecycle, deps_for_pinned_mux
from mux.contracts.ids import ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.errors import ScopeViolation

from .conformance_support import ACCOUNT, REVISION, TENANT, admission, record_usage
from .test_conformance import SCOPE, session, terminal


@pytest.mark.parametrize("journal_mode", ["matched", "omitted", "changed-payload"])
@pytest.mark.parametrize("ci_shape", [False, True])
async def test_c17_real_wake_takeover_binding_generation_and_driver_lease(
    host, monkeypatch, provider, journal_mode, ci_shape, run_host
):
    from daimon.testing.db import isolated_test_schema
    from sqlalchemy import text

    engine = host.kw["bind"]
    physical_database = engine.url.database
    if ci_shape:
        # Change URL metadata only. The actual connection creator still targets
        # this worker's owned lane DB/schema; never open the local shared DB.
        assert isolated_test_schema(engine) is not None
        monkeypatch.setattr(engine.sync_engine, "url", engine.url.set(database="daimon_test"))
        for flag in ("GITHUB_ACTIONS", "CI"):
            monkeypatch.setenv(flag, "true")
    async with host() as db:
        assert await db.scalar(text("SELECT current_database()")) == physical_database
        assert await db.scalar(text("SELECT current_schema()")) == isolated_test_schema(engine)
    store = PostgresStateStore(host)
    profile = "openai.persistent_workspace" if provider == "openai" else "gemini.inline_reuse"
    before = ProviderBinding(
        id="binding",
        thread=ThreadRef(channel=REVISION.channel, thread_id="thread"),
        provider=provider,
        profile=profile,
        native_refs={"session": "old-session"},
        generation=1,
        config_revision=1,
        legacy_account_id=str(ACCOUNT),
    )
    await store.put_binding(before, expected_generation=0)
    replacement = before.model_copy(update={"generation": 2, "native_refs": {"session": "session"}})
    request = ContinuationRequest(
        tenant_id=TENANT,
        platform="slack",
        parent_channel_id="channel",
        thread_id="thread",
        requester_account_id=ACCOUNT,
        requester_external_user_id="caller",
        target_ma_agent_id="agent",
        target_name="offline",
        requested_work="offline wake",
        reason="task_handoff",
        idempotency_key=uuid.uuid4(),
    )
    event = terminal(provider, "completed")
    recorded = (
        event.model_copy(update={"payload": dict(event.payload) | {"native_reason": "different"}})
        if journal_mode == "changed-payload"
        else event
    )

    async def run(persistence):
        result = await run_host(
            monkeypatch, recorded, persistence if journal_mode != "omitted" else None
        )
        assert result.termination == TerminationReason.COMPLETED

    envelope = WakeEnvelope(
        host, store, SCOPE, request, replacement, session(provider), event, datetime.now(UTC), run
    )
    facts = await wake_evidence(envelope)
    assert facts["before_generation"] == 1
    assert facts["current_generation"] == facts["replacement_generation"] == 2
    assert facts["stale_binding_refused"] and facts["stale_pump_calls"] == 0
    assert not facts["stale_wake_started"] and not facts["stale_wake_settled"]
    assert facts["wake_started"] and facts["wake_settled"]
    assert facts["wake_status"] == "delivered" and facts["pump_calls"] == 1
    assert facts["journal_event_present"] is (journal_mode == "matched")
    assert facts["lease_released"]
    with pytest.raises(ValueError, match="next generation"):
        await wake_evidence(envelope)
    with pytest.raises(ScopeViolation):
        await wake_evidence(
            replace(envelope, scope=SCOPE.model_copy(update={"account_id": "foreign"}))
        )


async def test_collector_joins_actual_host_pump_and_prepared_observation(
    host, monkeypatch, provider, run_host
):
    # The actual driver/pump and public prepared observation run together. The
    # scripted TurnIO is a host plumbing control, not native-stream certification.

    # Main #823 (c2300c7dbf59b12cb10e1975a6f96fe64e2495b3) adds
    # prepared-wrapper drain tracking. The dependency stub must supply the
    # actual registry so this scripted attempt is reached, not refused early.
    running = RunningTurns()

    async def attempt(*args, **kwargs):
        assert running.count() == 1
        state = await run_host(monkeypatch, terminal(provider, "completed"))
        return RunOutcome(state, "session", None, False)

    monkeypatch.setattr(run_module, "run_prepared_turn_impl", attempt)
    prepared = PreparedTurn(admission(), "session", None, None, False, ACCOUNT, record_usage)

    async def no_reseed():
        raise AssertionError("completed control must not recover")

    async def run():
        return await run_module.run_prepared_turn(
            replace(deps_for_pinned_mux(None, host), running_turns=running),
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="offline",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        )

    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=[TerminationReason.COMPLETED],
        cases=[
            TerminationCase(
                "host-pump", TerminationReason.COMPLETED, session(provider, "session"), run
            )
        ],
    )
    assert running.count() == 0
    row = facts["terminations"][0]
    assert row["actual_reason"] == row["persisted_reason"] == row["expected_reason"] == "completed"
    assert row["persisted_rows"] == 1
    assert row["actual_session"] == row["persisted_session"] == row["expected_session"] == "session"
    assert row["persisted_account"] == str(ACCOUNT)
