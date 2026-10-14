"""Committed C18 outcome evidence; scripted controls are not provider certificates."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from daimon.core._models import Tenant, TurnOutcome
from daimon.core.errors import TurnError
from daimon.core.turn import outcomes
from daimon.core.turn import run as run_module
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.outcomes import TurnObservation, current_outcome, drain_outcomes
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.run import RunOutcome
from daimon.core.turn.running import RunningTurns
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.factories import make_tenant
from daimon.testing.turn_conformance import TerminationCase, termination_evidence
from daimon.testing.turn_fakes import RecordingLifecycle, deps_for
from mux.contracts.ids import ResourceRef, Scope
from mux.errors import ScopeViolation
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .conformance_support import (
    ACCOUNT,
    TENANT,
    admission,
    record_usage,
    run_host_fixture_preflight_probe,
)

SCOPE = Scope(
    tenant_id=str(TENANT), account_id=str(ACCOUNT), principal_id="host", authorization_id="offline"
)


def session(provider="openai", id_="native-session"):
    return ResourceRef(
        provider=provider,
        kind="session",
        id=id_,
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
        account_scope_id="offline-project",
    )


@pytest.fixture(params=[False, True])
async def host(db_preflight, db_engine, db_clean, request):
    del db_preflight  # Admission precedes every engine/cleanup dependency.
    if db_engine.url.database == "daimon_test" and not (
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
    ):
        pytest.skip("host outcome proofs require an owned lane database or isolated CI schema")
    routes = {Tenant: db_engine, TurnOutcome.__table__: db_engine} if request.param else None
    sm = async_sessionmaker(db_engine, expire_on_commit=False, binds=routes)
    require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        await make_tenant(db, id=TENANT)
    return sm


def scripted_run(reason, *, native="native-session", account=ACCOUNT, recovered=False):
    """State/writer adapter control, deliberately without native normalization."""

    async def run():
        observation = current_outcome.get()
        assert observation is not None
        observation.account_id = account
        observation.session_id = native
        state = TurnState(
            termination=reason,
            error=TurnError(kind="upstream", message="private error content")
            if reason.is_failure
            else None,
        )
        return RunOutcome(state, native, None, True) if recovered else state

    return run


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_every_typed_reason_is_read_from_its_exact_committed_row(host, provider):
    # This is collector/writer coverage, not a claim that every provider can
    # reach every member. Provider owners supply their independent reachability.
    reasons = tuple(TerminationReason)
    cases = [
        TerminationCase(
            reason.value,
            reason,
            session(provider),
            scripted_run(reason),
            expected_error_class="TurnError" if reason.is_failure else None,
        )
        for reason in reasons
    ]
    facts = await termination_evidence(
        host, scope=SCOPE, platform="slack", required_reasons=reasons, cases=cases
    )
    assert facts["required_reasons"] == tuple(reason.value for reason in reasons)
    assert len(facts["terminations"]) == len(reasons)
    assert len({row["outcome_id"] for row in facts["terminations"]}) == len(reasons)
    for row, reason in zip(facts["terminations"], reasons, strict=True):
        assert row["persisted_rows"] == 1
        assert row["expected_reason"] == row["actual_reason"] == row["persisted_reason"] == reason
        assert row["expected_session"] == row["actual_session"] == row["persisted_session"]
        assert row["persisted_session"] == "native-session"
        assert row["expected_account"] == row["persisted_account"] == str(ACCOUNT)
        assert row["expected_tenant"] == row["persisted_tenant"] == str(TENANT)
        assert row["expected_platform"] == row["persisted_platform"] == "slack"
        assert (
            row["expected_recovered"]
            is row["actual_recovered"]
            is row["persisted_recovered"]
            is False
        )
        assert (
            row["expected_error_class"] == row["actual_error_class"] == row["persisted_error_class"]
        )
        assert "private error content" not in str(row)
    assert "pass" not in facts and "verdict" not in facts


async def test_partial_execution_keeps_the_full_independent_oracle_visible(host):
    required = list(TerminationReason)

    async def run():
        required.clear()
        return await scripted_run(TerminationReason.COMPLETED)()

    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=required,
        cases=[TerminationCase("one-root", TerminationReason.COMPLETED, session(), run)],
    )
    assert facts["required_reasons"] == tuple(reason.value for reason in TerminationReason)
    assert len(facts["terminations"]) == 1
    assert "pass" not in facts and "coverage_complete" not in facts


@pytest.mark.parametrize(
    "corruption",
    ["reason", "error", "session", "account", "tenant", "platform", "recovered", "id", "omit"],
)
async def test_expected_values_never_repair_writer_failure_or_wrong_rows(
    host, monkeypatch, corruption
):
    original = outcomes.record

    async def corrupt(db, row):
        if corruption == "omit":
            return
        change = {
            "reason": {"reason": TerminationReason.COMPLETED},
            "error": {"error_class": None},
            "session": {"session_id": "other-native-session"},
            "account": {"account_id": uuid.uuid4()},
            "tenant": {"tenant_id": None},
            "platform": {"platform": "discord"},
            "recovered": {"recovered": True},
            "id": {"id": uuid.uuid4()},
        }[corruption]
        await original(db, replace(row, **change))

    monkeypatch.setattr(outcomes, "record", corrupt)
    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=[TerminationReason.UPSTREAM],
        cases=[
            TerminationCase(
                "errored",
                TerminationReason.UPSTREAM,
                session(),
                scripted_run(TerminationReason.UPSTREAM),
                expected_error_class="TurnError",
            )
        ],
    )
    row = facts["terminations"][0]
    assert row["actual_reason"] == row["expected_reason"] == "upstream"
    if corruption in {"tenant", "id", "omit"}:
        assert row["persisted_rows"] == 0
        assert (
            row["persisted_reason"] is row["persisted_session"] is row["persisted_account"] is None
        )
    else:
        key = {
            "reason": "reason",
            "error": "error_class",
            "session": "session",
            "account": "account",
            "platform": "platform",
            "recovered": "recovered",
        }[corruption]
        assert row["persisted_rows"] == 1
        assert row["expected_" + key] != row["persisted_" + key]


async def test_swallowed_writer_exception_stays_missing_without_private_text(host, monkeypatch):
    async def broken(*args):
        raise RuntimeError("private database parameters")

    monkeypatch.setattr(outcomes, "record", broken)
    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=[TerminationReason.COMPLETED],
        cases=[
            TerminationCase(
                "missing",
                TerminationReason.COMPLETED,
                session(),
                scripted_run(TerminationReason.COMPLETED),
            )
        ],
    )
    assert facts["terminations"][0]["persisted_rows"] == 0
    assert facts["terminations"][0]["persisted_reason"] is None
    assert "private database parameters" not in str(facts)


@pytest.mark.parametrize("wrong", ["account", "session", "none"])
async def test_expected_attribution_never_seeds_actual_host_identity(host, wrong):
    async def run():
        observation = current_outcome.get()
        assert observation.account_id is observation.session_id is None
        if wrong != "none":
            observation.account_id = uuid.uuid4() if wrong == "account" else ACCOUNT
            observation.session_id = "wrong" if wrong == "session" else "native-session"
        return TurnState(termination=TerminationReason.COMPLETED)

    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=[TerminationReason.COMPLETED],
        cases=[TerminationCase("wrong-identity", TerminationReason.COMPLETED, session(), run)],
    )
    row = facts["terminations"][0]
    if wrong != "session":
        assert row["expected_account"] != row["persisted_account"]
    if wrong != "account":
        assert row["expected_session"] != row["persisted_session"]


async def test_pre_native_refusal_has_no_phantom_session(host):
    async def run():
        current_outcome.get().account_id = ACCOUNT
        raise AdmissionDenied(reason="writers_none")

    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=[TerminationReason.ADMISSION_CHANNEL_PROTECTED],
        cases=[
            TerminationCase(
                "refusal",
                TerminationReason.ADMISSION_CHANNEL_PROTECTED,
                None,
                run,
                expected_error_class="AdmissionDenied",
            )
        ],
    )
    row = facts["terminations"][0]
    assert row["actual_reason"] == row["persisted_reason"] == "admission_channel_protected"
    assert row["expected_session"] is row["actual_session"] is row["persisted_session"] is None
    assert row["persisted_error_class"] == row["actual_error_class"] == "AdmissionDenied"
    assert row["persisted_account"] == str(ACCOUNT)


async def test_unterminated_state_is_missing_truth_not_default_completion(host):
    async def run():
        return TurnState()

    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=[TerminationReason.COMPLETED],
        cases=[TerminationCase("no-terminal", TerminationReason.COMPLETED, None, run)],
    )
    row = facts["terminations"][0]
    assert row["actual_reason"] is None
    assert row["persisted_reason"] == "unknown"


async def test_real_prepared_boundary_owns_once_only_attribution_and_recovery(host, monkeypatch):
    # Actual public prepared wrapper and outcome writer, with the attempt's
    # implementation scripted. No native driver/reachability certification.

    # Main #823 (c2300c7dbf59b12cb10e1975a6f96fe64e2495b3) adds
    # prepared-wrapper drain tracking. The dependency stub must supply the
    # actual registry so this scripted attempt is reached, not refused early.
    running = RunningTurns()

    async def attempt(*args, **kwargs):
        assert running.count() == 1
        return RunOutcome(
            TurnState(termination=TerminationReason.COMPLETED), "recovered-native", None, True
        )

    monkeypatch.setattr(run_module, "run_prepared_turn_impl", attempt)
    prepared = PreparedTurn(
        admission(), "original-native", None, None, False, ACCOUNT, record_usage
    )

    async def no_reseed():
        raise AssertionError("scripted successful attempt does not request reseeding")

    async def run():
        return await run_module.run_prepared_turn(
            replace(deps_for(None, host), running_turns=running),
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
                "recovered",
                TerminationReason.COMPLETED,
                session(id_="recovered-native"),
                run,
                expected_recovered=True,
            )
        ],
    )
    assert running.count() == 0
    row = facts["terminations"][0]
    assert (
        row["expected_session"]
        == row["actual_session"]
        == row["persisted_session"]
        == "recovered-native"
    )
    assert (
        row["expected_recovered"] is row["actual_recovered"] is row["persisted_recovered"] is True
    )
    assert row["persisted_account"] == str(ACCOUNT)
    async with host() as db:
        stored = list(await db.scalars(select(TurnOutcome)))
    assert len(stored) == 1 and str(stored[0].id) == row["outcome_id"]
    assert stored[0].thread_id == "thread"


async def test_finalizer_reason_is_not_overwritten_by_exception_fallback(host):
    async def run():
        observation = current_outcome.get()
        observation.account_id = ACCOUNT
        observation.session_id = "native-session"
        error = RuntimeError("private recovery detail")
        observation.finish(error=error, reason=TerminationReason.RECOVERY_FAILED)
        raise error

    facts = await termination_evidence(
        host,
        scope=SCOPE,
        platform="slack",
        required_reasons=[TerminationReason.RECOVERY_FAILED],
        cases=[
            TerminationCase(
                "raised-after-finalizer",
                TerminationReason.RECOVERY_FAILED,
                session(),
                run,
                expected_error_class="RuntimeError",
            )
        ],
    )
    row = facts["terminations"][0]
    assert row["actual_reason"] == "unknown"  # Independent exception mapper fact.
    assert row["persisted_reason"] == row["expected_reason"] == "recovery_failed"
    assert row["persisted_error_class"] == "RuntimeError"
    assert "private recovery detail" not in str(facts)


async def test_unrelated_rows_are_never_used_as_this_execution_evidence(host):
    previous = TurnObservation(
        host, TENANT, "slack", account_id=ACCOUNT, session_id="native-session"
    )
    previous.finish(reason=TerminationReason.COMPLETED)
    await drain_outcomes()

    async def run():
        # A stale admission can finish another observation. Collector must not
        # search for a matching expected reason/session and adopt that row.
        return TurnState(termination=TerminationReason.COMPLETED)

    original = outcomes.record

    async def redirect(db, row):
        await original(db, replace(row, id=previous.id))

    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        patch.setattr(outcomes, "record", redirect)
        facts = await termination_evidence(
            host,
            scope=SCOPE,
            platform="slack",
            required_reasons=[TerminationReason.COMPLETED],
            cases=[TerminationCase("stale", TerminationReason.COMPLETED, session(), run)],
        )
    assert facts["terminations"][0]["persisted_rows"] == 0


@pytest.mark.parametrize(
    "foreign", ["kind", "tenant_id", "account_id", "platform", "legacy", "uuid"]
)
async def test_foreign_envelopes_refuse_before_any_case_or_sql(host, foreign):
    calls = connects = 0

    def connect(*args):
        nonlocal connects
        connects += 1

    event.listen(host.kw["bind"].sync_engine, "before_cursor_execute", connect)

    async def run():
        nonlocal calls
        calls += 1
        return TurnState()

    scope = SCOPE
    ref = session()
    if foreign in {"kind", "tenant_id", "account_id"}:
        ref = ref.model_copy(update={foreign: "agent" if foreign == "kind" else str(uuid.uuid4())})
    elif foreign == "platform":
        scope = Scope.platform(reason="offline")
    elif foreign == "legacy":
        scope = Scope.legacy_host_authorized(call_site="offline")
    else:
        scope = scope.model_copy(update={"account_id": "invalid-uuid"})
    try:
        with pytest.raises((ScopeViolation, ValueError)):
            await termination_evidence(
                host,
                scope=scope,
                platform="slack",
                required_reasons=[TerminationReason.COMPLETED],
                cases=[TerminationCase("foreign", TerminationReason.COMPLETED, ref, run)],
            )
        assert calls == connects == 0
    finally:
        event.remove(host.kw["bind"].sync_engine, "before_cursor_execute", connect)


@pytest.mark.parametrize(
    "bad",
    [
        "empty-reasons",
        "repeated-reasons",
        "untyped-reason",
        "empty-cases",
        "blank-name",
        "repeated-name",
        "undeclared",
        "blank-platform",
    ],
)
async def test_invalid_oracles_refuse_before_execution(host, bad):
    calls = 0

    async def run():
        nonlocal calls
        calls += 1
        return TurnState()

    reasons = [TerminationReason.COMPLETED]
    case = TerminationCase("one", TerminationReason.COMPLETED, session(), run)
    cases = [case]
    platform = "slack"
    if bad == "empty-reasons":
        reasons = []
    elif bad == "repeated-reasons":
        reasons *= 2
    elif bad == "untyped-reason":
        reasons = ["completed"]
    elif bad == "empty-cases":
        cases = []
    elif bad == "blank-name":
        cases = [replace(case, name=" ")]
    elif bad == "repeated-name":
        cases *= 2
    elif bad == "undeclared":
        cases = [replace(case, expected=TerminationReason.UPSTREAM)]
    else:
        platform = " "
    with pytest.raises(ValueError):
        await termination_evidence(
            host, scope=SCOPE, platform=platform, required_reasons=reasons, cases=cases
        )
    assert calls == 0


@pytest.mark.parametrize("route", ["primary", "model", "table", "connection"])
async def test_database_guard_checks_every_bind_before_connect_or_callback(route):
    owned = create_async_engine("postgresql+asyncpg://daimon:daimon@localhost/daimon_test_nc_N4")
    foreign = create_async_engine("postgresql+asyncpg://invalid:invalid@invalid/daimon_test")
    calls = connects = 0

    def connect(*args):
        nonlocal connects
        connects += 1
        raise AssertionError("guard must not connect")

    event.listen(owned.sync_engine, "do_connect", connect)
    event.listen(foreign.sync_engine, "do_connect", connect)
    routes = None
    primary = owned
    if route == "primary":
        primary = foreign
    elif route == "connection":
        primary = foreign.connect()  # Unstarted handle; no connection.
    else:
        routes = {TurnOutcome if route == "model" else TurnOutcome.__table__: foreign}
    sm = async_sessionmaker(primary, binds=routes)

    async def run():
        nonlocal calls
        calls += 1
        return TurnState()

    try:
        with pytest.raises(ValueError):
            await termination_evidence(
                sm,
                scope=SCOPE,
                platform="slack",
                required_reasons=[TerminationReason.COMPLETED],
                cases=[TerminationCase("guarded", TerminationReason.COMPLETED, session(), run)],
            )
        assert connects == calls == 0
    finally:
        await owned.dispose()
        await foreign.dispose()


@pytest.mark.parametrize("bad_result", ["untyped", "cancelled"])
async def test_callback_cancellation_and_untyped_values_do_not_become_completion(host, bad_result):
    async def run():
        if bad_result == "cancelled":
            raise asyncio.CancelledError
        return {"pass": True}

    with pytest.raises(asyncio.CancelledError if bad_result == "cancelled" else TypeError):
        await termination_evidence(
            host,
            scope=SCOPE,
            platform="slack",
            required_reasons=[TerminationReason.COMPLETED],
            cases=[TerminationCase("invalid", TerminationReason.COMPLETED, session(), run)],
        )
    assert current_outcome.get() is None
    async with host() as db:
        assert list(await db.scalars(select(TurnOutcome))) == []


@pytest.mark.parametrize("routed", [False, True])
@pytest.mark.parametrize(
    "flags",
    [(None, None), ("true", None), (None, "true"), ("true", "false"), ("TRUE", "true"), ("", "")],
)
async def test_outcome_fixture_skips_contributor_db_before_provisioning(monkeypatch, routed, flags):
    import sys
    from types import SimpleNamespace

    for name, value in zip(("GITHUB_ACTIONS", "CI"), flags, strict=True):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    engine = create_async_engine("postgresql+asyncpg://invalid:invalid@localhost/daimon_test")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(None)
        raise AssertionError("contributor fixture must skip before connecting or provisioning")

    event.listen(engine.sync_engine, "do_connect", forbidden)
    monkeypatch.setattr(sys.modules[__name__], "make_tenant", forbidden)
    try:
        with pytest.raises(pytest.skip.Exception, match="owned lane database or isolated CI"):
            await host.__wrapped__(None, engine, None, SimpleNamespace(param=routed))
        assert calls == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("routed", [False, True])
async def test_outcome_fixture_refuses_unregistered_ci_before_provisioning(monkeypatch, routed):
    import sys
    from types import SimpleNamespace

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    engine = create_async_engine("postgresql+asyncpg://invalid:invalid@localhost/daimon_test")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(None)
        raise AssertionError("unregistered CI must refuse before connecting or provisioning")

    event.listen(engine.sync_engine, "do_connect", forbidden)
    monkeypatch.setattr(sys.modules[__name__], "make_tenant", forbidden)
    try:
        with pytest.raises(ValueError, match="registered isolated CI"):
            await host.__wrapped__(None, engine, None, SimpleNamespace(param=routed))
        assert calls == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("routed", [False, True])
async def test_outcome_fixture_preserves_registered_ci_provisioning(
    db_preflight, db_engine, db_clean, monkeypatch, routed
):
    from types import SimpleNamespace

    from daimon.testing.db import isolated_test_schema
    from sqlalchemy import text

    if db_engine.url.database == "daimon_test" and not (
        os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("CI") == "true"
    ):
        pytest.skip("host outcome proofs require an owned lane database or isolated CI schema")
    require_owned_sessionmaker(db_engine)
    async with db_engine.connect() as db:
        physical = await db.scalar(text("SELECT current_database()"))
    schema = isolated_test_schema(db_engine)
    assert schema is not None
    # Metadata only: the pool still connects to the owned lane database.
    monkeypatch.setattr(db_engine.sync_engine, "url", db_engine.url.set(database="daimon_test"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("CI", "true")
    sm = await host.__wrapped__(db_preflight, db_engine, None, SimpleNamespace(param=routed))
    async with sm() as db:
        assert await db.scalar(text("SELECT current_database()")) == physical
        assert await db.scalar(text("SELECT current_schema()")) == schema


@pytest.mark.parametrize(
    "case", ["foreign", "remote", "query", "contributor", "lane", "ci", "ci_foreign_schema"]
)
def test_outcome_fixture_real_preflight_precedes_database_setup(tmp_path: Path, case: str) -> None:
    run_host_fixture_preflight_probe(
        tmp_path,
        target=(
            "packages/core/tests/turn/test_termination_conformance.py::"
            "test_every_typed_reason_is_read_from_its_exact_committed_row[False-openai]"
        ),
        case=case,
    )
